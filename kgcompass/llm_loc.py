import json
import os
import argparse
import re
from datasets import load_dataset
import openai
from utils import get_commit_method_by_signature, extract_json_code
from config import (
    DATASET_NAME,
    DEEPSEEK_BASE_URL,
    GITHUB_TOKEN,
    TEMPERATURE,
    TOP_P,
    LLM_LOC_MAX,
    BAILIAN_API_KEY,
    MODEL_NAME,
    CANDIDATE_LOCATIONS_MAX,
)
from utils import format_entity_content, context_entity_sort_key
from github import Github
from benchmark import get_target_sample
from language_factory import ParserFactory
try:
    from verilog_timing import classify_verilog_location_groups, normalize_verilog_related_entities
except Exception:
    from .verilog_timing import classify_verilog_location_groups, normalize_verilog_related_entities

def _final_response_content(message_or_text):
    if message_or_text is None:
        return ""
    if isinstance(message_or_text, str):
        text = message_or_text
    else:
        content = getattr(message_or_text, "content", "")
        if isinstance(content, list):
            text = "\n".join(
                str((item.get("text") or item.get("content") or "")) if isinstance(item, dict)
                else str(getattr(item, "text", "") or item)
                for item in content
            )
        else:
            text = str(content or "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^\s*(?:final answer|final)\s*:\s*", "", text, flags=re.IGNORECASE)
    return text.strip()

class PreFaultLocalization:
    def __init__(self, instance_id: str, benchmark_name: str = "swe-bench"):
        self.instance_id = instance_id
        self.dataset_name = DATASET_NAME
        self.benchmark_name = benchmark_name
        self.target_sample = self._load_target_sample()
        if self.target_sample:
            self.language = self.target_sample.get('language', 'python').lower()
        else:
            self.language = 'python'
        
        self.model_name = MODEL_NAME
        # Create a client instance pointing to the OpenAI-compatible endpoint
        self.client = openai.OpenAI(api_key=BAILIAN_API_KEY, base_url=DEEPSEEK_BASE_URL)

    def _load_target_sample(self):
        if self.benchmark_name in {'local', 'verilog-local'}:
            return get_target_sample(self.instance_id, benchmark_name=self.benchmark_name)

        split_name = 'test'
        if 'multi-swe-bench' in self.dataset_name.lower():
            split_name = 'java_verified'

        ds = load_dataset(self.dataset_name, split=split_name)
        self.dataset = {item['instance_id']: item for item in ds}
        return self.dataset.get(self.instance_id)

    def generate(self, prompt, stream=False):
        """Unified interface for generating responses via OpenAI-compatible endpoint"""
        messages = [{'role': 'user', 'content': prompt}]
        try:
            if stream and os.getenv("LOC_STREAM", "1").lower() in {"0", "false", "no"}:
                stream = False
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=TEMPERATURE,
                top_p=TOP_P,
                stream=stream
            )

            if stream:
                raw_chunks = []
                for chunk in response:
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None) or ""
                    reasoning_content = getattr(delta, "reasoning_content", None) or ""
                    if reasoning_content and not content:
                        continue
                    raw_chunks.append(content)
                collected_content = _final_response_content("".join(raw_chunks))
                print(collected_content, end='', flush=True)
                print() # for a newline
                return collected_content
            else:
                return _final_response_content(response.choices[0].message)
        except Exception as e:
            print(f"An error occurred while calling the LLM API: {e}")
            return None

    def pre_locate(self, updated_description, stream=False):
        # This prompt is now universal and will be enhanced by KG context
        if self.language == 'verilog':
            method_format_note = """For Verilog/SystemVerilog, the method field may identify:
- a module body, e.g. `uart_rx.module`
- a procedural block, e.g. `uart_rx.always@42`
- a continuous assignment, e.g. `uart_regs.assign.clear_rx_overflow@35`
- a function, task, macro, or instantiation entity from the Knowledge Graph
Use related Signal/Port/Parameter/Macro/State/GenerateBlock/Testbench/Assertion entities as evidence, but return the nearest editable module block, always block, assign, function/task, or instantiation as the method.
Prefer KG paths with explicit RTL relationships such as READS Signal, WRITES Signal, DRIVES Port, CONNECTS Port, INSTANTIATES module, GUARDS macro, and TRANSITIONS_TO state.
File paths should use `.v`, `.sv`, `.vh`, or `.svh` paths relative to the repository root."""
            example_path = "src/rtl/uart_rx.v"
            example_method = "uart_regs.assign.clear_rx_overflow@35"
        else:
            method_format_note = """The method field should be one of these formats:
- `package.module.Class.method_name`
- `package.module.function_name`"""
            example_path = "package/submodule/file.py"
            example_method = "package.module.Class.method_name"

        prompt_template = """Based on the following bug description and context from a Knowledge Graph, predict potential relevant code locations.
The Knowledge Graph has provided potentially related issues and functions. Use this information to improve your prediction.

Bug Description and Knowledge Graph Context:
{updated_description}

Please provide a JSON array containing the predicted locations where the bug fix is needed. Each location should include the full file path and the full method/function name.

{method_format_note}

Format:
```json
[
    {{
        "file_path": "{example_path}",
        "method": "{example_method}"
    }}
]
```

Note:
- Focus on core functionality rather than test files.
- List the most likely locations first.
- Include up to {LLM_LOC_MAX} primary and related locations.
- Use the provided context to make informed predictions about file paths and method names.
"""
        prompt = prompt_template.format(
            updated_description=updated_description,
            LLM_LOC_MAX=LLM_LOC_MAX,
            method_format_note=method_format_note,
            example_path=example_path,
            example_method=example_method,
        )
        return self.generate(prompt, stream=stream)

    def _local_repo_path(self):
        if self.benchmark_name not in {'local', 'verilog-local'} or not self.target_sample:
            return None
        repo_identifier = self.target_sample.get('repo_identifier') or self.instance_id.rsplit('-', 1)[0]
        repos_dir = os.getenv('VERILOG_REPOS_DIR', 'verilog_repair_cases')
        repo_path = os.path.join(repos_dir, repo_identifier)
        return repo_path if os.path.isdir(repo_path) else None

    def _score_local_entity(self, entity, query, file_path):
        if not query:
            return 0
        haystack = " ".join([
            entity.get('name', ''),
            entity.get('signature', ''),
            entity.get('file_path', ''),
            file_path,
        ]).lower()
        normalized_query = query.lower().replace('instantiates', 'inst').replace(' as ', ' ')
        normalized_query = normalized_query.replace('assign ', 'assign.')
        if normalized_query == entity.get('name', '').lower():
            return 100
        if normalized_query in haystack:
            return 90

        tokens = [
            token.lower()
            for token in re.split(r'[^A-Za-z0-9_]+', query)
            if len(token) > 1 and token.lower() not in {'as', 'at', 'the', 'method'}
        ]
        if not tokens:
            return 0
        return sum(1 for token in tokens if token in haystack)

    def find_local_method_details(self, file_path, qualified_name):
        repo_path = self._local_repo_path()
        if not repo_path:
            return None

        normalized_file_path = file_path.replace('\\', '/').lstrip('./')
        full_file_path = os.path.join(repo_path, *normalized_file_path.split('/'))
        if not os.path.exists(full_file_path):
            return None

        parser = ParserFactory.create_parser(self.language)
        candidates = parser.extract_methods(full_file_path)
        candidates.extend(parser.get_global_methods(full_file_path, os.path.basename(repo_path)))
        candidates.extend(parser.get_global_variables(full_file_path, os.path.basename(repo_path)))

        best_entity = None
        best_score = 0
        for entity in candidates:
            score = self._score_local_entity(entity, qualified_name, normalized_file_path)
            if score > best_score:
                best_entity = entity
                best_score = score

        if best_entity is None or best_score == 0:
            return None

        method_details = dict(best_entity)
        method_details['file_path'] = method_details.get('file_path') or normalized_file_path
        return method_details


def process_instance(directory, instance_id, benchmark_name="swe-bench"):
    """
    Reads a KG location file, uses it to prompt an LLM for more specific locations,
    and enriches the original KG file with the LLM's findings.
    """
    pre_fl = PreFaultLocalization(instance_id, benchmark_name=benchmark_name)
    if pre_fl.target_sample is None:
        return f"Error: Could not find instance {instance_id} in dataset"

    # --- KG-dependent workflow ---
    kg_location_file = os.path.join(os.path.dirname(directory), 'kg_locations', f"{instance_id}.json")
    if not os.path.exists(kg_location_file):
        print(f"Error: KG Location file does not exist at {kg_location_file}")
        return f"Skipping {instance_id}."
    
    with open(kg_location_file, 'r') as f:
        locate_result = json.load(f)
    
    # Build a hint for the LLM using the problem statement and related issues/methods from the KG
    hint_parts = []
    
    problem_statement = pre_fl.target_sample.get('problem_statement', '') or pre_fl.target_sample.get('text', '')
    if problem_statement:
        hint_parts.append(f"## Problem Statement\n{problem_statement}")

    # Add related issues from KG
    if locate_result.get('related_entities', {}).get('issues'):
        sorted_issues = sorted(
            locate_result['related_entities']['issues'],
            key=lambda x: context_entity_sort_key(x, 'issues')
        )
        if sorted_issues:
            issue_texts = []
            # Add the top issue and up to 2 related issues
            for issue in sorted_issues[:3]:
                title = issue.get('title', 'N/A')
                content = issue.get('content', 'N/A')
                issue_texts.append(f"### Issue: {title}\n\n{content}")
            if issue_texts:
                hint_parts.append("## Potentially Related Issues from Knowledge Graph\n\n" + "\n\n".join(issue_texts))

    # Add editable RTL/code targets from KG.
    related_entities = locate_result.setdefault('related_entities', {})
    if pre_fl.language == 'verilog':
        related_entities.update(normalize_verilog_related_entities(related_entities))
    edit_targets = (
        related_entities.get('edit_targets')
        or related_entities.get('direct_drivers')
        or related_entities.get('methods')
    )
    if edit_targets:
        method_texts = []
        sorted_methods = sorted(
            edit_targets,
            key=lambda x: context_entity_sort_key(x, 'edit_targets')
        )[:CANDIDATE_LOCATIONS_MAX]
        
        methods_content = ""
        for method in sorted_methods:
            methods_content += format_entity_content(method)
        if methods_content:
            hint_parts.append("## Editable RTL Targets from Knowledge Graph\n\n" + methods_content)

    evidence_entities = (
        related_entities.get('evidence_entities')
        or related_entities.get('rtl_entities')
    )
    if evidence_entities:
        rtl_entities = sorted(
            evidence_entities,
            key=lambda x: context_entity_sort_key(x, 'evidence_entities')
        )[:CANDIDATE_LOCATIONS_MAX]
        rtl_content = ""
        for entity in rtl_entities:
            rtl_content += format_entity_content(entity, show_path=True)
        if rtl_content:
            hint_parts.append("## RTL Evidence Entities from Knowledge Graph\n\n" + rtl_content)

    hint = "\n\n".join(hint_parts).replace('\\n', '\n')
    
    print(f"[llm_loc] sending localization prompt to model={pre_fl.model_name}; chars={len(hint)}")
    if os.getenv("KGCOMPASS_VERBOSE_PROMPTS", "0") == "1":
        print("----- localization prompt begin -----")
        print(hint)
        print("----- localization prompt end -----")

    print("[llm_loc] waiting for localization output")
    raw_llm_output = pre_fl.pre_locate(hint, stream=True)
    print(f"[llm_loc] raw output chars={len(raw_llm_output or '')}")

    if raw_llm_output is None:
        print(f"Error: Did not receive a valid response from the LLM for {instance_id}. Skipping.")
        # To avoid breaking the chain, we can create an empty/error JSON or just skip.
        # Skipping seems safer to not pollute results.
        return f"LLM call failed for {instance_id}, skipping file generation."

    json_str = extract_json_code(raw_llm_output)

    try:
        llm_hint_list = json.loads(json_str)
        if not isinstance(llm_hint_list, list):
            llm_hint_list = []
    except json.JSONDecodeError:
        print(f"Error: Failed to parse JSON from LLM output for {instance_id}.")
        llm_hint_list = []
    
    # Get repository information to fetch code snippets
    repo_name = pre_fl.target_sample.get('repo')
    commit_id = pre_fl.target_sample.get('base_commit')
    
    github_repo = None
    commit = None
    if pre_fl.benchmark_name not in {'local', 'verilog-local'} and repo_name and commit_id and GITHUB_TOKEN:
        try:
            g = Github(GITHUB_TOKEN)
            github_repo = g.get_repo(repo_name)
            commit = github_repo.get_commit(commit_id)
        except Exception as e:
            print(f"Warning: Could not get GitHub repo/commit for {instance_id}: {e}")

    # Add detailed information for each method identified by the LLM to the locate_result
    cnt = 0
    if isinstance(llm_hint_list, list):
        for item in llm_hint_list:
            if not isinstance(item, dict) or 'file_path' not in item or 'method' not in item:
                continue

            qualified_name_from_llm = item['method']
            file_path = item['file_path']
            
            method_details = None
            if pre_fl.language == 'python' and github_repo and commit:
                try:
                    method_details = get_commit_method_by_signature(github_repo, commit, file_path, qualified_name_from_llm)
                except Exception as e:
                    print(f"Warning: get_commit_method_by_signature failed for {qualified_name_from_llm} in {file_path}: {e}")
            elif pre_fl.benchmark_name in {'local', 'verilog-local'}:
                method_details = pre_fl.find_local_method_details(file_path, qualified_name_from_llm)
            
            if method_details is not None:
                cnt += 1
                if cnt > LLM_LOC_MAX:
                    break
                
                method_details['path'] = [{"start_node": "root", "description": "points to method", "type": "INFERENCE", "end_node": method_details['name']}]
                method_details['type'] = 'method'
                method_details['similarity'] = 1.0
                related_entities.setdefault('methods', []).append(method_details)
                if pre_fl.language == 'verilog':
                    related_entities.update(normalize_verilog_related_entities(related_entities))
    
    # Save the augmented locate_result object, overwriting the file in the llm_locations dir
    output_path = os.path.join(directory, f"{instance_id}.json")
    with open(output_path, 'w') as f:
        json.dump(locate_result, f, indent=4)
    
    return f"LLM location saved to {output_path}"


def main():
    parser = argparse.ArgumentParser(description="LLM-based Fault Localization using KG context.")
    parser.add_argument("directory", type=str, help="Directory to save the results")
    parser.add_argument("--instance_id", type=str, help="The instance_id to process.", required=True)
    parser.add_argument("--benchmark_name", type=str, default=os.getenv("BENCHMARK_NAME", "swe-bench"), help="Benchmark source name, e.g. swe-bench or verilog-local.")
    args = parser.parse_args()

    # Create the directory if it doesn't exist
    if not os.path.exists(args.directory):
        os.makedirs(args.directory)

    print(f"Processing a single specified instance: {args.instance_id}")
    result = process_instance(args.directory, args.instance_id, benchmark_name=args.benchmark_name)
    print(result)


if __name__ == '__main__':
    main()
