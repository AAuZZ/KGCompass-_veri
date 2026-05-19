from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime

import openai

from benchmark import get_target_sample
from config import (
    BAILIAN_API_KEY,
    DEEPSEEK_BASE_URL,
    MAX_INPUT_LENGTH,
    MODEL_NAME,
    TEMPERATURE,
    TOP_P,
)
from repair_loop import FailureAnalyzer, PatchApplicationResult, PromptBuilder
from utils import (
    context_entity_sort_key,
    format_entity_content,
    parse_diff_edit_commands_strict,
    split_edit_multifile_commands,
    check_syntax,
)
from verilog_validation import VerilogValidationRunner
from experience_store import ExperienceStore
try:
    from verilog_kg_digest import build_verilog_bug_kg_digest
except Exception:
    from .verilog_kg_digest import build_verilog_bug_kg_digest
try:
    from verilog_kg_slice import build_verilog_fault_neighborhood
except Exception:
    from .verilog_kg_slice import build_verilog_fault_neighborhood
try:
    from verilog_timing import annotate_verilog_entity, classify_verilog_location_groups, normalize_verilog_related_entities
except Exception:
    from .verilog_timing import annotate_verilog_entity, classify_verilog_location_groups, normalize_verilog_related_entities


def _compact_text(text: str, limit: int = 1200) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]..."


def _unique_keep_order(items):
    seen = set()
    ordered = []
    for item in items:
        marker = str(item)
        if marker in seen:
            continue
        ordered.append(item)
        seen.add(marker)
    return ordered


def _hash_path(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    sha1 = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            sha1.update(chunk)
    return sha1.hexdigest()


_PROGRESS_LOG_PATH = os.getenv("KGCOMPASS_REPAIR_PROGRESS_LOG", "")


def _log(stage: str, message: str = ""):
    prefix = f"[repair:{stage}]"
    line = f"{prefix} {message}" if message else prefix
    print(line, flush=True)
    if _PROGRESS_LOG_PATH:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(_PROGRESS_LOG_PATH)), exist_ok=True)
            with open(_PROGRESS_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass


def _final_response_content(message_or_text):
    if message_or_text is None:
        return ""
    if isinstance(message_or_text, str):
        text = message_or_text
    else:
        content = getattr(message_or_text, "content", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(getattr(item, "text", "") or item))
            text = "\n".join(part for part in parts if part)
        else:
            text = str(content or "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"^\s*(?:final answer|final)\s*:\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


class CodeRepair:
    def __init__(self):
        self.temperature = TEMPERATURE
        self.top_p = TOP_P
        self.model = MODEL_NAME
        self.MAX_INPUT_LENGTH = int(MAX_INPUT_LENGTH)
        self.client = openai.OpenAI(api_key=BAILIAN_API_KEY, base_url=DEEPSEEK_BASE_URL)
        self.failure_analyzer = FailureAnalyzer()
        self.prompt_builder = PromptBuilder(max_input_length=self.MAX_INPUT_LENGTH)
        self.experience_store = ExperienceStore()

    def get_completion(self, prompt, stream=False):
        messages = [{"role": "user", "content": prompt}]
        try:
            if stream and os.getenv("REPAIR_STREAM", "1").lower() in {"0", "false", "no"}:
                stream = False
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                top_p=self.top_p,
                stream=stream,
            )
            if stream:
                return response
            return _final_response_content(response.choices[0].message)
        except Exception as e:
            print(f"An error occurred while calling the LLM API: {e}")
            return None

    def adjust_command_indentation(self, command, indent_change):
        search_replace = command["command"].split("\n=======\n")
        search_part = search_replace[0].split("<<<<<<< SEARCH")[1].strip("\n")
        replace_part = search_replace[1].split(">>>>>>> REPLACE")[0].strip("\n")

        def adjust_lines(text):
            lines = text.splitlines()
            if indent_change < 0:
                width = abs(indent_change)
                return "\n".join(
                    line[width:] if line.startswith(" " * width) else line
                    for line in lines
                )
            return "\n".join(" " * indent_change + line for line in lines)

        adjusted_search = adjust_lines(search_part)
        adjusted_replace = adjust_lines(replace_part)

        return {
            "command": f"<<<<<<< SEARCH\n{adjusted_search}\n=======\n{adjusted_replace}\n>>>>>>> REPLACE",
            "start_line": command["start_line"],
            "end_line": command["end_line"],
        }

    def _language_prompt_parts(self, language):
        if language == "verilog":
            return {
                "language_name": "Verilog/SystemVerilog",
                "fence_language": "verilog",
                "example_file": "src/rtl/spi_flash_irq.sv",
                "example_start_line": 42,
                "example_end_line": 57,
                "example_search": "    always @(posedge clk or negedge reset_n) begin\n        if (!reset_n) begin\n            irq_sticky <= 1'b0;\n            status_fifo_nonempty <= 1'b0;\n        end else begin\n            if (clear_irq) begin\n                irq_sticky <= 1'b0;\n            end else if (rx_overrun || fifo_overflow) begin\n                irq_sticky <= 1'b1;\n            end\n\n            status_fifo_nonempty <= fifo_nonempty;\n        end\n    end",
                "example_replace": "    always @(posedge clk or negedge reset_n) begin\n        if (!reset_n) begin\n            irq_sticky <= 1'b0;\n            status_fifo_nonempty <= 1'b0;\n        end else begin\n            if (clear_irq) begin\n                irq_sticky <= 1'b0;\n                status_fifo_nonempty <= 1'b0;\n            end else begin\n                if (rx_overrun || fifo_overflow) begin\n                    irq_sticky <= 1'b1;\n                end\n                if (fifo_nonempty) begin\n                    status_fifo_nonempty <= 1'b1;\n                end\n            end\n        end\n    end",
                "use_replace_only": True,
                "language_notes": (
                    "For Verilog/SystemVerilog, preserve module ports, output kinds, signal widths, nonblocking assignments, "
                    "reset behavior, clocked vs combinational block style, and cross-module interfaces. "
                    "Prefer editing the smallest RTL unit that directly implements the failing behavior. "
                    "Treat evidence entities as constraints and do not edit them unless the issue clearly points there. "
                    "Use explicit KG relations such as READS, WRITES, DRIVES, CONNECTS, INSTANTIATES, GUARDS, "
                    "TRANSITIONS_TO, TESTS, and EXERCISES to reason about signal flow and state behavior before editing. "
                    "If the bug is a wiring issue, edit the instance connection. If it is a sticky-state or latch issue, "
                    "edit the smallest always block in place and keep the existing interface unchanged. "
                    "If a targeted testbench checks an idle output immediately after a register write, make the idle value "
                    "visible without waiting for a later transaction edge."
                ),
            }

        return {
            "language_name": "python",
            "fence_language": "python",
            "example_file": "django/core/management/commands/migrate.py",
            "example_start_line": 120,
            "example_end_line": 122,
            "example_search": "    def my_method(self):\n        result = 1 + 1\n        return result",
            "example_replace": "    def my_method(self):\n        result = 1 + 2  # Fixed the calculation\n        return result",
            "language_notes": "",
        }

    def _extract_edit_blocks(self, raw_output_text, language):
        preferred = {"python": ["python"], "verilog": ["verilog", "systemverilog", "sv"]}.get(language, [language])
        blocks = []
        fence_pattern = re.compile(r"```([A-Za-z0-9_-]*)\n(.*?)\n```", re.DOTALL)
        for match in fence_pattern.finditer(raw_output_text):
            fence_lang, block = match.group(1), match.group(2)
            if fence_lang and fence_lang.lower() not in preferred:
                continue
            if "### " not in block:
                prefix = raw_output_text[:match.start()]
                header_match = re.search(
                    r"(###\s+[^\n]+\n(?:-\s+start_line\s*:\s*\d+\n)?(?:-\s+end_line\s*:\s*\d+\n)?)\s*$",
                    prefix,
                )
                if header_match:
                    block = header_match.group(1) + block
            blocks.append(block)
        blocks = [block for block in blocks if "<<<<<<< SEARCH" in block and ">>>>>>> REPLACE" in block]
        return blocks or [raw_output_text]

    def _candidate_is_valid(self, code, language):
        if not code.strip():
            return False
        if language == "python":
            return check_syntax(code)
        return True

    def _load_benchmark_item(self, instance_id):
        benchmark_name = os.getenv("BENCHMARK_NAME", "verilog-local")
        if benchmark_name in {"local", "verilog-local"}:
            return get_target_sample(instance_id, benchmark_name=benchmark_name)
        return None

    def _resolve_repo_path(self, playground_dir, repo_identifier, default_root=None):
        if playground_dir is None:
            playground_dir = default_root or os.path.join(os.getcwd(), "playground")
        if repo_identifier is None:
            raise ValueError("repo_identifier is required when playground_dir is not an explicit repo root")
        return os.path.abspath(os.path.join(playground_dir, repo_identifier))

    def _clone_base_repo(self, source_repo_path):
        source_repo_path = os.path.abspath(source_repo_path)
        workdir = tempfile.mkdtemp(prefix="kgcompass_patch_", dir=os.path.dirname(source_repo_path))
        clone_path = os.path.join(workdir, os.path.basename(source_repo_path))
        subprocess.run(["git", "clone", "--quiet", source_repo_path, clone_path], check=True)
        return os.path.abspath(workdir), os.path.abspath(clone_path)

    def _clone_repo_to_attempt_root(self, source_repo_path, attempt_root):
        source_repo_path = os.path.abspath(source_repo_path)
        os.makedirs(attempt_root, exist_ok=True)
        workdir = tempfile.mkdtemp(prefix="kgcompass_patch_", dir=attempt_root)
        clone_path = os.path.join(workdir, os.path.basename(source_repo_path))
        subprocess.run(["git", "clone", "--quiet", source_repo_path, clone_path], check=True)
        return os.path.abspath(workdir), os.path.abspath(clone_path)

    def _write_json(self, path, payload):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)

    def _group_entities_for_prompt(self, related_entities, language="python"):
        grouped = []
        if not related_entities:
            return grouped

        if language == "verilog":
            group_order = [
                ("fault_anchor_entities", "Fault Anchor Entities"),
                ("repair_anchor_entities", "Repair Anchor Entities"),
                ("edit_targets", "Editable RTL Targets"),
                ("direct_drivers", "Direct Drivers"),
                ("top_level_wiring", "Top-Level Wiring"),
                ("config_registers", "Config Registers"),
                ("evidence_entities", "RTL Evidence Entities"),
                ("methods", "Legacy Methods"),
                ("rtl_entities", "Legacy RTL Entities"),
                ("issues", "Related Issues"),
            ]
        else:
            group_order = [
                ("methods", "Relevant Methods"),
                ("classes", "Relevant Classes"),
                ("issues", "Related Issues"),
            ]

        for group_name, _label in group_order:
            entities = related_entities.get(group_name) or []
            if not entities:
                continue
            deduped = {}
            for entity in entities:
                key = (
                    entity.get("name"),
                    entity.get("signature"),
                    entity.get("file_path"),
                    entity.get("start_line"),
                    entity.get("end_line"),
                )
                deduped[key] = entity
            sorted_entities = sorted(deduped.values(), key=lambda item: context_entity_sort_key(item, group_name))
            grouped.append((group_name, sorted_entities))
        return grouped

    def _truncate_entity_groups(self, grouped_entities, language="python"):
        if language == "verilog":
            limits = {
                "fault_anchor_entities": 6,
                "repair_anchor_entities": 6,
                "edit_targets": 8,
                "direct_drivers": 6,
                "top_level_wiring": 6,
                "config_registers": 4,
            "evidence_entities": 10,
            "methods": 8,
            "rtl_entities": 10,
            "issues": 4,
            }
        else:
            limits = {
                "methods": 10,
                "classes": 6,
                "issues": 4,
            }
        truncated = []
        for group_name, entities in grouped_entities:
            limit = limits.get(group_name, 8)
            truncated.append((group_name, entities[:limit]))
        return truncated

    def _render_grouped_entities(self, grouped_entities, header_map, show_path_groups=None):
        sections = []
        show_path_groups = set(show_path_groups or [])
        for group_name, entities in grouped_entities:
            if not entities:
                continue
            sections.append(f"## {header_map.get(group_name, group_name)}")
            for entity in entities:
                sections.append(format_entity_content(entity, show_path=group_name in show_path_groups).rstrip())
        return "\n".join(sections)

    def _line_numbered_excerpt(self, lines, start_line, end_line):
        excerpt = []
        for line_no in range(start_line, end_line + 1):
            excerpt.append(f"{line_no}: {lines[line_no - 1]}")
        return "\n".join(excerpt)

    def _extract_verilog_issue_file_hints(self, problem_statement, repo_path="", related=None, limit=8):
        text = problem_statement or ""
        hints = []
        seen = set()

        def add_hint(file_path):
            normalized = str(file_path or "").replace("\\", "/").strip()
            if not normalized or normalized in seen:
                return
            seen.add(normalized)
            hints.append(normalized)

        related_files = []
        for group_entities in (related or {}).values():
            for entity in group_entities or []:
                file_path = str(entity.get("file_path") or "").replace("\\", "/").strip()
                if file_path:
                    related_files.append(file_path)
        related_files = _unique_keep_order(related_files)

        file_patterns = [
            r"`([^`]+\.(?:sv|svh|v|vh))`",
            r"(?<![\w./-])([A-Za-z0-9_./-]+\.(?:sv|svh|v|vh))\b",
        ]
        raw_hints = []
        for pattern in file_patterns:
            raw_hints.extend(match.group(1).strip() for match in re.finditer(pattern, text, flags=re.IGNORECASE))

        module_like_tokens = []
        for token in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{4,})\b", text):
            token = token.strip()
            if not token or token.lower() in {
                "module",
                "signal",
                "output",
                "input",
                "always",
                "assign",
                "reset",
                "clock",
                "error",
                "issue",
                "logic",
                "state",
                "register",
            }:
                continue
            if "_" not in token and not any(ch.isdigit() for ch in token):
                continue
            module_like_tokens.append(token)
        module_like_tokens = _unique_keep_order(module_like_tokens)[:12]

        def repo_file_candidates():
            if not repo_path or not os.path.isdir(repo_path):
                return []
            candidates = []
            for root, _, files in os.walk(repo_path):
                for filename in files:
                    if not re.search(r"\.(?:sv|svh|v|vh)$", filename, re.IGNORECASE):
                        continue
                    rel_path = os.path.relpath(os.path.join(root, filename), repo_path).replace("\\", "/")
                    candidates.append(rel_path)
            return _unique_keep_order(candidates)

        repo_candidates = repo_file_candidates()

        for raw_hint in raw_hints:
            normalized = raw_hint.replace("\\", "/").strip("`'\" ")
            if not normalized:
                continue
            if re.search(r"\.(?:sv|svh|v|vh)$", normalized, re.IGNORECASE):
                if repo_path:
                    abs_hint = os.path.abspath(os.path.join(repo_path, *normalized.split("/")))
                    if os.path.exists(abs_hint):
                        add_hint(os.path.relpath(abs_hint, repo_path).replace("\\", "/"))
                        continue
                basename = os.path.splitext(os.path.basename(normalized))[0].lower()
                for candidate in [*related_files, *repo_candidates]:
                    candidate_base = os.path.splitext(os.path.basename(candidate))[0].lower()
                    if candidate_base == basename:
                        add_hint(candidate)
                        break
                else:
                    add_hint(normalized)
                continue

            token_lower = normalized.lower()
            for candidate in [*related_files, *repo_candidates]:
                candidate_base = os.path.splitext(os.path.basename(candidate))[0].lower()
                candidate_path = candidate.lower()
                if (
                    candidate_base == token_lower
                    or token_lower in candidate_base
                    or candidate_base in token_lower
                    or token_lower in candidate_path
                ):
                    add_hint(candidate)
                    break

        for token in module_like_tokens:
            token_lower = token.lower()
            for candidate in [*related_files, *repo_candidates]:
                candidate_base = os.path.splitext(os.path.basename(candidate))[0].lower()
                candidate_path = candidate.lower()
                if (
                    candidate_base == token_lower
                    or token_lower in candidate_base
                    or candidate_base in token_lower
                    or token_lower in candidate_path
                ):
                    add_hint(candidate)
                    break
            if len(hints) >= limit:
                break

        return hints[:limit]

    def _collect_candidate_source_context(
        self,
        repo_path,
        related,
        language="python",
        max_files=5,
        full_file_limit=400,
        window=100,
        fault_anchor_entities=None,
        repair_anchor_entities=None,
        fault_anchor_spans=None,
        preferred_files=None,
    ):
        if language != "verilog" or not repo_path:
            return ""

        ranked_entities = []
        preferred_files = _unique_keep_order([
            str(path or "").replace("\\", "/").strip()
            for path in (preferred_files or [])
            if str(path or "").strip()
        ])
        fault_anchor_entities = list(fault_anchor_entities or [])
        repair_anchor_entities = list(repair_anchor_entities or [])
        fault_anchor_spans = list(fault_anchor_spans or [])
        anchor_file_spans = {}
        for span in fault_anchor_spans:
            if not isinstance(span, dict):
                continue
            file_path = str(span.get("file_path") or "").replace("\\", "/").strip()
            if not file_path:
                continue
            try:
                start_line = int(span.get("start_line") or 0)
                end_line = int(span.get("end_line") or 0)
            except (TypeError, ValueError):
                start_line = end_line = 0
            if start_line <= 0 or end_line <= 0:
                continue
            anchor_file_spans.setdefault(file_path, []).append((start_line, end_line))

        for entity in repair_anchor_entities:
            file_path = str(entity.get("file_path") or "").replace("\\", "/")
            if not file_path or file_path.startswith("tb/"):
                continue
            if not re.search(r"\.(?:sv|v|svh|vh)$", file_path, re.IGNORECASE):
                continue
            ranked_entities.append(("repair_anchor", entity))

        for file_path, spans in anchor_file_spans.items():
            for start_line, end_line in spans[:max_files]:
                ranked_entities.append((
                    "fault_anchor",
                    {
                        "file_path": file_path,
                        "start_line": start_line,
                        "end_line": end_line,
                        "name": "validation_failure_anchor",
                        "signature": f"{file_path}:{start_line}-{end_line}",
                        "repair_role": "fault_anchor",
                        "verilog_kind": "fault_anchor",
                        "semantic_summary": "Validation failure anchor window",
                        "source_code": "",
                    },
                ))

        for entity in fault_anchor_entities:
            file_path = str(entity.get("file_path") or "").replace("\\", "/")
            if not file_path or file_path.startswith("tb/"):
                continue
            if not re.search(r"\.(?:sv|v|svh|vh)$", file_path, re.IGNORECASE):
                continue
            ranked_entities.append(("fault_anchor", entity))

        for group_name in ("direct_drivers", "edit_targets", "top_level_wiring", "config_registers", "methods", "rtl_entities"):
            for entity in related.get(group_name) or []:
                file_path = str(entity.get("file_path") or "").replace("\\", "/")
                if not file_path or file_path.startswith("tb/"):
                    continue
                if not re.search(r"\.(?:sv|v|svh|vh)$", file_path, re.IGNORECASE):
                    continue
                ranked_entities.append((group_name, entity))

        selected = {}
        for file_path in preferred_files:
            if not file_path:
                continue
            selected.setdefault(file_path, {"group": "issue_mentioned", "entities": []})

        if fault_anchor_entities:
            for entity in fault_anchor_entities:
                file_path = str(entity.get("file_path") or "").replace("\\", "/")
                if not file_path:
                    continue
                selected.setdefault(file_path, {"group": "fault_anchor", "entities": []})

        if repair_anchor_entities:
            for entity in repair_anchor_entities:
                file_path = str(entity.get("file_path") or "").replace("\\", "/")
                if not file_path:
                    continue
                selected.setdefault(file_path, {"group": "repair_anchor", "entities": []})

        for group_name, entity in ranked_entities:
            file_path = str(entity.get("file_path") or "").replace("\\", "/")
            if file_path in selected:
                selected[file_path]["entities"].append(entity)
                continue
            selected[file_path] = {"group": group_name, "entities": [entity]}
            if len(selected) >= max_files:
                break

        if not selected:
            return ""

        sections = []
        for file_path, info in selected.items():
            abs_path = os.path.abspath(os.path.join(repo_path, *file_path.split("/")))
            if not os.path.exists(abs_path):
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            if not lines:
                continue

            force_full_file = file_path in preferred_files or file_path in anchor_file_spans
            if force_full_file or len(lines) <= full_file_limit:
                start_line, end_line = 1, len(lines)
                mode = "full_file_preferred" if force_full_file else "full_file"
                if file_path in anchor_file_spans:
                    mode = "full_file_fault_anchor"
            else:
                starts = []
                ends = []
                for entity in info["entities"]:
                    try:
                        start = int(entity.get("start_line") or 0)
                        end = int(entity.get("end_line") or 0)
                    except (TypeError, ValueError):
                        start = end = 0
                    if start > 0:
                        starts.append(start)
                    if end >= start and end > 0:
                        ends.append(end)
                if file_path in anchor_file_spans:
                    for start, end in anchor_file_spans[file_path]:
                        starts.append(start)
                        ends.append(end)
                anchor_start = min(starts) if starts else 1
                anchor_end = max(ends) if ends else anchor_start
                start_line = max(1, anchor_start - window)
                end_line = min(len(lines), anchor_end + window)
                mode = f"window_around_candidates_{window}"
                if file_path in anchor_file_spans:
                    mode = f"window_around_fault_anchor_{window}"

            entity_lines = []
            for entity in info["entities"][:5]:
                entity_lines.append(
                    f"- {entity.get('file_path')}:{entity.get('start_line')}-{entity.get('end_line')} "
                    f"{entity.get('signature') or entity.get('name')} "
                    f"[{entity.get('repair_role') or entity.get('verilog_kind') or entity.get('type') or 'unknown'}]"
                )
            anchor_lines = []
            for start_line_anchor, end_line_anchor in anchor_file_spans.get(file_path, [])[:5]:
                anchor_lines.append(f"- fault_anchor_span: {file_path}:{start_line_anchor}-{end_line_anchor}")

            sections.append(
                "\n".join([
                    f"### {file_path}",
                    f"- source_mode: {mode}",
                    f"- shown_lines: {start_line}-{end_line}",
                    *anchor_lines,
                    "- candidate_entities:",
                    *entity_lines,
                    "```verilog",
                    self._line_numbered_excerpt(lines, start_line, end_line),
                    "```",
                ])
            )

        return "\n\n".join(sections)

    def _extract_json_object(self, text):
        text = (text or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return {}
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _collect_debug_expanded_context(
        self,
        repo_path,
        diagnosis,
        fallback_context="",
        failure_file_path="",
        max_files=5,
        full_file_limit=800,
        window=100,
    ):
        if not repo_path:
            return fallback_context

        requested = []
        failure_file_path = str(failure_file_path or "").replace("\\", "/").strip()
        if failure_file_path and re.search(r"\.(?:sv|v|svh|vh)$", failure_file_path, re.IGNORECASE):
            requested.append(failure_file_path)
        for path in diagnosis.get("need_files") or []:
            path = str(path or "").replace("\\", "/").strip()
            if path and re.search(r"\.(?:sv|v|svh|vh)$", path, re.IGNORECASE):
                requested.append(path)
        for item in diagnosis.get("edit_candidates") or []:
            if isinstance(item, dict):
                path = str(item.get("file") or item.get("file_path") or "").replace("\\", "/").strip()
                if path and re.search(r"\.(?:sv|v|svh|vh)$", path, re.IGNORECASE):
                    requested.append(path)
        requested = _unique_keep_order([path for path in requested if not path.startswith("tb/")])[:max_files]
        if not requested:
            return fallback_context

        keywords = []
        for key in ("observed_signal", "expected_behavior", "failure_hypothesis"):
            value = diagnosis.get(key)
            if isinstance(value, str):
                keywords.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", value))
        for entry in diagnosis.get("signal_chain") or []:
            if isinstance(entry, str):
                keywords.extend(re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", entry))
        keywords = [kw for kw in _unique_keep_order(keywords) if len(kw) > 2][:12]

        sections = []
        for file_path in requested:
            abs_path = os.path.abspath(os.path.join(repo_path, *file_path.split("/")))
            if not abs_path.startswith(os.path.abspath(repo_path)) or not os.path.exists(abs_path):
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.read().splitlines()
            except OSError:
                continue
            if not lines:
                continue

            force_full_file = file_path in requested
            if force_full_file or len(lines) <= full_file_limit:
                start_line, end_line = 1, len(lines)
                mode = "full_file_requested_by_debug_diagnosis" if force_full_file else "full_file"
            else:
                hits = []
                for idx, line in enumerate(lines, start=1):
                    if any(re.search(rf"\b{re.escape(keyword)}\b", line) for keyword in keywords):
                        hits.append(idx)
                if hits:
                    anchor_start, anchor_end = min(hits), max(hits)
                else:
                    anchor_start = anchor_end = 1
                start_line = max(1, anchor_start - window)
                end_line = min(len(lines), anchor_end + window)
                mode = f"window_requested_by_debug_diagnosis_{window}"

            sections.append(
                "\n".join(
                    [
                        f"### {file_path}",
                        f"- source_mode: {mode}",
                        f"- shown_lines: {start_line}-{end_line}",
                        "```verilog",
                        self._line_numbered_excerpt(lines, start_line, end_line),
                        "```",
                    ]
                )
            )

        expanded = "\n\n".join(sections)
        return expanded or fallback_context

    def _format_debug_kg_context(self, related, analysis=None, limit_per_group=6):
        if not related:
            return ""
        digest = build_verilog_fault_neighborhood(
            related,
            issue_text="",
            analysis=analysis,
            repo_root="",
            limit_per_group=limit_per_group,
            max_paths=10,
        )
        return digest.get("text", "")

    def _build_debug_patch_prompt(
        self,
        *,
        problem_statement,
        candidate_source_context,
        hard_constraints,
        language_prompt_parts,
        attempt_index,
        max_attempts,
        analysis,
        prior_attempts,
        baseline_note,
        candidate_repo_path,
        language,
    ):
        if language != "verilog":
            return self.prompt_builder.build_debug_prompt(
                problem_statement=problem_statement,
                candidate_source_context=candidate_source_context,
                hard_constraints=hard_constraints,
                language_prompt_parts=language_prompt_parts,
                attempt_index=attempt_index,
                max_attempts=max_attempts,
                analysis=analysis,
                prior_attempts=prior_attempts,
                baseline_note=baseline_note,
            )

        diagnosis_prompt = self.prompt_builder.build_debug_diagnosis_prompt(
            problem_statement=problem_statement,
            candidate_source_context=candidate_source_context,
            analysis=analysis,
            prior_attempts=prior_attempts,
            baseline_note=baseline_note,
            max_files=5,
        )
        _log("debug-diagnose", "requesting structured diagnosis from LLM")
        diagnosis_text = self.get_completion(diagnosis_prompt, stream=False) or ""
        diagnosis = self._extract_json_object(diagnosis_text)
        if not diagnosis:
            _log("debug-diagnose", "diagnosis JSON parse failed; using fallback diagnosis")
            diagnosis = {
                "failure_hypothesis": analysis.summary or analysis.failure_reason or "debug diagnosis unavailable",
                "observed_signal": analysis.observed_signal,
                "expected_behavior": analysis.expected_idle,
                "signal_chain": [],
                "need_files": [],
                "edit_candidates": [],
                "do_not_change": ["testbench", "unrelated RTL"],
            }
        else:
            requested = diagnosis.get("need_files") or []
            hypothesis = str(diagnosis.get("failure_hypothesis") or "")
            _log("debug-diagnose", f"hypothesis={_compact_text(hypothesis, 180)}")
            _log("debug-diagnose", f"requested_files={requested[:5]}")

        expanded_context = self._collect_debug_expanded_context(
            candidate_repo_path,
            diagnosis,
            fallback_context=candidate_source_context,
            failure_file_path=str(getattr(analysis, "failure_file_path", "") or ""),
        )
        _log("debug-context", f"expanded_context_chars={len(expanded_context or '')}")
        return self.prompt_builder.build_debug_patch_prompt(
            problem_statement=problem_statement,
            expanded_source_context=expanded_context,
            diagnosis=diagnosis,
            hard_constraints=hard_constraints,
            language_prompt_parts=language_prompt_parts,
            attempt_index=attempt_index,
            max_attempts=max_attempts,
            analysis=analysis,
            prior_attempts=prior_attempts,
            baseline_note=baseline_note,
        )

    def _collect_prompt_context(self, locate_result, verification_cfg, language, repo_path=None):
        related = locate_result.get("related_entities", {}) if locate_result else {}
        if language == "verilog":
            related = normalize_verilog_related_entities(related)
        issues = related.get("issues") or []
        sorted_issues = sorted(issues, key=lambda x: x.get("similarity", 0), reverse=True)

        problem_statement = ""
        if sorted_issues:
            problem_statement += f"### {sorted_issues[0].get('title', 'N/A')}\n{sorted_issues[0].get('content', '')}"
            if len(sorted_issues) > 1 and sorted_issues[1].get("similarity", 0) > 0.1:
                problem_statement += f"\n\n### {sorted_issues[1].get('title', 'N/A')}\n{sorted_issues[1].get('content', '')}"
        if not problem_statement:
            problem_statement = locate_result.get("issue", "No issue description provided.") if locate_result else "No issue description provided."

        edit_targets = related.get("edit_targets") or related.get("direct_drivers") or related.get("methods") or []
        evidence_entities = related.get("evidence_entities") or related.get("rtl_entities") or []

        edit_lines = []
        for entity in edit_targets[:8]:
            edit_lines.append(
                f"- {entity.get('file_path', '')}:{entity.get('start_line')}-{entity.get('end_line')} "
                f"{entity.get('signature') or entity.get('name', '')} "
                f"[{entity.get('verilog_kind') or entity.get('type') or 'unknown'}]"
            )

        evidence_lines = []
        for entity in evidence_entities[:10]:
            evidence_lines.append(
                f"- {entity.get('file_path', '')}:{entity.get('start_line')}-{entity.get('end_line')} "
                f"{entity.get('signature') or entity.get('name', '')} "
                f"[{entity.get('verilog_kind') or entity.get('type') or 'unknown'}]"
            )

        issue_lines = []
        for issue in sorted_issues[:3]:
            title = issue.get("title", "N/A")
            content = _compact_text(issue.get("content", ""), 500)
            issue_lines.append(f"- {title}: {content}")

        files = []
        edit_target_kinds = []
        evidence_kinds = []
        for entity in edit_targets + evidence_entities + issues:
            file_path = entity.get("file_path")
            if file_path:
                files.append(file_path)
            kind = str(entity.get("verilog_kind") or entity.get("type") or "").strip()
            if entity in edit_targets and kind:
                edit_target_kinds.append(kind)
            if entity in evidence_entities and kind:
                evidence_kinds.append(kind)
        files = _unique_keep_order(files)
        edit_target_kinds = _unique_keep_order([kind.lower() for kind in edit_target_kinds if kind])
        evidence_kinds = _unique_keep_order([kind.lower() for kind in evidence_kinds if kind])

        localized_summary = []
        if issue_lines:
            localized_summary.append("Related issues:")
            localized_summary.extend(issue_lines)
        if edit_lines:
            localized_summary.append("Primary edit targets:")
            localized_summary.extend(edit_lines)
        if language == "verilog":
            for label, group_name in [
                ("Direct drivers:", "direct_drivers"),
                ("Top-level wiring:", "top_level_wiring"),
                ("Config registers:", "config_registers"),
                ("Repair anchors:", "repair_anchor_entities"),
            ]:
                group_entities = related.get(group_name) or []
                if not group_entities:
                    continue
                localized_summary.append(label)
                for entity in group_entities[:6]:
                    localized_summary.append(
                        f"- {entity.get('file_path', '')}:{entity.get('start_line')}-{entity.get('end_line')} "
                        f"{entity.get('signature') or entity.get('name', '')} "
                        f"[{entity.get('repair_role') or entity.get('verilog_kind') or 'unknown'}]"
                    )
        issue_mentioned_files = []
        if language == "verilog":
            issue_mentioned_files = self._extract_verilog_issue_file_hints(
                problem_statement,
                repo_path=repo_path or "",
                related=related,
            )
            if issue_mentioned_files:
                localized_summary.append(f"Issue-mentioned files: {', '.join(issue_mentioned_files[:8])}")
        if evidence_lines:
            localized_summary.append("Evidence entities:")
            localized_summary.extend(evidence_lines)
        if files:
            localized_summary.append(f"Candidate files: {', '.join(files[:8])}")
        localization_summary = "\n".join(localized_summary)
        bug_kg_digest = {}
        if language == "verilog":
            bug_kg_digest = build_verilog_fault_neighborhood(
                related,
                issue_text=problem_statement,
                analysis=None,
                repo_root=repo_path or "",
            )

        fault_anchor_entities = bug_kg_digest.get("fault_anchor_entities") or []
        repair_anchor_entities = bug_kg_digest.get("repair_anchor_entities") or []
        fault_anchor_spans = bug_kg_digest.get("fault_anchor_spans") or []

        grouped_entities = self._truncate_entity_groups(self._group_entities_for_prompt(related, language=language), language=language)
        if language == "verilog" and (fault_anchor_entities or repair_anchor_entities or fault_anchor_spans):
            prompt_related = dict(related)
            prompt_related["fault_anchor_entities"] = fault_anchor_entities
            prompt_related["repair_anchor_entities"] = repair_anchor_entities
            prompt_related["edit_targets"] = prompt_related.get("edit_targets") or prompt_related.get("direct_drivers") or []
            grouped_entities = self._truncate_entity_groups(self._group_entities_for_prompt(prompt_related, language=language), language=language)
        header_map = {
            "fault_anchor_entities": "Fault Anchor Entities",
            "repair_anchor_entities": "Repair Anchor Entities",
            "methods": "Relevant Methods",
            "classes": "Relevant Classes",
            "edit_targets": "Editable RTL Targets",
            "direct_drivers": "Direct Drivers",
            "top_level_wiring": "Top-Level Wiring",
            "config_registers": "Config Registers",
            "edit_targets": "Editable RTL Targets",
            "rtl_entities": "Legacy RTL Entities",
            "evidence_entities": "RTL Evidence Entities",
            "issues": "Related Issues",
        }
        current_edit_targets = self._render_grouped_entities(
            [(name, entities) for name, entities in grouped_entities if name in {"fault_anchor_entities", "repair_anchor_entities", "edit_targets"}],
            header_map,
            show_path_groups={"fault_anchor_entities", "repair_anchor_entities", "edit_targets"},
        )
        if not current_edit_targets:
            current_edit_targets = self._render_grouped_entities(
                [(name, entities) for name, entities in grouped_entities if name in {"methods"}],
                header_map,
                show_path_groups={"methods"},
            )
        evidence_entities_text = self._render_grouped_entities(
            [(name, entities) for name, entities in grouped_entities if name in {"evidence_entities", "rtl_entities"}],
            header_map,
            show_path_groups={"evidence_entities", "rtl_entities"},
        )
        candidate_source_context = self._collect_candidate_source_context(repo_path, related, language=language)
        if language == "verilog" and (fault_anchor_entities or repair_anchor_entities or fault_anchor_spans):
            candidate_source_context = self._collect_candidate_source_context(
                repo_path,
                related,
                language=language,
                fault_anchor_entities=fault_anchor_entities,
                repair_anchor_entities=repair_anchor_entities,
                fault_anchor_spans=fault_anchor_spans,
                preferred_files=issue_mentioned_files,
            ) or candidate_source_context

        tests = []
        targeted = verification_cfg.get("targeted") or {}
        if targeted.get("name"):
            tests.append(targeted["name"])
        for section_name in ("regression", "coverage"):
            section = verification_cfg.get(section_name) or []
            if isinstance(section, dict):
                section = [section]
            for entry in section:
                if entry.get("name"):
                    tests.append(entry["name"])
        tests = _unique_keep_order(tests)

        hard_constraints = [
            "Only edit the cloned worktree under workdirs/.",
            "Never modify verilog_repair_cases/.",
            "Keep the change scope minimal and explain the failing RTL behavior directly.",
            "Preserve smoke-test behavior and avoid unrelated control-flow changes.",
        ]

        return {
            "problem_statement": problem_statement,
            "localization_summary": localization_summary,
            "current_edit_targets": current_edit_targets,
            "evidence_entities": evidence_entities_text,
            "candidate_source_context": candidate_source_context,
            "fault_anchor_entities": fault_anchor_entities,
            "repair_anchor_entities": repair_anchor_entities,
            "fault_anchor_spans": fault_anchor_spans,
            "files": files,
            "issue_mentioned_files": issue_mentioned_files,
            "tests": tests,
            "edit_target_kinds": edit_target_kinds,
            "evidence_kinds": evidence_kinds,
            "hard_constraints": hard_constraints,
            "related_entities": related,
            "bug_kg_digest": bug_kg_digest,
            "bug_kg_digest_text": bug_kg_digest.get("text", "") if bug_kg_digest else "",
        }

    def _collect_retrieval_memory(self, instance_id, prompt_context, failure_signature="", benchmark_name="verilog-local"):
        return self.experience_store.retrieve(
            instance_id=instance_id,
            benchmark_name=benchmark_name,
            failure_signature=failure_signature,
            files=prompt_context.get("files") or [],
            tests=prompt_context.get("tests") or [],
            edited_files=prompt_context.get("edited_files") or [],
            text="\n".join(
                [
                    prompt_context.get("problem_statement", ""),
                    prompt_context.get("localization_summary", ""),
                    prompt_context.get("current_edit_targets", ""),
                    prompt_context.get("evidence_entities", ""),
                ]
            ),
            limit=4,
        )

    def _build_validation_note(self, validation_report):
        if validation_report is None or not getattr(validation_report, "steps", None):
            return ""
        for step in validation_report.steps:
            if step.kind != "targeted":
                continue
            lines = [
                f"targeted_step={step.name}",
                f"targeted_expected={step.expected_outcome}",
                f"targeted_actual={step.actual_outcome}",
            ]
            if step.run_stdout_excerpt:
                lines.append(f"targeted_run_stdout={step.run_stdout_excerpt}")
            if step.run_stderr_excerpt:
                lines.append(f"targeted_run_stderr={step.run_stderr_excerpt}")
            return "\n".join(lines)
        return ""

    def _score_attempt(self, validation_report, analysis, patch_result, skip_validation=False):
        compile_score = 0
        regression_score = 0
        targeted_score = 0
        timing_score = 0
        validation_bonus = 0
        patch_bonus = 1 if patch_result and patch_result.applied else 0

        if validation_report is not None:
            steps = list(getattr(validation_report, "steps", None) or [])
            compile_steps = [step for step in steps if step.kind == "compile"]
            regression_steps = [step for step in steps if step.kind == "regression"]
            targeted_steps = [step for step in steps if step.kind == "targeted"]

            compile_score = 2 if compile_steps and all(step.passed for step in compile_steps) else 0
            regression_score = 2 if regression_steps and all(step.passed for step in regression_steps) else 0
            if targeted_steps:
                targeted = targeted_steps[0]
                if targeted.passed:
                    targeted_score = 3
                elif getattr(analysis, "timing_sensitive", False):
                    targeted_score = 2 if (getattr(analysis, "timing_signature", "") or getattr(analysis, "observed_signal", "") or getattr(analysis, "failure_line_number", 0)) else 1
                else:
                    targeted_score = 1 if targeted.actual_outcome in {"run_failed", "unexpected_pass", "fail"} else 0
                if getattr(analysis, "timing_sensitive", False):
                    timing_score = 2 if (getattr(analysis, "timing_signature", "") and (getattr(analysis, "observed_signal", "") or getattr(analysis, "expected_idle", ""))) else 1
            if validation_report.passed:
                validation_bonus = 5
        elif skip_validation and patch_bonus:
            validation_bonus = 1

        warning_score = 0
        if validation_report is not None:
            warnings = []
            for step in getattr(validation_report, "steps", None) or []:
                warnings.extend([line for line in [step.compile_stderr_excerpt, step.compile_stdout_excerpt] if line])
            if warnings and any(token in "\n".join(warnings).lower() for token in ("warning", "mismatch", "width", "padding")):
                warning_score = -1 if not getattr(validation_report, "passed", False) else 0

        total = compile_score + regression_score + targeted_score + timing_score + validation_bonus + patch_bonus + warning_score
        return {
            "compile": compile_score,
            "regression": regression_score,
            "targeted_progress": targeted_score,
            "timing_consistency": timing_score,
            "validation_bonus": validation_bonus,
            "patch_bonus": patch_bonus,
            "warning_penalty": warning_score,
            "total": total,
        }

    def _build_attempt_record(
        self,
        *,
        attempt_index,
        analysis,
        patch_result,
        raw_patch_path,
        candidate_diff_path,
        validation_report,
        validation_report_path,
        prompt_context,
        prompt_mode,
        skip_validation,
    ):
        record = {
            "attempt_index": attempt_index,
            "status": "validated" if (validation_report and validation_report.passed) or skip_validation else analysis.signature,
            "failure_signature": analysis.signature,
            "base_signature": analysis.base_signature,
            "prompt_mode": prompt_mode,
            "failure_step": analysis.failure_step,
            "failure_kind": analysis.failure_kind,
            "failure_reason": analysis.failure_reason,
            "summary": analysis.summary,
            "recommendation": analysis.recommendation,
            "timing_signature": analysis.timing_signature,
            "timing_sensitive": analysis.timing_sensitive,
            "timing_hints": analysis.timing_hints,
            "failure_line_number": analysis.failure_line_number,
            "observed_signal": analysis.observed_signal,
            "expected_idle": analysis.expected_idle,
            "stimulus_window": analysis.stimulus_window,
            "repeated": analysis.repeated,
            "repetition_count": analysis.repetition_count,
            "notes": analysis.notes,
            "evidence": analysis.evidence,
            "targeted_excerpt": analysis.targeted_excerpt,
            "candidate_patch_excerpt": analysis.candidate_patch_excerpt,
            "candidate_patch_path": raw_patch_path,
            "diff_patch_path": candidate_diff_path,
            "patch_hash": analysis.patch_hash,
            "diff_hash": analysis.diff_hash,
            "validation_report_path": validation_report_path,
            "validation_passed": bool(validation_report.passed) if validation_report else bool(skip_validation),
            "files": prompt_context["files"],
            "tests": prompt_context["tests"],
            "edit_target_kinds": prompt_context["edit_target_kinds"],
            "evidence_kinds": prompt_context["evidence_kinds"],
            "edited_files": patch_result.edited_files,
            "patch_application": patch_result.to_dict(),
        }
        return record

    def post_process_and_apply_patch(self, instance_id, raw_output_path, locations_dir, playground_dir=None, repo_identifier=None, repo_path=None, language="python"):
        if repo_path is None:
            repo_path = self._resolve_repo_path(playground_dir, repo_identifier, default_root=os.path.dirname(locations_dir))
        repo_path = os.path.abspath(repo_path)

        if not os.path.isdir(repo_path):
            return PatchApplicationResult(
                applied=False,
                status="repo_missing",
                reason=f"Repository path not found at {repo_path}.",
                raw_patch_path=raw_output_path,
                raw_patch_excerpt="",
            )

        with open(raw_output_path, "r", encoding="utf-8") as f:
            raw_output_text = f.read()

        raw_excerpt = _compact_text(raw_output_text, 2000)

        blocks = self._extract_edit_blocks(raw_output_text, language)
        file_to_commands = split_edit_multifile_commands(blocks)

        if not file_to_commands:
            return PatchApplicationResult(
                applied=False,
                status="no_edit_commands",
                reason="No edit commands found in LLM output.",
                raw_patch_path=raw_output_path,
                raw_patch_excerpt=raw_excerpt,
                notes=["no_edit_commands"],
            )

        applied_any = False
        failed_any = False
        edited_files = []
        diff_paths = []
        notes = []
        for file_path_str, edit_commands in file_to_commands.items():
            try:
                edited_file = ast.literal_eval(file_path_str)
            except Exception:
                notes.append(f"Could not parse file path: {file_path_str}")
                failed_any = True
                continue

            if edited_file.startswith(".."):
                edited_file = "/".join(edited_file.split("/")[2:])

            full_file_path = os.path.join(repo_path, edited_file)
            if not os.path.exists(full_file_path):
                notes.append(f"File to be edited not found: {full_file_path}")
                failed_any = True
                continue

            with open(full_file_path, "r", encoding="utf-8") as f:
                original_content = f.read()

            require_line_range = language == "verilog"
            missing_line_ranges = [
                cmd for cmd in edit_commands
                if cmd.get("start_line") is None or cmd.get("end_line") is None
            ]
            if require_line_range and missing_line_ranges:
                notes.append(f"Missing start_line/end_line metadata for {edited_file}.")
                failed_any = True
                continue

            new_content = parse_diff_edit_commands_strict(
                edit_commands,
                original_content,
                require_line_range=require_line_range,
            )

            if new_content == original_content or not self._candidate_is_valid(new_content, language):
                if not require_line_range:
                    for indent_change in [-4, 4, -8, 8]:
                        adjusted_commands = [self.adjust_command_indentation(cmd, indent_change) for cmd in edit_commands]
                        adjusted_content = parse_diff_edit_commands_strict(adjusted_commands, original_content)
                        if adjusted_content != original_content and self._candidate_is_valid(adjusted_content, language):
                            new_content = adjusted_content
                            break

            if new_content == original_content or not self._candidate_is_valid(new_content, language):
                if not require_line_range:
                    new_content = parse_diff_edit_commands_strict(edit_commands, original_content, only_one_replace=True)
                    if new_content == original_content or not self._candidate_is_valid(new_content, language):
                        for indent_change in [-4, 4, -8, 8]:
                            adjusted_commands = [self.adjust_command_indentation(cmd, indent_change) for cmd in edit_commands]
                            adjusted_content = parse_diff_edit_commands_strict(adjusted_commands, original_content, only_one_replace=True)
                            if adjusted_content != original_content and self._candidate_is_valid(adjusted_content, language):
                                new_content = adjusted_content
                                break

            if new_content == original_content or not self._candidate_is_valid(new_content, language):
                if require_line_range:
                    notes.append(f"Failed to apply exact line-range patch for {edited_file}.")
                else:
                    notes.append(f"Failed to generate a valid patch for {edited_file}.")
                failed_any = True
                continue

            with open(full_file_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            diff = difflib.unified_diff(
                original_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{edited_file}",
                tofile=f"b/{edited_file}",
            )
            patch_content = "".join(diff)

            diff_patch_dir = os.path.join(os.path.dirname(raw_output_path), "diff_patches")
            os.makedirs(diff_patch_dir, exist_ok=True)
            sanitized_file_path = edited_file.replace("/", "_")
            diff_file_path = os.path.join(diff_patch_dir, f"{instance_id}_{sanitized_file_path}.diff")
            abs_diff_file_path = os.path.abspath(diff_file_path)
            with open(abs_diff_file_path, "w", encoding="utf-8") as f:
                f.write(patch_content)

            notes.append(f"Applied patch for {edited_file}.")

            applied_any = True
            edited_files.append(edited_file)
            diff_paths.append(abs_diff_file_path)

        if applied_any and not failed_any:
            return PatchApplicationResult(
                applied=True,
                status="applied",
                reason="patch applied",
                edited_files=_unique_keep_order(edited_files),
                diff_paths=_unique_keep_order(diff_paths),
                notes=_unique_keep_order(notes),
                raw_patch_path=raw_output_path,
                raw_patch_excerpt=raw_excerpt,
            )

        status = "patch_apply_failed" if failed_any else "no_changes"
        reason = "; ".join(notes) if notes else "Failed to generate a valid patch."
        return PatchApplicationResult(
            applied=False,
            status=status,
            reason=reason,
            edited_files=_unique_keep_order(edited_files),
            diff_paths=_unique_keep_order(diff_paths),
            notes=_unique_keep_order(notes),
            raw_patch_path=raw_output_path,
            raw_patch_excerpt=raw_excerpt,
        )

    def process_instance(self, instance_id, locations_dir, output_dir, playground_dir=None, repo_identifier=None):
        _log("start", f"instance={instance_id}")

        location_file = os.path.join(locations_dir, f"{instance_id}.json")
        if not os.path.exists(location_file):
            _log("error", f"location file not found: {location_file}")
            return

        with open(location_file, "r", encoding="utf-8") as f:
            locate_result = json.load(f)
        language = str(locate_result.get("language", "python")).lower()

        benchmark_item = self._load_benchmark_item(instance_id)
        verification_cfg = (benchmark_item or {}).get("verification", {})
        configured_max_attempts = int(verification_cfg.get("max_attempts", int(os.getenv("VERILOG_REPAIR_MAX_ATTEMPTS", "8"))))
        generation_max_attempts = int(os.getenv("VERILOG_GENERATION_MAX_ATTEMPTS", "3")) if language == "verilog" else configured_max_attempts
        debug_max_attempts = int(os.getenv("VERILOG_DEBUG_MAX_ATTEMPTS", "3")) if language == "verilog" else 0
        max_attempts = generation_max_attempts + debug_max_attempts if language == "verilog" else configured_max_attempts
        skip_validation = os.getenv("VERILOG_ALLOW_UNVALIDATED", "0") == "1"
        validation_runner = VerilogValidationRunner(verification_cfg)
        base_repo_path = self._resolve_repo_path(
            playground_dir,
            repo_identifier,
            default_root=os.path.join(os.path.dirname(locations_dir), "playground"),
        )

        prompt_context = self._collect_prompt_context(locate_result, verification_cfg, language, repo_path=base_repo_path)
        problem_statement = prompt_context["problem_statement"].replace("\r", "")
        localization_summary = prompt_context["localization_summary"].replace("\r", "")
        current_edit_targets = prompt_context["current_edit_targets"] or "No editable RTL targets found."
        evidence_entities = prompt_context["evidence_entities"] or "No evidence entities found."
        candidate_source_context = prompt_context.get("candidate_source_context", "")
        debug_kg_context = ""
        hard_constraints = prompt_context["hard_constraints"]
        language_prompt_parts = self._language_prompt_parts(language)
        benchmark_name = str((benchmark_item or {}).get("benchmark_name") or "verilog-local")
        retrieval_memory = self._collect_retrieval_memory(
            instance_id=instance_id,
            prompt_context=prompt_context,
            failure_signature="",
            benchmark_name=benchmark_name,
        )

        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)
        output_dir = os.path.abspath(output_dir)
        run_root = os.path.abspath(os.path.dirname(output_dir))
        attempts_root = os.path.join(run_root, "validation_attempts")
        os.makedirs(attempts_root, exist_ok=True)

        baseline_report = None
        baseline_note = ""
        if verification_cfg and not skip_validation:
            _log("baseline", f"running baseline validation on {base_repo_path}")
            baseline_artifact_root = os.path.join(run_root, "validation_baseline")
            baseline_report = validation_runner.run_suite(base_repo_path, baseline_artifact_root, mode="baseline")
            self._write_json(os.path.join(baseline_artifact_root, "validation_report.json"), baseline_report.to_dict())
            baseline_ok = bool(baseline_report.passed)
            if not baseline_ok:
                self._write_json(
                    os.path.join(run_root, "validation_summary.json"),
                    {
                        "baseline": baseline_report.to_dict(),
                        "attempts": [],
                        "max_attempts": max_attempts,
                        "generation_max_attempts": generation_max_attempts,
                        "debug_max_attempts": debug_max_attempts,
                    },
                )
                self._write_json(
                    os.path.join(run_root, "repair_summary.json"),
                    {
                        "instance_id": instance_id,
                        "validated": False,
                        "reason": "baseline_validation_failed",
                        "baseline_report": baseline_report.to_dict(),
                    },
                )
                _log("baseline", "baseline validation failed; aborting repair loop")
                return
            baseline_note = self._build_validation_note(baseline_report)
            _log("baseline", "baseline validation reproduced expected behavior")

        prompt = self.prompt_builder.build_generation_prompt(
            problem_statement=problem_statement,
            candidate_source_context=candidate_source_context,
            hard_constraints=hard_constraints,
            language_prompt_parts=language_prompt_parts,
            attempt_index=1,
            max_attempts=generation_max_attempts,
            analysis=None,
            baseline_note=baseline_note,
        )

        attempt_history = []
        validation_summary = {
            "attempts": [],
            "max_attempts": max_attempts,
            "generation_max_attempts": generation_max_attempts,
            "debug_max_attempts": debug_max_attempts,
        }
        best_attempt = None
        best_attempt_score = -1
        phase = "generation"
        generation_attempt_index = 1
        debug_attempt_index = 0
        current_analysis = None
        debug_source_repo_path = base_repo_path

        for attempt_index in range(1, max_attempts + 1):
            if language == "verilog" and phase == "generation" and generation_attempt_index > generation_max_attempts:
                _log("generation", "attempt limit reached without applicable patch")
                break
            if language == "verilog" and phase == "debug" and debug_attempt_index >= debug_max_attempts:
                _log("debug", "attempt limit reached")
                break
            phase_attempt = generation_attempt_index if phase == "generation" else debug_attempt_index + 1
            phase_limit = generation_max_attempts if phase == "generation" else debug_max_attempts
            _log("attempt", f"phase={phase} phase_attempt={phase_attempt}/{phase_limit} total_attempt={attempt_index}/{max_attempts}")
            clone_source_repo_path = debug_source_repo_path if phase == "debug" else base_repo_path
            candidate_repo_workdir, candidate_repo_path = self._clone_repo_to_attempt_root(clone_source_repo_path, attempts_root)
            _log("workspace", f"candidate_repo={candidate_repo_path}")
            if language == "verilog" and phase == "debug":
                prompt = self._build_debug_patch_prompt(
                    problem_statement=problem_statement,
                    candidate_source_context=candidate_source_context,
                    hard_constraints=hard_constraints,
                    language_prompt_parts=language_prompt_parts,
                    attempt_index=phase_attempt,
                    max_attempts=debug_max_attempts,
                    analysis=current_analysis,
                    prior_attempts=attempt_history,
                    baseline_note=baseline_note,
                    candidate_repo_path=candidate_repo_path,
                    language=language,
                )
            stream = self.get_completion(prompt, stream=True)
            if not stream:
                _log("llm", "failed to get a valid response from model")
                if candidate_repo_workdir and os.path.isdir(candidate_repo_workdir):
                    shutil.rmtree(candidate_repo_workdir, ignore_errors=True)
                validation_summary["attempts"].append(
                    {
                        "attempt": attempt_index,
                        "phase": phase,
                        "status": "llm_failed",
                        "prompt_mode": "llm_failed",
                    }
                )
                if phase == "generation":
                    generation_attempt_index += 1
                    prompt = self.prompt_builder.build_generation_prompt(
                        problem_statement=problem_statement,
                        candidate_source_context=candidate_source_context,
                        hard_constraints=hard_constraints,
                        language_prompt_parts=language_prompt_parts,
                        attempt_index=generation_attempt_index,
                        max_attempts=generation_max_attempts,
                        analysis=None,
                        baseline_note=baseline_note,
                    )
                continue

            attempt_dir = os.path.join(attempts_root, f"attempt_{attempt_index:02d}")
            os.makedirs(attempt_dir, exist_ok=True)
            raw_patch_path = os.path.join(attempt_dir, f"{instance_id}.patch")
            _log("llm", f"writing raw patch output to {raw_patch_path}")
            final_chunks = []
            if isinstance(stream, str):
                final_text = _final_response_content(stream)
            else:
                raw_chunks = []
                for chunk in stream:
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None) or ""
                    reasoning_content = getattr(delta, "reasoning_content", None) or ""
                    if reasoning_content and not content:
                        continue
                    raw_chunks.append(content)
                final_text = _final_response_content("".join(raw_chunks))
            with open(raw_patch_path, "w", encoding="utf-8") as f:
                f.write(final_text)
            final_chunks.append(final_text)
            _log("llm", f"received_patch_chars={len(final_text)}")
            preview = _compact_text(final_text, 900)
            _log("llm-preview", preview.replace("\n", "\\n"))
            print("----- patch preview begin -----")
            print(preview)
            print("----- patch preview end -----")

            candidate_diff_path = ""
            validation_report = None
            validation_report_path = ""
            validated = False
            patch_result = PatchApplicationResult(
                applied=False,
                status="uninitialized",
                reason="not processed",
                raw_patch_path=raw_patch_path,
                raw_patch_excerpt="",
            )

            try:
                patch_result = self.post_process_and_apply_patch(
                    instance_id,
                    raw_patch_path,
                    locations_dir,
                    repo_path=candidate_repo_path,
                    language=language,
                )
                _log("patch", f"status={patch_result.status} applied={patch_result.applied} files={patch_result.edited_files}")
                if patch_result.reason:
                    _log("patch", f"reason={_compact_text(patch_result.reason, 300)}")

                if not patch_result.applied:
                    analysis = self.failure_analyzer.analyze(
                        None,
                        previous_attempts=attempt_history,
                        patch_result=patch_result,
                        attempt_index=attempt_index,
                        candidate_patch_path=raw_patch_path,
                        patch_hash=_hash_path(raw_patch_path),
                        diff_hash="",
                        validation_report_path="",
                        change_files=patch_result.edited_files,
                    )
                    record = self._build_attempt_record(
                        attempt_index=attempt_index,
                        analysis=analysis,
                        patch_result=patch_result,
                        raw_patch_path=raw_patch_path,
                        candidate_diff_path="",
                        validation_report=None,
                        validation_report_path="",
                        prompt_context=prompt_context,
                        prompt_mode=analysis.prompt_mode,
                        skip_validation=False,
                    )
                    attempt_history.append(
                        {
                            "attempt": attempt_index,
                            "status": record["status"],
                            "failure_signature": record["failure_signature"],
                            "base_signature": record["base_signature"],
                            "timing_signature": record["timing_signature"],
                            "prompt_mode": record["prompt_mode"],
                            "failure_step": record["failure_step"],
                            "summary": record["summary"],
                            "recommendation": record["recommendation"],
                            "files": record["files"],
                            "tests": record["tests"],
                            "edited_files": record["edited_files"],
                            "evidence": record["evidence"],
                            "repetition_count": record["repetition_count"],
                        }
                    )
                    self.experience_store.record_attempt(
                        instance_id=instance_id,
                        benchmark_name=benchmark_name,
                        repo_identifier=repo_identifier or instance_id,
                        attempt_record=record,
                    )
                    retrieval_memory = self._collect_retrieval_memory(
                        instance_id=instance_id,
                        prompt_context=prompt_context,
                        failure_signature=record["failure_signature"],
                        benchmark_name=benchmark_name,
                    )
                    validation_summary["attempts"].append(
                        {
                            "attempt": attempt_index,
                            "phase": phase,
                            "status": record["status"],
                            "raw_patch": raw_patch_path,
                            "analysis": analysis.to_dict(),
                            "patch_application": patch_result.to_dict(),
                        }
                    )
                    if phase == "generation":
                        generation_attempt_index += 1
                        if generation_attempt_index <= generation_max_attempts:
                            prompt = self.prompt_builder.build_generation_prompt(
                                problem_statement=problem_statement,
                                candidate_source_context=candidate_source_context,
                                hard_constraints=hard_constraints,
                                language_prompt_parts=language_prompt_parts,
                                attempt_index=generation_attempt_index,
                                max_attempts=generation_max_attempts,
                                analysis=analysis,
                                baseline_note=baseline_note,
                            )
                    else:
                        debug_attempt_index += 1
                        current_analysis = analysis
                    continue

                diff_result = subprocess.run(
                    ["git", "-C", candidate_repo_path, "diff", "--no-color"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                candidate_diff_path = os.path.join(attempt_dir, f"{instance_id}.diff")
                self._write_json(
                    os.path.join(attempt_dir, "diff_manifest.json"),
                    {
                        "returncode": diff_result.returncode,
                        "diff_path": candidate_diff_path,
                        "has_diff": bool(diff_result.stdout.strip()),
                    },
                )
                with open(candidate_diff_path, "w", encoding="utf-8") as f:
                    f.write(diff_result.stdout or "")

                if not skip_validation:
                    _log("validation", f"running candidate validation for attempt_{attempt_index:02d}")
                    validation_artifact_root = os.path.join(attempt_dir, "validation")
                    validation_report = validation_runner.run_suite(candidate_repo_path, validation_artifact_root, mode="candidate")
                    validation_report_path = os.path.join(validation_artifact_root, "validation_report.json")
                    self._write_json(validation_report_path, validation_report.to_dict())
                else:
                    validation_report = None
                    validation_report_path = ""

                validated = True if skip_validation else bool(validation_report and validation_report.passed)
                analysis = self.failure_analyzer.analyze(
                    validation_report,
                    previous_attempts=attempt_history,
                    patch_result=None,
                    attempt_index=attempt_index,
                    candidate_patch_path=raw_patch_path,
                    patch_hash=_hash_path(raw_patch_path),
                    diff_hash=_hash_path(candidate_diff_path),
                    validation_report_path=validation_report_path,
                    change_files=patch_result.edited_files,
                )

                score_breakdown = self._score_attempt(validation_report, analysis, patch_result, skip_validation=skip_validation)
                score = score_breakdown["total"]
                _log(
                    "validation",
                    f"passed={validated} signature={analysis.signature} failure_step={analysis.failure_step or 'none'} score={score}",
                )
                if analysis.summary:
                    _log("validation", f"summary={_compact_text(analysis.summary, 300)}")
                if score > best_attempt_score:
                    best_attempt_score = score
                    best_attempt = {
                        "attempt": attempt_index,
                        "raw_patch_path": raw_patch_path,
                        "diff_patch_path": candidate_diff_path,
                        "repo_path": candidate_repo_path,
                        "validation_report": validation_report.to_dict() if validation_report else None,
                        "analysis": analysis.to_dict(),
                        "patch_application": patch_result.to_dict(),
                        "score_breakdown": score_breakdown,
                    }

                record = self._build_attempt_record(
                    attempt_index=attempt_index,
                    analysis=analysis,
                    patch_result=patch_result,
                    raw_patch_path=raw_patch_path,
                    candidate_diff_path=candidate_diff_path,
                    validation_report=validation_report,
                    validation_report_path=validation_report_path,
                    prompt_context=prompt_context,
                    prompt_mode="validated" if validated else analysis.prompt_mode,
                    skip_validation=skip_validation,
                )
                attempt_history.append(
                    {
                        "attempt": attempt_index,
                        "status": record["status"],
                        "failure_signature": record["failure_signature"],
                        "base_signature": record["base_signature"],
                        "timing_signature": record["timing_signature"],
                        "prompt_mode": record["prompt_mode"],
                        "failure_step": record["failure_step"],
                        "summary": record["summary"],
                        "recommendation": record["recommendation"],
                        "score_breakdown": score_breakdown,
                        "files": record["files"],
                        "tests": record["tests"],
                        "edited_files": record["edited_files"],
                        "evidence": record["evidence"],
                        "repetition_count": record["repetition_count"],
                    }
                )

                validation_summary["attempts"].append(
                    {
                        "attempt": attempt_index,
                        "phase": phase,
                        "status": "validated" if validated else "validation_failed",
                        "raw_patch": raw_patch_path,
                        "diff_patch": candidate_diff_path,
                        "validation_report": validation_report.to_dict() if validation_report else None,
                        "analysis": analysis.to_dict(),
                        "patch_application": patch_result.to_dict(),
                        "score_breakdown": score_breakdown,
                    }
                )

                retrieval_memory = self._collect_retrieval_memory(
                    instance_id=instance_id,
                    prompt_context=prompt_context,
                    failure_signature=analysis.signature,
                    benchmark_name=benchmark_name,
                )
                if validated:
                    final_patch_path = os.path.join(output_dir, f"{instance_id}.patch")
                    final_diff_path = os.path.join(output_dir, f"{instance_id}.diff")
                    shutil.copyfile(raw_patch_path, final_patch_path)
                    shutil.copyfile(candidate_diff_path, final_diff_path)
                    self._write_json(
                        os.path.join(run_root, "validation_report.json"),
                        validation_report.to_dict() if validation_report else {"available": False},
                    )
                    self._write_json(
                        os.path.join(run_root, "repair_summary.json"),
                        {
                            "instance_id": instance_id,
                            "validated": True,
                            "attempt": attempt_index,
                            "final_patch": final_patch_path,
                            "final_diff": final_diff_path,
                            "baseline_report": baseline_report.to_dict() if baseline_report else None,
                        },
                    )
                    _log("success", f"validated_patch={final_patch_path}")
                    _log("success", f"validated_diff={final_diff_path}")
                    return

                completed_phase = phase
                phase = "debug"
                current_analysis = analysis
                debug_source_repo_path = candidate_repo_path
                if completed_phase == "debug":
                    debug_attempt_index += 1
                _log("debug", f"next round will use validation feedback: {analysis.signature}")
            finally:
                keep_for_debug = (
                    language == "verilog"
                    and phase == "debug"
                    and not validated
                    and candidate_repo_path == debug_source_repo_path
                )
                if candidate_repo_workdir and os.path.isdir(candidate_repo_workdir) and not keep_for_debug:
                    shutil.rmtree(candidate_repo_workdir, ignore_errors=True)

        self._write_json(
            os.path.join(run_root, "validation_summary.json"),
            {
                **validation_summary,
                "attempt_history": attempt_history,
            },
        )
        self._write_json(
            os.path.join(run_root, "repair_summary.json"),
            {
                "instance_id": instance_id,
                "validated": False,
                "best_attempt": best_attempt,
                "validation_summary": validation_summary,
                "attempt_history": attempt_history,
            },
        )
        if skip_validation:
            _log("finish", "validation disabled by VERILOG_ALLOW_UNVALIDATED=1")
        else:
            _log("finish", "all repair attempts failed validation")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Code Repair Script")
    parser.add_argument("final_locations_dir", type=str, help="Directory containing the final location files.")
    parser.add_argument("--instance_id", required=True, type=str, help="The specific instance ID to process.")
    parser.add_argument(
        "--playground_dir",
        type=str,
        default=None,
        help="Root directory where repositories are located (default: sibling 'playground' of final_locations_dir).",
    )
    parser.add_argument(
        "--repo_identifier",
        type=str,
        default=None,
        help="Repository directory name inside playground (e.g., 'astropy__astropy'). If omitted, it will be derived from instance_id.",
    )

    args = parser.parse_args()
    patch_dir = os.path.join(os.path.dirname(args.final_locations_dir), "patches")
    repairer = CodeRepair()
    repairer.process_instance(
        instance_id=args.instance_id,
        locations_dir=args.final_locations_dir,
        output_dir=patch_dir,
        playground_dir=args.playground_dir,
        repo_identifier=args.repo_identifier,
    )
