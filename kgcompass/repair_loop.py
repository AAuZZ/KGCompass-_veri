from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:
    from verilog_timing import classify_timing_failure_excerpt
except Exception:  # pragma: no cover - package-relative fallback.
    from .verilog_timing import classify_timing_failure_excerpt


def _dedupe_keep_order(items: Iterable[Any]) -> List[Any]:
    seen = set()
    ordered = []
    for item in items:
        marker = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
        if marker in seen:
            continue
        ordered.append(item)
        seen.add(marker)
    return ordered


def _compact_text(text: str, limit: int = 1200) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]..."


def _string_list(items: Iterable[Any], limit: int = 8) -> str:
    values = [str(item) for item in items if str(item).strip()]
    if not values:
        return ""
    if len(values) > limit:
        values = values[:limit] + ["..."]
    return ", ".join(values)


def _extract_first_step_excerpt(validation_result: Any) -> str:
    steps = getattr(validation_result, "steps", None) or []
    for step in steps:
        if getattr(step, "passed", True):
            continue
        parts = [
            getattr(step, "compile_stdout_excerpt", "") or "",
            getattr(step, "compile_stderr_excerpt", "") or "",
            getattr(step, "run_stdout_excerpt", "") or "",
            getattr(step, "run_stderr_excerpt", "") or "",
        ]
        text = "\n".join(part for part in parts if part)
        return _compact_text(text, 900)
    return ""


def _extract_step_source_excerpt(step: Any, limit: int = 14) -> str:
    run_text = "\n".join(
        part for part in [
            getattr(step, "run_stdout_excerpt", "") or "",
            getattr(step, "run_stderr_excerpt", "") or "",
            getattr(step, "compile_stderr_excerpt", "") or "",
        ]
        if part
    )
    failing_line = None
    line_match = re.search(r"([A-Za-z0-9_./\\-]+\.(?:sv|v|svh|vh)):(\d+)", run_text)
    if line_match:
        failing_line = int(line_match.group(2))

    source_path = ""
    for item in reversed(getattr(step, "compile_command", []) or []):
        if not isinstance(item, str):
            continue
        if "kgcompass_vcd_wrapper_" in item:
            continue
        if re.search(r"\.(?:sv|v|svh|vh)$", item, re.IGNORECASE):
            source_path = item
            break

    if not source_path or not os.path.exists(source_path):
        return ""

    try:
        with open(source_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""

    if failing_line is None:
        start = 1
        end = min(len(lines), 18)
    else:
        start = max(1, failing_line - 6)
        end = min(len(lines), failing_line + 6)

    excerpt = []
    for line_no in range(start, end + 1):
        excerpt.append(f"{line_no}: {lines[line_no - 1]}")
    return f"{os.path.basename(source_path)}:{start}-{end}\n" + "\n".join(excerpt)


def _step_name(step: Any) -> str:
    return str(getattr(step, "name", "") or "")


def _step_kind(step: Any) -> str:
    return str(getattr(step, "kind", "") or "")


def _step_passed(step: Any) -> bool:
    return bool(getattr(step, "passed", False))


def _step_expected(step: Any) -> str:
    return str(getattr(step, "expected_outcome", "") or "")


def _step_actual(step: Any) -> str:
    return str(getattr(step, "actual_outcome", "") or "")


def _step_summary(step: Any) -> str:
    parts = []
    for attr in ("summary", "run_stdout_excerpt", "compile_stdout_excerpt", "compile_stderr_excerpt", "run_stderr_excerpt"):
        value = str(getattr(step, attr, "") or "").strip()
        if value:
            parts.append(value)
    return _compact_text("\n".join(parts), 900)


def _extract_failure_line_number(text: str) -> int:
    if not text:
        return 0
    match = re.search(r"([A-Za-z0-9_./\\-]+\.(?:sv|v|svh|vh)):(\d+)", text)
    return int(match.group(2)) if match else 0


@dataclass
class PatchApplicationResult:
    applied: bool
    status: str
    reason: str
    edited_files: List[str] = field(default_factory=list)
    diff_paths: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    raw_patch_path: str = ""
    raw_patch_excerpt: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FailureAnalysis:
    signature: str
    prompt_mode: str
    summary: str
    failure_step: str = ""
    failure_kind: str = ""
    failure_reason: str = ""
    failure_expected: str = ""
    failure_actual: str = ""
    base_signature: str = ""
    base_prompt_mode: str = ""
    repeated: bool = False
    repetition_count: int = 0
    recommendation: str = ""
    notes: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    targeted_excerpt: str = ""
    stimulus_window: str = ""
    timing_signature: str = ""
    timing_sensitive: bool = False
    timing_hints: List[str] = field(default_factory=list)
    failure_line_number: int = 0
    observed_signal: str = ""
    expected_idle: str = ""
    candidate_patch_excerpt: str = ""
    change_files: List[str] = field(default_factory=list)
    patch_hash: str = ""
    diff_hash: str = ""
    validation_report_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class FailureAnalyzer:
    def __init__(self) -> None:
        self.prompt_modes = {
            "patch_apply_failed": "format_rescue",
            "compile_failed": "compile_fix",
            "targeted_failed": "targeted_fix",
            "timing_sensitive_targeted_failed": "timing_targeted_fix",
            "regression_failed": "regression_fix",
            "coverage_shortfall": "coverage_fix",
            "repeated_no_progress": "strategy_shift",
            "toolchain_unavailable": "toolchain_unavailable",
            "validated": "done",
        }
        self.recommendations = {
            "patch_apply_failed": "Rewrite the SEARCH/REPLACE blocks with exact file-relative headers, a verbatim SEARCH span, and the smallest possible edit surface. Do not rename ports, signals, or outputs when the issue is inside an RTL block.",
            "compile_failed": "Fix syntax, ports, block structure, or invalid signal usage before broadening the logic change.",
            "targeted_failed": "Repair the smallest RTL region that controls the failing signal, edge, or idle state.",
            "timing_sensitive_targeted_failed": "Repair the path that makes the idle or edge state immediately observable after the write, and prefer the direct driver over upper wiring.",
            "regression_failed": "Preserve the smoke-test path and avoid changing unrelated control flow or defaults.",
            "coverage_shortfall": "Exercise the missing state, edge, or boundary transition that the coverage bins still miss.",
            "repeated_no_progress": "Switch repair surface or hypothesis instead of repeating the same patch shape.",
            "toolchain_unavailable": "Validation is unavailable; only propose compile-safe changes backed by graph evidence.",
            "validated": "No repair needed.",
        }

    def _base_signature_and_mode(self, patch_result: Optional[PatchApplicationResult], validation_result: Any) -> tuple[str, str, str, str, str]:
        if patch_result is not None and not patch_result.applied:
            base_signature = "patch_apply_failed"
            reason = patch_result.reason or "patch_application_failed"
            mode = self.prompt_modes.get(base_signature, "format_rescue")
            return base_signature, base_signature, mode, reason, "patch_application"

        if validation_result is None:
            return "validation_failed", "validation_failed", "strategy_shift", "validation result missing", ""

        if not getattr(validation_result, "available", True):
            return "toolchain_unavailable", "toolchain_unavailable", self.prompt_modes["toolchain_unavailable"], "; ".join(getattr(validation_result, "notes", []) or []), "toolchain"

        if getattr(validation_result, "passed", False):
            return "validated", "validated", self.prompt_modes["validated"], "validation passed", ""

        steps = getattr(validation_result, "steps", None) or []
        failed_step = None
        for step in steps:
            if not _step_passed(step):
                failed_step = step
                break

        if failed_step is None:
            return "validation_failed", "validation_failed", "strategy_shift", getattr(validation_result, "failure_summary", "") or "validation failed", ""

        kind = _step_kind(failed_step)
        expected = _step_expected(failed_step)
        actual = _step_actual(failed_step)
        failure_hint = classify_timing_failure_excerpt(
            _extract_first_step_excerpt(validation_result),
            getattr(failed_step, "run_stdout_excerpt", "") or getattr(failed_step, "run_stderr_excerpt", "") or "",
        )
        if kind == "targeted" and failure_hint.get("timing_sensitive"):
            timing_signature = failure_hint.get("timing_signature") or "timing_sensitive_targeted_failed"
            return timing_signature, "timing_sensitive_targeted_failed", "timing_targeted_fix", _step_summary(failed_step) or "timing-sensitive targeted validation failed", _step_name(failed_step)
        if kind == "compile" or actual == "compile_failed":
            return "compile_failed", "compile_failed", self.prompt_modes["compile_failed"], _step_summary(failed_step) or "compile failed", _step_name(failed_step)
        if kind == "targeted":
            return "targeted_failed", "targeted_failed", self.prompt_modes["targeted_failed"], _step_summary(failed_step) or "targeted validation failed", _step_name(failed_step)
        if kind == "regression":
            return "regression_failed", "regression_failed", self.prompt_modes["regression_failed"], _step_summary(failed_step) or "regression validation failed", _step_name(failed_step)
        if kind == "coverage":
            return "coverage_shortfall", "coverage_shortfall", self.prompt_modes["coverage_shortfall"], _step_summary(failed_step) or "coverage threshold not met", _step_name(failed_step)
        return f"{kind}_failed" if kind else "validation_failed", f"{kind}_failed" if kind else "validation_failed", "strategy_shift", _step_summary(failed_step) or "validation failed", _step_name(failed_step)

    def _consecutive_signature_count(self, signature: str, previous_attempts: Sequence[Dict[str, Any]]) -> int:
        count = 0
        for record in reversed(previous_attempts or []):
            record_signature = str(record.get("failure_signature") or record.get("base_signature") or "")
            if record_signature == signature:
                count += 1
            else:
                break
        return count

    def _timing_signature_for_validation(self, validation_result: Any) -> Dict[str, Any]:
        targeted_step = None
        for step in getattr(validation_result, "steps", None) or []:
            if _step_kind(step) == "targeted" and not _step_passed(step):
                targeted_step = step
                break
        if targeted_step is None:
            return {
                "timing_signature": "",
                "timing_sensitive": False,
                "timing_hints": [],
                "stimulus_window": "",
                "observed_signal": "",
                "expected_idle": "",
            }
        stimulus_window = str(getattr(targeted_step, "stimulus_window", "") or "")
        if not stimulus_window:
            stimulus_window = _extract_step_source_excerpt(targeted_step)
        failure_text = "\n".join(
            part for part in [
                getattr(targeted_step, "run_stdout_excerpt", "") or "",
                getattr(targeted_step, "run_stderr_excerpt", "") or "",
            ]
            if part
        )
        return classify_timing_failure_excerpt(
            stimulus_window,
            failure_text,
        )

    def _build_evidence(self, validation_result: Any, patch_result: Optional[PatchApplicationResult]) -> List[str]:
        evidence: List[str] = []
        if patch_result is not None and not patch_result.applied:
            evidence.extend([note for note in patch_result.notes if note])
            if patch_result.raw_patch_excerpt:
                evidence.append("candidate_patch_excerpt")
                evidence.append(patch_result.raw_patch_excerpt)
            return evidence

        steps = getattr(validation_result, "steps", None) or []
        for step in steps:
            if _step_passed(step):
                continue
            evidence.append(f"step={_step_name(step)}")
            evidence.append(f"kind={_step_kind(step)}")
            evidence.append(f"expected={_step_expected(step)}")
            evidence.append(f"actual={_step_actual(step)}")
            summary = _step_summary(step)
            if summary:
                evidence.append(summary)
            src_excerpt = _extract_step_source_excerpt(step)
            if src_excerpt:
                evidence.append("targeted_testbench_excerpt")
                evidence.append(src_excerpt)
            timing_hint = self._timing_signature_for_validation(validation_result)
            if timing_hint.get("timing_sensitive"):
                evidence.append(f"timing_signature={timing_hint.get('timing_signature')}")
                if timing_hint.get("observed_signal"):
                    evidence.append(f"observed_signal={timing_hint.get('observed_signal')}")
                if timing_hint.get("expected_idle"):
                    evidence.append(f"expected_idle={timing_hint.get('expected_idle')}")
                if timing_hint.get("stimulus_window"):
                    evidence.append("stimulus_window")
                    evidence.append(_compact_text(str(timing_hint.get("stimulus_window")), 700))
            break
        return evidence

    def analyze(
        self,
        validation_result: Any,
        previous_attempts: Sequence[Dict[str, Any]] = (),
        patch_result: Optional[PatchApplicationResult] = None,
        attempt_index: int = 1,
        candidate_patch_path: str = "",
        patch_hash: str = "",
        diff_hash: str = "",
        validation_report_path: str = "",
        change_files: Optional[Sequence[str]] = None,
    ) -> FailureAnalysis:
        base_signature, base_signature_copy, prompt_mode, failure_reason, failure_step = self._base_signature_and_mode(
            patch_result,
            validation_result,
        )
        failure_kind = _step_kind(next((s for s in (getattr(validation_result, "steps", None) or []) if not _step_passed(s)), None)) if validation_result else ""
        failure_expected = ""
        failure_actual = ""
        if validation_result is not None and getattr(validation_result, "steps", None):
            for step in getattr(validation_result, "steps", []):
                if _step_passed(step):
                    continue
                failure_expected = _step_expected(step)
                failure_actual = _step_actual(step)
                failure_kind = _step_kind(step)
                failure_step = _step_name(step)
                break

        timing_hint = self._timing_signature_for_validation(validation_result) if validation_result is not None else {
            "timing_signature": "",
            "timing_sensitive": False,
            "timing_hints": [],
            "stimulus_window": "",
            "observed_signal": "",
            "expected_idle": "",
        }

        evidence = self._build_evidence(validation_result, patch_result)
        summary = failure_reason
        if validation_result is not None and getattr(validation_result, "passed", False):
            summary = "validation passed"
        elif base_signature == "patch_apply_failed":
            summary = f"Patch application failed: {failure_reason}"
        elif validation_result is not None and not getattr(validation_result, "available", True):
            summary = f"Validation unavailable: {failure_reason}"
        elif validation_result is not None:
            summary = failure_reason or getattr(validation_result, "failure_summary", "") or "validation failed"

        repetition_count = self._consecutive_signature_count(base_signature, previous_attempts)
        repeated = repetition_count >= 2 and base_signature not in {"validated", "toolchain_unavailable"}
        signature = base_signature
        if repeated:
            signature = "repeated_no_progress"
            prompt_mode = self.prompt_modes[signature]

        if timing_hint.get("timing_sensitive") and signature == "targeted_failed":
            signature = "timing_sensitive_targeted_failed"
            prompt_mode = self.prompt_modes[signature]

        recommendation = self.recommendations.get(signature, self.recommendations.get(base_signature, "Revise the smallest plausible RTL region."))
        if timing_hint.get("timing_sensitive"):
            recommendation = self.recommendations.get("timing_sensitive_targeted_failed", recommendation)
        repeat_threshold = 1 if timing_hint.get("timing_sensitive") else 2
        repeated = repetition_count >= repeat_threshold and base_signature not in {"validated", "toolchain_unavailable"}
        if repeated:
            signature = "repeated_no_progress"
            prompt_mode = self.prompt_modes[signature]
        notes = []
        if repeated:
            notes.append(f"repeated_base_signature={base_signature}")
            notes.append(f"repetition_count={repetition_count}")
        if timing_hint.get("timing_sensitive"):
            notes.append(f"timing_signature={timing_hint.get('timing_signature')}")
            notes.append(f"observed_signal={timing_hint.get('observed_signal')}")
            notes.append(f"expected_idle={timing_hint.get('expected_idle')}")
        if patch_result is not None and patch_result.notes:
            notes.extend([note for note in patch_result.notes if note])

        candidate_patch_excerpt = ""
        if candidate_patch_path and os.path.exists(candidate_patch_path):
            try:
                with open(candidate_patch_path, "r", encoding="utf-8") as f:
                    candidate_patch_excerpt = _compact_text(f.read(), 1800)
            except OSError:
                candidate_patch_excerpt = ""

        return FailureAnalysis(
            signature=signature,
            prompt_mode=prompt_mode,
            summary=summary,
            failure_step=failure_step,
            failure_kind=failure_kind,
            failure_reason=failure_reason,
            failure_expected=failure_expected,
            failure_actual=failure_actual,
            base_signature=base_signature_copy,
            base_prompt_mode=self.prompt_modes.get(base_signature_copy, prompt_mode),
            repeated=repeated,
            repetition_count=repetition_count,
            recommendation=recommendation,
            notes=_dedupe_keep_order(notes),
            evidence=_dedupe_keep_order(evidence),
            targeted_excerpt=_extract_first_step_excerpt(validation_result) if validation_result is not None else "",
            stimulus_window=_compact_text(str(timing_hint.get("stimulus_window") or ""), 1800),
            timing_signature=str(timing_hint.get("timing_signature") or ""),
            timing_sensitive=bool(timing_hint.get("timing_sensitive")),
            timing_hints=list(timing_hint.get("timing_hints") or []),
            failure_line_number=_extract_failure_line_number(_extract_first_step_excerpt(validation_result) if validation_result is not None else ""),
            observed_signal=str(timing_hint.get("observed_signal") or ""),
            expected_idle=str(timing_hint.get("expected_idle") or ""),
            candidate_patch_excerpt=candidate_patch_excerpt,
            change_files=list(change_files or []),
            patch_hash=patch_hash,
            diff_hash=diff_hash,
            validation_report_path=validation_report_path,
        )


class PromptBuilder:
    def __init__(self, language: str = "verilog", max_input_length: int = 65536) -> None:
        self.language = language
        self.max_input_length = int(max_input_length)
        self.global_rtl_rules = self._load_global_rtl_rules()

    def _load_global_rtl_rules(self) -> str:
        here = os.path.abspath(os.path.dirname(__file__))
        candidates = [
            os.path.abspath(os.path.join(here, "..", "RTL_REPAIR_RULES.md")),
            os.path.abspath("RTL_REPAIR_RULES.md"),
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        return f.read().strip()
                except OSError:
                    return ""
        return ""

    def _section(self, title: str, body: str) -> str:
        body = (body or "").strip()
        if not body:
            return ""
        return f"## {title}\n{body}\n"

    def _bullet_section(self, title: str, items: Sequence[str]) -> str:
        lines = [item.strip() for item in items if str(item).strip()]
        if not lines:
            return ""
        return self._section(title, "\n".join(f"- {line}" for line in lines))

    def _limit(self, text: str, limit: int) -> str:
        return _compact_text(text or "", limit)

    def _format_attempt(self, attempt: Dict[str, Any]) -> str:
        parts = []
        attempt_no = attempt.get("attempt") or attempt.get("attempt_index") or "?"
        parts.append(f"- attempt {attempt_no}: {attempt.get('status') or attempt.get('failure_signature') or 'unknown'}")
        if attempt.get("failure_signature") and attempt.get("failure_signature") != attempt.get("status"):
            parts.append(f"  signature={attempt.get('failure_signature')}")
        if attempt.get("timing_signature"):
            parts.append(f"  timing_signature={attempt.get('timing_signature')}")
        if attempt.get("prompt_mode"):
            parts.append(f"  mode={attempt.get('prompt_mode')}")
        if attempt.get("failure_step"):
            parts.append(f"  step={attempt.get('failure_step')}")
        if attempt.get("summary"):
            parts.append(f"  summary={_compact_text(str(attempt.get('summary')), 280)}")
        if attempt.get("recommendation"):
            parts.append(f"  lesson={_compact_text(str(attempt.get('recommendation')), 260)}")
        files = attempt.get("edited_files") or attempt.get("files") or []
        if files:
            parts.append(f"  files={_string_list(files, limit=6)}")
        tests = attempt.get("tests") or []
        if tests:
            parts.append(f"  tests={_string_list(tests, limit=4)}")
        evidence = attempt.get("evidence") or []
        if evidence:
            parts.append(f"  evidence={_compact_text(' | '.join(map(str, evidence[:4])), 300)}")
        return "\n".join(parts)

    def _format_memory(self, record: Dict[str, Any]) -> str:
        parts = []
        score = record.get("score")
        if score is not None:
            parts.append(f"- score={score}")
        if record.get("instance_id"):
            parts.append(f"  instance={record.get('instance_id')}")
        if record.get("failure_signature"):
            parts.append(f"  signature={record.get('failure_signature')}")
        if record.get("base_signature"):
            parts.append(f"  base_signature={record.get('base_signature')}")
        if record.get("prompt_mode"):
            parts.append(f"  mode={record.get('prompt_mode')}")
        if record.get("status"):
            parts.append(f"  status={record.get('status')}")
        if record.get("summary"):
            parts.append(f"  summary={_compact_text(str(record.get('summary')), 260)}")
        if record.get("recommendation"):
            parts.append(f"  lesson={_compact_text(str(record.get('recommendation')), 220)}")
        files = record.get("files") or []
        if files:
            parts.append(f"  files={_string_list(files, limit=6)}")
        tests = record.get("tests") or []
        if tests:
            parts.append(f"  tests={_string_list(tests, limit=4)}")
        edited_files = record.get("edited_files") or []
        if edited_files:
            parts.append(f"  edited_files={_string_list(edited_files, limit=6)}")
        evidence = record.get("evidence") or []
        if evidence:
            parts.append(f"  evidence={_compact_text(' | '.join(map(str, evidence[:4])), 300)}")
        if record.get("patch_apply_status"):
            parts.append(f"  patch_apply={record.get('patch_apply_status')}")
        if record.get("patch_apply_reason"):
            parts.append(f"  patch_reason={_compact_text(str(record.get('patch_apply_reason')), 220)}")
        if record.get("validation_passed") is not None:
            parts.append(f"  validated={bool(record.get('validation_passed'))}")
        return "\n".join(parts)

    def _format_language_block(self, language_prompt_parts: Dict[str, str]) -> str:
        language_name = language_prompt_parts.get("language_name", "repository language")
        fence_language = language_prompt_parts.get("fence_language", "text")
        use_replace_only = bool(language_prompt_parts.get("use_replace_only"))
        example_file = language_prompt_parts.get("example_file", "path/to/example")
        example_start_line = int(language_prompt_parts.get("example_start_line", 1) or 1)
        example_end_line = int(language_prompt_parts.get("example_end_line", example_start_line) or example_start_line)
        example_search = language_prompt_parts.get("example_search", "")
        example_replace = language_prompt_parts.get("example_replace", "")
        language_notes = language_prompt_parts.get("language_notes", "")
        if use_replace_only:
            return f"""
Please return only line-range replacement edits.

Edit format:
1. `### file/path.sv`
2. `- start_line: N`
3. `- end_line: M`
4. `<<<<<<< REPLACE`
5. replacement text for lines N-M
6. `>>>>>>> REPLACE`

Example for {language_name}:

```{fence_language}
### {example_file}
- start_line: {example_start_line}
- end_line: {example_end_line}
<<<<<<< REPLACE
{example_replace}
>>>>>>> REPLACE
```

Important:
- Do not output a SEARCH block for Verilog/SystemVerilog.
- Choose start_line/end_line from Candidate Source Files when available.
- The tool will read the original code from the current worktree and replace that inclusive line range.
- Keep line ranges small and aligned to complete RTL statements or blocks.
- Do not invent new ports, signals, or instance connections unless they already exist in the file context or the fix explicitly requires adding them.
- Do not edit the source repository; only edit the cloned worktree.
- Output one REPLACE block per file region.
{language_notes}
"""
        return f"""
Please return only SEARCH/REPLACE edits.

Edit format:
1. `### file/path.sv`
2. `- start_line: N`
3. `- end_line: M`
4. `<<<<<<< SEARCH`
5. exact source text from lines N-M
6. `=======`
7. replacement text for lines N-M
8. `>>>>>>> REPLACE`

Example for {language_name}:

```{fence_language}
### {example_file}
- start_line: {example_start_line}
- end_line: {example_end_line}
<<<<<<< SEARCH
{example_search}
=======
{example_replace}
>>>>>>> REPLACE
```

Important:
- Always include `- start_line:` and `- end_line:` for every edit block.
- If a Candidate Source Files section is provided, choose line ranges from that section and copy SEARCH text exactly from it, excluding the leading `N: ` line-number prefix.
- The SEARCH block must be the exact current content of that inclusive line range.
- Keep the SEARCH block exact and contiguous.
- Keep edits as small as possible.
- Do not invent new ports, signals, or instance connections unless they already exist in the file context.
- Do not edit the source repository; only edit the cloned worktree.
- Output one SEARCH/REPLACE block per file region.
{language_notes}
"""

    def _rtl_signal_chain_checklist(self, analysis: Optional[FailureAnalysis]) -> str:
        if analysis is None:
            return ""

        lines = [
            "Before writing the patch, close this RTL chain mentally and make the patch satisfy it:",
        ]
        if analysis.observed_signal:
            lines.append(f"1. Identify the direct driver of `{analysis.observed_signal}` in the current source.")
        else:
            lines.append("1. Identify the direct driver of the failing observed output in the current source.")
        lines.extend(
            [
                "2. Identify the configuration/input/state signals used by that driver.",
                "3. Check whether those signals are decoded correctly in the register/config block.",
                "4. Check whether top-level instance connections actually propagate those signals to the driver module.",
                "5. Check idle/not-active behavior separately from active transaction behavior.",
                "6. If the test observes immediately after a register write, make the driver output correct in that immediate idle window.",
                "7. Only edit the smallest source region that closes the broken link in this chain.",
            ]
        )
        if analysis.expected_idle:
            lines.append(f"Expected idle value from validation: `{analysis.expected_idle}`.")
        if analysis.timing_signature:
            lines.append(f"Timing signature: `{analysis.timing_signature}`.")
        return "\n".join(lines)

    def build_prompt(
        self,
        *,
        problem_statement: str,
        localization_summary: str,
        current_edit_targets: str,
        evidence_entities: str,
        hard_constraints: Sequence[str],
        language_prompt_parts: Dict[str, str],
        attempt_index: int,
        max_attempts: int,
        candidate_source_context: str = "",
        analysis: Optional[FailureAnalysis] = None,
        prior_attempts: Sequence[Dict[str, Any]] = (),
        retrieval_memory: Sequence[Dict[str, Any]] = (),
        baseline_note: str = "",
        repair_mode: str = "initial",
    ) -> str:
        sections = []
        sections.append(self._section("Repair Mode", f"- mode: {repair_mode}\n- attempt: {attempt_index}/{max_attempts}"))
        sections.append(self._section("Issue Summary", self._limit(problem_statement, 2500)))
        sections.append(self._section("Localization Summary", self._limit(localization_summary, 2200)))
        sections.append(self._section("Current Edit Targets", self._limit(current_edit_targets, 5000)))
        if candidate_source_context:
            sections.append(self._section("Candidate Source Files", self._limit(candidate_source_context, 18000)))
        sections.append(self._section("Evidence Entities", self._limit(evidence_entities, 3500)))

        failure_lines = []
        if baseline_note:
            failure_lines.append(f"baseline_note: {baseline_note}")
        if analysis is not None:
            failure_lines.append(f"signature: {analysis.signature}")
            if analysis.base_signature and analysis.base_signature != analysis.signature:
                failure_lines.append(f"base_signature: {analysis.base_signature}")
            if analysis.signature == "patch_apply_failed":
                if language_prompt_parts.get("use_replace_only"):
                    failure_lines.append("patch_application_focus: output only file path, start_line/end_line, and replacement text. Do not include SEARCH.")
                    failure_lines.append("patch_application_focus: choose the visible line range from Candidate Source Files and replace the smallest complete RTL block.")
                else:
                    failure_lines.append("patch_application_focus: include exact start_line/end_line metadata and keep the SEARCH block equal to that line span.")
                failure_lines.append("patch_application_focus: if the last patch changed ports, outputs, or signal names, undo that and keep the edit inside one RTL block.")
            if analysis.failure_step:
                failure_lines.append(f"failure_step: {analysis.failure_step}")
            if analysis.failure_kind:
                failure_lines.append(f"failure_kind: {analysis.failure_kind}")
            if analysis.failure_reason:
                failure_lines.append(f"failure_reason: {analysis.failure_reason}")
            if analysis.summary:
                failure_lines.append(f"summary: {analysis.summary}")
            if analysis.repetition_count:
                failure_lines.append(f"repetition_count: {analysis.repetition_count}")
            if analysis.recommendation:
                failure_lines.append(f"recommendation: {analysis.recommendation}")
            if analysis.timing_signature:
                failure_lines.append(f"timing_signature: {analysis.timing_signature}")
            if analysis.timing_sensitive:
                failure_lines.append("timing_sensitive: true")
            if analysis.observed_signal:
                failure_lines.append(f"observed_signal: {analysis.observed_signal}")
            if analysis.expected_idle:
                failure_lines.append(f"expected_idle: {analysis.expected_idle}")
            if analysis.failure_line_number:
                failure_lines.append(f"failure_line_number: {analysis.failure_line_number}")
            if analysis.stimulus_window:
                failure_lines.append("stimulus_window:")
                failure_lines.append(self._limit(analysis.stimulus_window, 1800))
            if analysis.timing_hints:
                failure_lines.append("timing_hints:")
                failure_lines.extend(f"- {hint}" for hint in analysis.timing_hints[:6])
            if analysis.notes:
                failure_lines.append("notes:")
                failure_lines.extend(f"- {note}" for note in analysis.notes[:6])
            if analysis.evidence:
                failure_lines.append("evidence:")
                failure_lines.extend(f"- {entry}" for entry in analysis.evidence[:10])
            if analysis.candidate_patch_excerpt:
                failure_lines.append("candidate_patch_excerpt:")
                failure_lines.append(self._limit(analysis.candidate_patch_excerpt, 1800))
            if analysis.targeted_excerpt:
                failure_lines.append("targeted_testbench_excerpt:")
                failure_lines.append(self._limit(analysis.targeted_excerpt, 1800))
        else:
            failure_lines.append("signature: initial_repair")
            if baseline_note:
                failure_lines.append("Use the baseline note below to avoid the known failing path.")
        sections.append(self._section("Failure Evidence", "\n".join(failure_lines)))

        prior_lines = []
        if prior_attempts:
            for attempt in prior_attempts:
                prior_lines.append(self._format_attempt(attempt))
        else:
            prior_lines.append("- none")
        sections.append(self._section("Prior Attempts", "\n\n".join(prior_lines)))

        memory_lines = []
        if retrieval_memory:
            for record in retrieval_memory:
                memory_lines.append(self._format_memory(record))
        else:
            memory_lines.append("- none")
        sections.append(self._section("Retrieval Memory", "\n\n".join(memory_lines)))

        constraint_lines = list(hard_constraints)
        if candidate_source_context:
            constraint_lines.append("Use Candidate Source Files as the source of truth for file paths, line numbers, and SEARCH text.")
            constraint_lines.append("When copying SEARCH text, remove only the displayed line-number prefix; preserve all code whitespace after it.")
            constraint_lines.append("Do not output a patch for a line range that is not visible in Candidate Source Files unless no visible candidate can solve the issue.")
        if analysis is not None and analysis.recommendation:
            constraint_lines.append(f"repair_mode_hint: {analysis.recommendation}")
        if analysis is not None and analysis.signature in {"patch_apply_failed", "repeated_no_progress"}:
            constraint_lines.append("Keep the fix inside the currently targeted file and block; do not broaden the edit surface.")
            constraint_lines.append("Do not change port lists, output kinds, or signal names unless the issue explicitly requires an interface change.")
            if language_prompt_parts.get("use_replace_only"):
                constraint_lines.append("If patch application failed, rewrite only start_line/end_line and replacement text. Do not output SEARCH.")
                constraint_lines.append("Use exactly one REPLACE block per RTL unit.")
            else:
                constraint_lines.append("If patch application failed, rewrite start_line/end_line and SEARCH so they match the exact local RTL line span.")
                constraint_lines.append("Use exactly one SEARCH/REPLACE block per RTL unit.")
        if retrieval_memory:
            constraint_lines.append("Prefer the closest matching memory example, but keep only the smallest RTL surface that fits the current file.")
            constraint_lines.append("If memory and current evidence disagree, trust current validation evidence first.")
        if analysis is not None and analysis.timing_sensitive:
            constraint_lines.append("timing_sensitive: write-after-write-observe window must be handled immediately")
            constraint_lines.append("immediate post-write idle-state check: do not wait for a later clock edge to make idle SCK observable")
            constraint_lines.append("Prefer direct driver > top-level wiring > config register > side evidence.")
            chain_checklist = self._rtl_signal_chain_checklist(analysis)
            if chain_checklist:
                sections.append(self._section("RTL Signal Propagation Checklist", chain_checklist))
        constraint_lines.append("The patch must only touch the cloned worktree under workdirs/; never modify verilog_repair_cases/.")
        constraint_lines.append("Keep the edit scope minimal and prefer the smallest RTL unit that explains the failure.")
        sections.append(self._bullet_section("Hard Constraints", constraint_lines))
        sections.append(self._format_language_block(language_prompt_parts))

        prompt = "\n".join(section for section in sections if section).strip()
        if len(prompt) <= self.max_input_length:
            return prompt

        # Trim the lowest-priority sections if the prompt becomes too long.
        trimmed_sections = list(sections)
        section_titles = [
            "Repair Mode",
            "Issue Summary",
            "Localization Summary",
            "Current Edit Targets",
            "Candidate Source Files",
            "Evidence Entities",
            "Failure Evidence",
            "Prior Attempts",
            "Retrieval Memory",
            "Hard Constraints",
            "Search/Replace Format",
        ]
        for idx in (8, 7, 5, 4, 3, 2, 1):
            if len("\n".join(section for section in trimmed_sections if section).strip()) <= self.max_input_length:
                break
            if idx < len(trimmed_sections):
                trimmed_sections[idx] = self._section(
                    section_titles[idx],
                    _compact_text(trimmed_sections[idx], 1800),
                )

        prompt = "\n".join(section for section in trimmed_sections if section).strip()
        if len(prompt) <= self.max_input_length:
            return prompt

        return prompt[: self.max_input_length - 20].rstrip() + "\n...[truncated due to prompt budget]..."

    def _fit_prompt(self, sections: Sequence[str]) -> str:
        prompt = "\n".join(section for section in sections if section).strip()
        if len(prompt) <= self.max_input_length:
            return prompt
        trimmed = list(sections)
        for idx in range(len(trimmed) - 2, 0, -1):
            if len("\n".join(section for section in trimmed if section).strip()) <= self.max_input_length:
                break
            trimmed[idx] = _compact_text(trimmed[idx], 1800)
        prompt = "\n".join(section for section in trimmed if section).strip()
        if len(prompt) <= self.max_input_length:
            return prompt
        return prompt[: self.max_input_length - 20].rstrip() + "\n...[truncated due to prompt budget]..."

    def build_generation_prompt(
        self,
        *,
        problem_statement: str,
        localization_summary: str,
        current_edit_targets: str,
        evidence_entities: str,
        candidate_source_context: str,
        hard_constraints: Sequence[str],
        language_prompt_parts: Dict[str, str],
        attempt_index: int,
        max_attempts: int,
        analysis: Optional[FailureAnalysis] = None,
        baseline_note: str = "",
    ) -> str:
        sections = [
            self._section(
                "Agent Role",
                "Generation Agent. Produce the first RTL repair patch. In this phase the priority is a minimal patch that applies cleanly to the current worktree.",
            )
        ]
        if self.global_rtl_rules:
            sections.append(self._section("Global RTL Repair Rules", self._limit(self.global_rtl_rules, 7000)))
        sections.extend(
            [
                self._section("Attempt", f"- phase: generation\n- attempt: {attempt_index}/{max_attempts}"),
                self._section("Issue Summary", self._limit(problem_statement, 2200)),
                self._section("KG Localization Summary", self._limit(localization_summary, 2200)),
                self._section("Editable Targets From KG", self._limit(current_edit_targets, 4200)),
            ]
        )
        if candidate_source_context:
            sections.append(self._section("Candidate RTL Source Files", self._limit(candidate_source_context, 18000)))
        sections.append(self._section("Evidence Entities", self._limit(evidence_entities, 2500)))

        feedback = []
        if baseline_note:
            feedback.append(f"baseline_note: {baseline_note}")
        if analysis is None:
            feedback.append("signature: initial_generation")
        else:
            feedback.append(f"signature: {analysis.signature}")
            feedback.append(f"summary: {analysis.summary}")
            feedback.append("The previous generation patch did not apply. Do not repeat it; choose visible line ranges and smaller complete RTL regions.")
            if analysis.candidate_patch_excerpt:
                feedback.append("failed_patch_excerpt:")
                feedback.append(self._limit(analysis.candidate_patch_excerpt, 1200))
        sections.append(self._section("Generation Feedback", "\n".join(feedback)))

        constraints = list(hard_constraints)
        constraints.extend(
            [
                "This is not the debug phase. Do not optimize against validation logs that are not shown.",
                "Prefer one or two minimal RTL regions. Avoid whole-file, whole-module, or broad always-block rewrites.",
                "Use only line ranges visible in Candidate RTL Source Files.",
                "For Verilog/SystemVerilog, output line-range REPLACE blocks only; do not output SEARCH.",
                "The patch must be cleanly applicable. If unsure, choose a smaller complete statement or block.",
            ]
        )
        sections.append(self._bullet_section("Generation Constraints", constraints))
        sections.append(self._format_language_block(language_prompt_parts))
        return self._fit_prompt(sections)

    def build_debug_prompt(
        self,
        *,
        problem_statement: str,
        candidate_source_context: str,
        hard_constraints: Sequence[str],
        language_prompt_parts: Dict[str, str],
        attempt_index: int,
        max_attempts: int,
        analysis: FailureAnalysis,
        prior_attempts: Sequence[Dict[str, Any]] = (),
        baseline_note: str = "",
    ) -> str:
        sections = [
            self._section(
                "Agent Role",
                "Debug Agent. A patch has already reached validation. Ignore KG localization and repair using validation failure evidence plus the current target source.",
            )
        ]
        if self.global_rtl_rules:
            sections.append(self._section("Global RTL Repair Rules", self._limit(self.global_rtl_rules, 7000)))
        sections.extend(
            [
                self._section("Attempt", f"- phase: debug\n- attempt: {attempt_index}/{max_attempts}\n- mode: {analysis.prompt_mode}"),
                self._section("Issue Summary", self._limit(problem_statement, 1600)),
            ]
        )

        failure = []
        if baseline_note:
            failure.append(f"baseline_note: {baseline_note}")
        failure.append(f"signature: {analysis.signature}")
        if analysis.base_signature and analysis.base_signature != analysis.signature:
            failure.append(f"base_signature: {analysis.base_signature}")
        if analysis.failure_step:
            failure.append(f"failure_step: {analysis.failure_step}")
        if analysis.failure_kind:
            failure.append(f"failure_kind: {analysis.failure_kind}")
        if analysis.failure_reason:
            failure.append(f"failure_reason: {analysis.failure_reason}")
        if analysis.summary:
            failure.append("summary:")
            failure.append(self._limit(analysis.summary, 2200))
        if analysis.timing_signature:
            failure.append(f"timing_signature: {analysis.timing_signature}")
        if analysis.observed_signal:
            failure.append(f"observed_signal: {analysis.observed_signal}")
        if analysis.expected_idle:
            failure.append(f"expected_idle: {analysis.expected_idle}")
        if analysis.stimulus_window:
            failure.append("stimulus_window:")
            failure.append(self._limit(analysis.stimulus_window, 1800))
        if analysis.targeted_excerpt:
            failure.append("targeted_testbench_excerpt:")
            failure.append(self._limit(analysis.targeted_excerpt, 2200))
        if analysis.evidence:
            failure.append("evidence:")
            failure.extend(f"- {entry}" for entry in analysis.evidence[:8])
        sections.append(self._section("Validation Failure Evidence", "\n".join(failure)))

        chain_checklist = self._rtl_signal_chain_checklist(analysis)
        if chain_checklist:
            sections.append(self._section("RTL Signal Propagation Checklist", chain_checklist))

        if candidate_source_context:
            sections.append(self._section("Current Target Source Files", self._limit(candidate_source_context, 20000)))

        prior_lines = [self._format_attempt(attempt) for attempt in prior_attempts[-5:]] if prior_attempts else ["- none"]
        sections.append(self._section("Prior Attempts", "\n\n".join(prior_lines)))

        constraints = list(hard_constraints)
        constraints.extend(
            [
                "Do not use KG localization in this phase; trust validation failure evidence and current target source.",
                "If compile failed, fix only the syntax/interface/width issue shown by the compiler.",
                "If targeted failed, repair the direct driver of the observed failing signal.",
                "Do not change testbench files or test expectations.",
                "Do not rewrite unrelated RTL behavior that passed regression.",
                "For Verilog/SystemVerilog, output line-range REPLACE blocks only; do not output SEARCH.",
            ]
        )
        if analysis.timing_sensitive:
            constraints.append("Timing-sensitive failure: make the observed output correct at the exact failing stimulus window, not only later in the transaction.")
        sections.append(self._bullet_section("Debug Constraints", constraints))
        sections.append(self._format_language_block(language_prompt_parts))
        return self._fit_prompt(sections)

    def build_debug_diagnosis_prompt(
        self,
        *,
        problem_statement: str,
        candidate_source_context: str,
        debug_kg_context: str = "",
        analysis: FailureAnalysis,
        prior_attempts: Sequence[Dict[str, Any]] = (),
        baseline_note: str = "",
        max_files: int = 5,
    ) -> str:
        sections = [
            self._section(
                "Agent Role",
                "Debug Diagnose Agent. Diagnose the validation failure and request the RTL files needed for the next patch. Do not output a patch.",
            )
        ]
        if self.global_rtl_rules:
            sections.append(self._section("Global RTL Repair Rules", self._limit(self.global_rtl_rules, 4500)))
        sections.append(self._section("Issue Summary", self._limit(problem_statement, 1600)))

        failure = []
        if baseline_note:
            failure.append(f"baseline_note: {baseline_note}")
        failure.append(f"signature: {analysis.signature}")
        if analysis.failure_step:
            failure.append(f"failure_step: {analysis.failure_step}")
        if analysis.failure_kind:
            failure.append(f"failure_kind: {analysis.failure_kind}")
        if analysis.summary:
            failure.append("summary:")
            failure.append(self._limit(analysis.summary, 2000))
        if analysis.observed_signal:
            failure.append(f"observed_signal: {analysis.observed_signal}")
        if analysis.expected_idle:
            failure.append(f"expected_idle: {analysis.expected_idle}")
        if analysis.timing_signature:
            failure.append(f"timing_signature: {analysis.timing_signature}")
        if analysis.stimulus_window:
            failure.append("stimulus_window:")
            failure.append(self._limit(analysis.stimulus_window, 1600))
        if analysis.targeted_excerpt:
            failure.append("targeted_testbench_excerpt:")
            failure.append(self._limit(analysis.targeted_excerpt, 1800))
        sections.append(self._section("Validation Failure Evidence", "\n".join(failure)))

        chain_checklist = self._rtl_signal_chain_checklist(analysis)
        if chain_checklist:
            sections.append(self._section("RTL Signal Propagation Checklist", chain_checklist))

        if debug_kg_context:
            sections.append(self._section("Debug KG Context", self._limit(debug_kg_context, 5000)))

        if candidate_source_context:
            sections.append(self._section("Currently Visible Source Context", self._limit(candidate_source_context, 14000)))

        prior_lines = [self._format_attempt(attempt) for attempt in prior_attempts[-4:]] if prior_attempts else ["- none"]
        sections.append(self._section("Prior Attempts", "\n\n".join(prior_lines)))

        schema = f"""
Return only JSON. Do not wrap it in Markdown fences.
The JSON object must use this schema:
{{
  "failure_hypothesis": "one concise sentence",
  "observed_signal": "{analysis.observed_signal or ''}",
  "expected_behavior": "what the RTL must do at the failing stimulus window",
  "signal_chain": [
    "source/config/input",
    "top-level wiring",
    "direct driver",
    "observed output"
  ],
  "need_files": [
    "src/rtl/file1.sv"
  ],
  "edit_candidates": [
    {{"file": "src/rtl/file.sv", "reason": "why this file may need editing"}}
  ],
  "do_not_change": [
    "testbench",
    "unrelated RTL"
  ]
}}
Rules:
- Request at most {max_files} RTL/include files.
- Prefer files already visible in the source context, then upstream/downstream files needed to close the signal chain.
- Do not request testbench files unless the failure evidence is unclear; tests are oracle by default.
- For targeted_failed, include the file containing the direct driver of the observed signal when identifiable.
- For cross-module propagation, include the top-level wiring file and the config/register decode file if relevant.
"""
        sections.append(self._section("Diagnosis Output Contract", schema.strip()))
        return self._fit_prompt(sections)

    def build_debug_patch_prompt(
        self,
        *,
        problem_statement: str,
        expanded_source_context: str,
        diagnosis: Dict[str, Any],
        debug_kg_context: str = "",
        hard_constraints: Sequence[str],
        language_prompt_parts: Dict[str, str],
        attempt_index: int,
        max_attempts: int,
        analysis: FailureAnalysis,
        prior_attempts: Sequence[Dict[str, Any]] = (),
        baseline_note: str = "",
    ) -> str:
        sections = [
            self._section(
                "Agent Role",
                "Debug Patch Agent. Use the diagnosis, validation evidence, and current worktree source to produce the smallest line-range REPLACE patch.",
            )
        ]
        if self.global_rtl_rules:
            sections.append(self._section("Global RTL Repair Rules", self._limit(self.global_rtl_rules, 5500)))
        sections.append(self._section("Attempt", f"- phase: debug_patch\n- attempt: {attempt_index}/{max_attempts}\n- mode: {analysis.prompt_mode}"))
        sections.append(self._section("Issue Summary", self._limit(problem_statement, 1400)))
        sections.append(self._section("Diagnosis JSON", json.dumps(diagnosis or {}, indent=2, ensure_ascii=False)))

        failure = []
        if baseline_note:
            failure.append(f"baseline_note: {baseline_note}")
        failure.append(f"signature: {analysis.signature}")
        if analysis.summary:
            failure.append("summary:")
            failure.append(self._limit(analysis.summary, 1800))
        if analysis.observed_signal:
            failure.append(f"observed_signal: {analysis.observed_signal}")
        if analysis.expected_idle:
            failure.append(f"expected_idle: {analysis.expected_idle}")
        if analysis.stimulus_window:
            failure.append("stimulus_window:")
            failure.append(self._limit(analysis.stimulus_window, 1200))
        sections.append(self._section("Validation Failure Evidence", "\n".join(failure)))

        if debug_kg_context:
            sections.append(self._section("Debug KG Context", self._limit(debug_kg_context, 5000)))

        if expanded_source_context:
            sections.append(self._section("Expanded Current Worktree Source", self._limit(expanded_source_context, 24000)))

        prior_lines = [self._format_attempt(attempt) for attempt in prior_attempts[-4:]] if prior_attempts else ["- none"]
        sections.append(self._section("Prior Attempts", "\n\n".join(prior_lines)))

        constraints = list(hard_constraints)
        constraints.extend(
            [
                "Patch only files shown in Expanded Current Worktree Source.",
                "Use Debug KG Context as a compact structure index for cross-file signal propagation; validation evidence remains the source of truth.",
                "Use the diagnosis to close the broken signal chain; do not re-localize broadly.",
                "For targeted_failed, repair the direct driver of observed_signal first. If the direct driver depends on missing upstream wiring, patch that wiring too.",
                "Do not modify testbench files or test expectations.",
                "Output only line-range REPLACE blocks. No JSON, no explanation, no Markdown fences.",
            ]
        )
        if analysis.timing_sensitive:
            constraints.append("Make the observed output correct at the exact failing stimulus window, not only later.")
        sections.append(self._bullet_section("Debug Patch Constraints", constraints))
        sections.append(self._format_language_block(language_prompt_parts))
        return self._fit_prompt(sections)
