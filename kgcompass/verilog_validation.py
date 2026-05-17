from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:
    from verilog_timing import classify_timing_failure_excerpt
except Exception:  # pragma: no cover - package-relative fallback.
    from .verilog_timing import classify_timing_failure_excerpt


def _snippet(text: str, limit: int = 2000) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]..."


def _dedupe_keep_order(items: Iterable[str]) -> List[str]:
    seen = set()
    ordered = []
    for item in items:
        if item in seen:
            continue
        ordered.append(item)
        seen.add(item)
    return ordered


@dataclass
class ValidationStepResult:
    name: str
    kind: str
    mode: str
    expected_outcome: str
    actual_outcome: str
    passed: bool
    compile_command: List[str]
    run_command: List[str]
    compile_returncode: Optional[int]
    run_returncode: Optional[int]
    compile_stdout_path: str
    compile_stderr_path: str
    run_stdout_path: str = ""
    run_stderr_path: str = ""
    vvp_output_path: str = ""
    vcd_output_path: str = ""
    failure_line_number: int = 0
    stimulus_window: str = ""
    timing_signature: str = ""
    timing_sensitive: bool = False
    observed_signal: str = ""
    expected_idle: str = ""
    summary: str = ""
    compile_stdout_excerpt: str = ""
    compile_stderr_excerpt: str = ""
    run_stdout_excerpt: str = ""
    run_stderr_excerpt: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationSuiteResult:
    available: bool
    mode: str
    passed: bool
    steps: List[ValidationStepResult] = field(default_factory=list)
    failure_step: str = ""
    failure_summary: str = ""
    notes: List[str] = field(default_factory=list)
    toolchain: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [step.to_dict() for step in self.steps]
        return payload


class VerilogValidationRunner:
    """
    Execute local Verilog validation steps on an isolated working tree.

    The runner understands a compact benchmark schema:

    verification:
      compile:
        source_files: [...]
        include_dirs: [...]
        top: uart_targeted_tb
        iverilog_flags: ["-g2012"]
        run: false
      targeted:
        source_files: [...]
        include_dirs: [...]
        top: uart_clear_rx_overflow_tb
        iverilog_flags: ["-g2012"]
        dump_vcd: true
        expected_outcome_by_mode:
          baseline: fail
          candidate: pass
        markers:
          pass: ["PASS:"]
          fail: ["FAIL:"]
      regression:
        - ...
      coverage:
        name: spi_flash_cov_tb
        source_globs: [...]
        include_dirs: [...]
        top: spi_flash_cov_tb
        iverilog_flags: ["-g2012"]
        vvp_args: ["+COVERAGE_MIN=6"]
        expected_outcome_by_mode:
          baseline: pass
          candidate: pass
        markers:
          pass: ["PASS:", "COVERAGE:"]
          fail: ["FAIL:"]
        coverage_min_hits: 6
    """

    def __init__(self, verification_config: Optional[Dict[str, Any]] = None):
        self.config = verification_config or {}
        self.iverilog = self._resolve_tool("IVERILOG_BIN", "iverilog")
        self.vvp = self._resolve_tool("VVP_BIN", "vvp")

    def _resolve_tool(self, env_name: str, command_name: str) -> str:
        override = os.getenv(env_name)
        if override and os.path.exists(override):
            return override
        found = shutil.which(command_name)
        return found or ""

    def available(self) -> bool:
        return bool(self.iverilog and self.vvp)

    def availability_notes(self) -> List[str]:
        notes = []
        if not self.iverilog:
            notes.append("iverilog not found")
        if not self.vvp:
            notes.append("vvp not found")
        return notes

    def _resolve_source_files(self, repo_path: str, step_cfg: Dict[str, Any]) -> List[str]:
        root = Path(repo_path)
        resolved = []

        for entry in step_cfg.get("source_files", []):
            if not entry:
                continue
            candidate = Path(entry)
            if not candidate.is_absolute():
                candidate = root / entry
            if candidate.exists():
                resolved.append(str(candidate))
            else:
                resolved.append(str(candidate))

        for pattern in step_cfg.get("source_globs", []):
            if not pattern:
                continue
            matches = sorted(glob.glob(str(root / pattern)))
            resolved.extend(matches)

        return _dedupe_keep_order(resolved)

    def _resolve_include_dirs(self, repo_path: str, step_cfg: Dict[str, Any]) -> List[str]:
        root = Path(repo_path)
        include_dirs = []
        for entry in step_cfg.get("include_dirs", []):
            if not entry:
                continue
            candidate = Path(entry)
            if not candidate.is_absolute():
                candidate = root / entry
            include_dirs.append(str(candidate))
        return _dedupe_keep_order(include_dirs)

    def _mode_expected_outcome(self, step_cfg: Dict[str, Any], mode: str) -> str:
        expected_by_mode = step_cfg.get("expected_outcome_by_mode", {}) or {}
        if mode in expected_by_mode:
            return str(expected_by_mode[mode]).lower()
        if "expected_outcome" in step_cfg:
            return str(step_cfg["expected_outcome"]).lower()
        return "pass"

    def _markers_for_mode(self, step_cfg: Dict[str, Any], mode: str) -> Dict[str, List[str]]:
        markers = step_cfg.get("markers", {}) or {}
        return {
            "pass": list(step_cfg.get("pass_markers", markers.get("pass", [])) or []),
            "fail": list(step_cfg.get("fail_markers", markers.get("fail", [])) or []),
            "pass_not": list(step_cfg.get("forbidden_pass_markers", markers.get("pass_not", [])) or []),
            "fail_not": list(step_cfg.get("forbidden_fail_markers", markers.get("fail_not", [])) or []),
        }

    def _coverage_min_hits(self, step_cfg: Dict[str, Any]) -> Optional[int]:
        for key in ("coverage_min_hits", "min_hits", "required_bins"):
            value = step_cfg.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    def _extract_failure_location(self, text: str) -> tuple[str, int]:
        if not text:
            return "", 0
        match = re.search(r"([A-Za-z0-9_./\\-]+\.(?:sv|v|svh|vh)):(\d+)", text)
        if not match:
            return "", 0
        return match.group(1), int(match.group(2))

    def _source_window(self, source_path: str, line_number: int, radius: int = 6) -> str:
        if not source_path or not os.path.exists(source_path):
            return ""
        try:
            lines = Path(source_path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        if not lines:
            return ""
        if line_number <= 0:
            start = 1
            end = min(len(lines), 18)
        else:
            start = max(1, line_number - radius)
            end = min(len(lines), line_number + radius)
        excerpt = [f"{line_no}: {lines[line_no - 1]}" for line_no in range(start, end + 1)]
        return f"{os.path.basename(source_path)}:{start}-{end}\n" + "\n".join(excerpt)

    def _build_vcd_wrapper(self, artifact_dir: str, step_name: str, top_module: str, vcd_output_path: str) -> tuple[str, str]:
        wrapper_name = f"kgcompass_vcd_wrapper_{step_name}"
        wrapper_path = os.path.join(artifact_dir, f"{wrapper_name}.sv")
        vcd_literal = vcd_output_path.replace("\\", "/")
        wrapper = "\n".join([
            f"module {wrapper_name};",
            "    initial begin",
            f"        $dumpfile(\"{vcd_literal}\");",
            "        $dumpvars(0, uut);",
            "    end",
            f"    {top_module} uut();",
            "endmodule",
            "",
        ])
        self._write_text(wrapper_path, wrapper)
        return wrapper_name, wrapper_path

    def _parse_coverage_hits(self, text: str) -> Optional[tuple[int, int, Optional[int]]]:
        if not text:
            return None
        import re

        patterns = [
            r"COVERAGE:\s*(\d+)\s*/\s*(\d+)\s*bins?\s*hit(?:,\s*required\s*=\s*(\d+))?",
            r"coverage\s*[:=]\s*(\d+)\s*/\s*(\d+)(?:\s*.*?required\s*[:=]\s*(\d+))?",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                hits = int(match.group(1))
                total = int(match.group(2))
                required = int(match.group(3)) if match.group(3) else None
                return hits, total, required
        return None

    def _extract_compile_warnings(self, text: str) -> List[str]:
        if not text:
            return []
        warnings = []
        for line in (text or "").splitlines():
            lower = line.lower()
            if "warning:" in lower or "warn:" in lower:
                warnings.append(line.strip())
        return warnings[:20]

    def _is_strict_warning(self, warning: str) -> bool:
        strict_tokens = (
            "padding",
            "width",
            "mismatch",
            "implicit",
            "undeclared",
            "truncat",
            "sign extension",
            "port",
        )
        lowered = (warning or "").lower()
        return any(token in lowered for token in strict_tokens)

    def _write_text(self, path: str, content: str) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
        return path

    def _run_subprocess(self, command: List[str], cwd: str, log_prefix: str, timeout_sec: int) -> Dict[str, Any]:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
        )
        return {
            "returncode": proc.returncode,
            "stdout": proc.stdout or "",
            "stderr": proc.stderr or "",
            "stdout_excerpt": _snippet(proc.stdout or ""),
            "stderr_excerpt": _snippet(proc.stderr or ""),
        }

    def _run_step(
        self,
        repo_path: str,
        artifact_dir: str,
        step_name: str,
        step_kind: str,
        step_cfg: Dict[str, Any],
        mode: str,
    ) -> ValidationStepResult:
        repo_path = os.path.abspath(repo_path)
        artifact_dir = os.path.abspath(artifact_dir)
        os.makedirs(artifact_dir, exist_ok=True)
        timeout_sec = int(step_cfg.get("timeout_sec", 90))
        expected_outcome = self._mode_expected_outcome(step_cfg, mode)
        markers = self._markers_for_mode(step_cfg, mode)

        source_files = self._resolve_source_files(repo_path, step_cfg)
        include_dirs = self._resolve_include_dirs(repo_path, step_cfg)
        flags = list(step_cfg.get("iverilog_flags", []))
        if not flags:
            flags = ["-g2012"]

        vvp_output = os.path.join(artifact_dir, f"{step_name}.vvp")
        compile_stdout_path = os.path.join(artifact_dir, f"{step_name}.compile.stdout.log")
        compile_stderr_path = os.path.join(artifact_dir, f"{step_name}.compile.stderr.log")
        run_stdout_path = os.path.join(artifact_dir, f"{step_name}.run.stdout.log")
        run_stderr_path = os.path.join(artifact_dir, f"{step_name}.run.stderr.log")
        vcd_output_path = os.path.join(artifact_dir, f"{step_name}.vcd") if step_cfg.get("dump_vcd") else ""

        compile_cmd = [self.iverilog, *flags]
        for include_dir in include_dirs:
            compile_cmd.extend(["-I", include_dir])
        top_module = step_cfg.get("top")
        wrapper_path = ""
        if vcd_output_path and top_module:
            wrapper_top, wrapper_path = self._build_vcd_wrapper(artifact_dir, step_name, str(top_module), vcd_output_path)
            compile_cmd.extend(["-s", wrapper_top])
        elif top_module:
            compile_cmd.extend(["-s", top_module])
        compile_cmd.extend(["-o", vvp_output])
        if wrapper_path:
            source_files.append(wrapper_path)
        compile_cmd.extend(source_files)

        compile_run = self._run_subprocess(compile_cmd, repo_path, f"{step_name}.compile", timeout_sec)
        self._write_text(compile_stdout_path, compile_run["stdout"])
        self._write_text(compile_stderr_path, compile_run["stderr"])
        compile_warnings = self._extract_compile_warnings(f"{compile_run['stdout']}\n{compile_run['stderr']}")

        compile_passed = compile_run["returncode"] == 0
        if not compile_passed:
            return ValidationStepResult(
                name=step_name,
                kind=step_kind,
                mode=mode,
                expected_outcome=expected_outcome,
                actual_outcome="compile_failed",
                passed=False,
                compile_command=compile_cmd,
                run_command=[],
                compile_returncode=compile_run["returncode"],
                run_returncode=None,
                compile_stdout_path=compile_stdout_path,
                compile_stderr_path=compile_stderr_path,
                run_stdout_path="",
                run_stderr_path="",
                vvp_output_path=vvp_output,
                vcd_output_path=vcd_output_path,
                summary="compile failed",
                compile_stdout_excerpt=compile_run["stdout_excerpt"],
                compile_stderr_excerpt=compile_run["stderr_excerpt"],
            )

        strict_warnings_enabled = str(step_cfg.get("strict_compile_warnings", os.getenv("VERILOG_STRICT_COMPILE_WARNINGS", "0"))).lower() in {"1", "true", "yes"}

        if expected_outcome == "pass" and compile_warnings and strict_warnings_enabled:
            warning_block = "\n".join(compile_warnings)
            warning_block_lower = warning_block.lower()
            if any(token in warning_block_lower for token in ("padding", "width", "implicit", "undeclared", "mismatch")):
                return ValidationStepResult(
                    name=step_name,
                    kind=step_kind,
                    mode=mode,
                    expected_outcome=expected_outcome,
                    actual_outcome="compile_warning",
                    passed=False,
                    compile_command=compile_cmd,
                    run_command=[],
                    compile_returncode=compile_run["returncode"],
                    run_returncode=None,
                    compile_stdout_path=compile_stdout_path,
                    compile_stderr_path=compile_stderr_path,
                    run_stdout_path="",
                    run_stderr_path="",
                    vvp_output_path=vvp_output,
                    vcd_output_path=vcd_output_path,
                    summary="compile warning indicates interface mismatch",
                    compile_stdout_excerpt=compile_run["stdout_excerpt"],
                    compile_stderr_excerpt=compile_run["stderr_excerpt"],
                )

        run_cfg = step_cfg.get("run", True)
        if not run_cfg:
            actual_outcome = "pass" if expected_outcome == "pass" else "fail"
            passed = expected_outcome == "pass"
            return ValidationStepResult(
                name=step_name,
                kind=step_kind,
                mode=mode,
                expected_outcome=expected_outcome,
                actual_outcome=actual_outcome,
                passed=passed,
                compile_command=compile_cmd,
                run_command=[],
                compile_returncode=compile_run["returncode"],
                run_returncode=None,
                compile_stdout_path=compile_stdout_path,
                compile_stderr_path=compile_stderr_path,
                run_stdout_path="",
                run_stderr_path="",
                vvp_output_path=vvp_output,
                vcd_output_path=vcd_output_path,
                summary="compile only step",
                compile_stdout_excerpt=compile_run["stdout_excerpt"],
                compile_stderr_excerpt=compile_run["stderr_excerpt"],
            )

        run_cmd = [self.vvp]
        extra_run_args = list(step_cfg.get("vvp_args", []))
        if vcd_output_path:
            extra_run_args.append(f"+VCD={vcd_output_path}")
        run_cmd.append(vvp_output)
        run_cmd.extend(extra_run_args)

        run_run = self._run_subprocess(run_cmd, repo_path, f"{step_name}.run", timeout_sec)
        self._write_text(run_stdout_path, run_run["stdout"])
        self._write_text(run_stderr_path, run_run["stderr"])

        observed_stdout = run_run["stdout"]
        observed_stderr = run_run["stderr"]
        observed_returncode = run_run["returncode"]
        failure_path, failure_line_number = self._extract_failure_location(f"{observed_stdout}\n{observed_stderr}")
        if failure_path and not os.path.isabs(failure_path):
            repo_candidate = os.path.abspath(os.path.join(repo_path, failure_path))
            if os.path.exists(repo_candidate):
                failure_path = repo_candidate
        stimulus_window = self._source_window(failure_path, failure_line_number)
        if not stimulus_window:
            stimulus_window = _snippet(f"{observed_stdout}\n{observed_stderr}", 1200)
        timing_hint = {
            "timing_signature": "",
            "timing_sensitive": False,
            "timing_hints": [],
            "stimulus_window": stimulus_window,
            "observed_signal": "",
            "expected_idle": "",
        }
        if step_kind == "targeted":
            timing_hint = classify_timing_failure_excerpt(stimulus_window, f"{observed_stdout}\n{observed_stderr}")
        timing_signature = str(timing_hint.get("timing_signature") or "")
        timing_sensitive = bool(timing_hint.get("timing_sensitive"))
        observed_signal = str(timing_hint.get("observed_signal") or "")
        expected_idle = str(timing_hint.get("expected_idle") or "")
        pass_markers = markers["pass"]
        fail_markers = markers["fail"]
        pass_not = markers["pass_not"]
        fail_not = markers["fail_not"]
        coverage_min_hits = self._coverage_min_hits(step_cfg) if step_kind == "coverage" else None
        coverage_hits = None

        if expected_outcome == "pass":
            passed = observed_returncode == 0
            if pass_markers:
                passed = passed and all(marker in observed_stdout for marker in pass_markers)
            if pass_not:
                passed = passed and all(marker not in observed_stdout for marker in pass_not)
            if coverage_min_hits is not None:
                coverage_info = self._parse_coverage_hits(observed_stdout)
                if coverage_info is None:
                    passed = False
                else:
                    coverage_hits = coverage_info[0]
                    passed = passed and coverage_hits >= coverage_min_hits
            actual_outcome = "pass" if passed else "run_failed"
            summary = "candidate validation passed" if passed else "candidate validation failed"
        elif expected_outcome == "fail":
            marker_hit = bool(fail_markers) and any(
                marker in observed_stdout or marker in observed_stderr
                for marker in fail_markers
            )
            passed = observed_returncode != 0 or marker_hit
            if fail_markers:
                passed = passed and marker_hit
            if fail_not:
                passed = passed and all(marker not in observed_stdout for marker in fail_not)
            actual_outcome = "fail" if passed else "unexpected_pass"
            summary = "baseline reproduction confirmed" if passed else "baseline reproduction missing"
        else:
            passed = observed_returncode == 0
            actual_outcome = "pass" if passed else "run_failed"
            summary = expected_outcome

        return ValidationStepResult(
            name=step_name,
            kind=step_kind,
            mode=mode,
            expected_outcome=expected_outcome,
            actual_outcome=actual_outcome,
            passed=passed,
            compile_command=compile_cmd,
            run_command=run_cmd,
            compile_returncode=compile_run["returncode"],
            run_returncode=run_run["returncode"],
            compile_stdout_path=compile_stdout_path,
            compile_stderr_path=compile_stderr_path,
            run_stdout_path=run_stdout_path,
            run_stderr_path=run_stderr_path,
            vvp_output_path=vvp_output,
            vcd_output_path=vcd_output_path,
            failure_line_number=failure_line_number,
            stimulus_window=timing_hint.get("stimulus_window") or stimulus_window,
            timing_signature=timing_signature,
            timing_sensitive=timing_sensitive,
            observed_signal=observed_signal,
            expected_idle=expected_idle,
            summary=summary + (f"; coverage_hits={coverage_hits}" if coverage_hits is not None else ""),
            compile_stdout_excerpt=compile_run["stdout_excerpt"],
            compile_stderr_excerpt=compile_run["stderr_excerpt"],
            run_stdout_excerpt=run_run["stdout_excerpt"],
            run_stderr_excerpt=run_run["stderr_excerpt"],
        )

    def run_suite(self, repo_path: str, artifact_root: str, mode: str = "candidate") -> ValidationSuiteResult:
        repo_path = os.path.abspath(repo_path)
        artifact_root = os.path.abspath(artifact_root)
        steps: List[ValidationStepResult] = []
        notes: List[str] = []

        if not self.available():
            notes.extend(self.availability_notes())
            return ValidationSuiteResult(
                available=False,
                mode=mode,
                passed=False,
                steps=[],
                failure_step="toolchain",
                failure_summary="; ".join(notes) or "validation toolchain unavailable",
                notes=notes,
                toolchain={"iverilog": self.iverilog, "vvp": self.vvp},
            )

        compile_cfg = self.config.get("compile") or {}
        targeted_cfg = self.config.get("targeted") or {}
        regression_cfg = self.config.get("regression") or []
        coverage_cfg = self.config.get("coverage") or []
        if isinstance(regression_cfg, dict):
            regression_cfg = [regression_cfg]
        if isinstance(coverage_cfg, dict):
            coverage_cfg = [coverage_cfg]

        compile_step = None
        if compile_cfg:
            compile_step = self._run_step(repo_path, os.path.join(artifact_root, "compile"), "compile", "compile", compile_cfg, mode)
            steps.append(compile_step)

        if targeted_cfg:
            targeted_step = self._run_step(repo_path, os.path.join(artifact_root, "targeted"), targeted_cfg.get("name", "targeted"), "targeted", targeted_cfg, mode)
            steps.append(targeted_step)

        for idx, reg_cfg in enumerate(regression_cfg, 1):
            reg_name = reg_cfg.get("name") or f"regression_{idx:02d}"
            reg_step = self._run_step(repo_path, os.path.join(artifact_root, "regression", reg_name), reg_name, "regression", reg_cfg, mode)
            steps.append(reg_step)

        for idx, cov_cfg in enumerate(coverage_cfg, 1):
            cov_name = cov_cfg.get("name") or f"coverage_{idx:02d}"
            cov_step = self._run_step(repo_path, os.path.join(artifact_root, "coverage", cov_name), cov_name, "coverage", cov_cfg, mode)
            steps.append(cov_step)

        failure_step = ""
        failure_summary = ""
        passed = True
        for step in steps:
            if step.passed:
                continue
            passed = False
            failure_step = step.name
            failure_summary = step.summary or step.actual_outcome
            break

        if passed:
            notes.append(f"mode={mode}")
        else:
            notes.append(f"failed_step={failure_step}")

        return ValidationSuiteResult(
            available=True,
            mode=mode,
            passed=passed,
            steps=steps,
            failure_step=failure_step,
            failure_summary=failure_summary,
            notes=notes,
            toolchain={"iverilog": self.iverilog, "vvp": self.vvp},
        )
