from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence


def _compact_text(text: str, limit: int = 1000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]..."


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


def _tokenize(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z_][\w$]*", text or "")
        if len(token) > 1
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ExperienceRecord:
    timestamp: str = ""
    instance_id: str = ""
    benchmark_name: str = ""
    repo_identifier: str = ""
    failure_signature: str = ""
    base_signature: str = ""
    prompt_mode: str = ""
    status: str = ""
    summary: str = ""
    recommendation: str = ""
    files: List[str] = field(default_factory=list)
    tests: List[str] = field(default_factory=list)
    edited_files: List[str] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    validation_passed: bool = False
    patch_apply_status: str = ""
    patch_apply_reason: str = ""
    attempt_index: int = 0
    patch_hash: str = ""
    diff_hash: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ExperienceStore:
    def __init__(self, path: Optional[str] = None) -> None:
        default_path = os.getenv(
            "VERILOG_EXPERIENCE_STORE_PATH",
            os.path.join(os.getcwd(), "state", "verilog_experience_store.jsonl"),
        )
        self.path = os.path.abspath(path or default_path)

    def _load(self) -> List[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return []
        records: List[Dict[str, Any]] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
        return records

    def append(self, record: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def record_attempt(
        self,
        *,
        instance_id: str,
        benchmark_name: str,
        repo_identifier: str,
        attempt_record: Dict[str, Any],
    ) -> None:
        if not attempt_record:
            return
        payload = ExperienceRecord(
            timestamp=_utc_now(),
            instance_id=instance_id,
            benchmark_name=benchmark_name,
            repo_identifier=repo_identifier,
            failure_signature=str(attempt_record.get("failure_signature") or ""),
            base_signature=str(attempt_record.get("base_signature") or ""),
            prompt_mode=str(attempt_record.get("prompt_mode") or ""),
            status=str(attempt_record.get("status") or ""),
            summary=_compact_text(str(attempt_record.get("summary") or ""), 1000),
            recommendation=_compact_text(str(attempt_record.get("recommendation") or ""), 700),
            files=list(attempt_record.get("files") or []),
            tests=list(attempt_record.get("tests") or []),
            edited_files=list(attempt_record.get("edited_files") or []),
            evidence=list(attempt_record.get("evidence") or []),
            validation_passed=bool(attempt_record.get("validation_passed")),
            patch_apply_status=str((attempt_record.get("patch_application") or {}).get("status") or ""),
            patch_apply_reason=_compact_text(str((attempt_record.get("patch_application") or {}).get("reason") or ""), 700),
            attempt_index=int(attempt_record.get("attempt_index") or 0),
            patch_hash=str(attempt_record.get("patch_hash") or ""),
            diff_hash=str(attempt_record.get("diff_hash") or ""),
        ).to_dict()
        self.append(payload)

    def _score_record(
        self,
        record: Dict[str, Any],
        *,
        instance_id: str = "",
        benchmark_name: str = "",
        failure_signature: str = "",
        files: Sequence[str] = (),
        tests: Sequence[str] = (),
        edited_files: Sequence[str] = (),
        text: str = "",
    ) -> float:
        score = 0.0
        record_instance = str(record.get("instance_id") or "")
        record_benchmark = str(record.get("benchmark_name") or "")
        record_failure_signature = str(record.get("failure_signature") or "")
        record_base_signature = str(record.get("base_signature") or "")

        if instance_id and record_instance == instance_id:
            score += 8.0
        elif instance_id and record_instance.split("-")[:1] == instance_id.split("-")[:1]:
            score += 2.0

        if benchmark_name and record_benchmark == benchmark_name:
            score += 1.5

        if failure_signature and record_failure_signature == failure_signature:
            score += 5.0
        elif failure_signature and record_base_signature == failure_signature:
            score += 2.5

        record_files = set(record.get("files") or []) | set(record.get("edited_files") or [])
        record_tests = set(record.get("tests") or [])
        query_files = set(files) | set(edited_files)
        query_tests = set(tests)

        if record_files and query_files:
            score += min(len(record_files & query_files), 6) * 1.2
        if record_tests and query_tests:
            score += min(len(record_tests & query_tests), 4) * 1.0

        record_text = " ".join(
            str(record.get(field) or "")
            for field in ("summary", "recommendation", "patch_apply_reason", "status")
        )
        query_tokens = _tokenize(text)
        record_tokens = _tokenize(record_text)
        if query_tokens and record_tokens:
            overlap = len(query_tokens & record_tokens)
            union = len(query_tokens | record_tokens)
            if union:
                score += (overlap / union) * 2.0

        if record.get("validation_passed"):
            score += 1.0
        if str(record.get("status") or "") == "validated":
            score += 1.5
        return score

    def retrieve(
        self,
        *,
        instance_id: str = "",
        benchmark_name: str = "",
        failure_signature: str = "",
        files: Sequence[str] = (),
        tests: Sequence[str] = (),
        edited_files: Sequence[str] = (),
        text: str = "",
        limit: int = 4,
    ) -> List[Dict[str, Any]]:
        records = self._load()
        scored = []
        for record in records:
            score = self._score_record(
                record,
                instance_id=instance_id,
                benchmark_name=benchmark_name,
                failure_signature=failure_signature,
                files=files,
                tests=tests,
                edited_files=edited_files,
                text=text,
            )
            if score <= 0:
                continue
            scored.append((score, record))
        scored.sort(key=lambda item: (-item[0], str(item[1].get("timestamp") or "")), reverse=False)
        top_records = []
        for score, record in scored[:limit]:
            top_records.append({
                "score": round(score, 4),
                "timestamp": record.get("timestamp", ""),
                "instance_id": record.get("instance_id", ""),
                "benchmark_name": record.get("benchmark_name", ""),
                "failure_signature": record.get("failure_signature", ""),
                "base_signature": record.get("base_signature", ""),
                "prompt_mode": record.get("prompt_mode", ""),
                "status": record.get("status", ""),
                "summary": _compact_text(str(record.get("summary") or ""), 320),
                "recommendation": _compact_text(str(record.get("recommendation") or ""), 260),
                "files": _dedupe_keep_order(record.get("files") or [])[:6],
                "tests": _dedupe_keep_order(record.get("tests") or [])[:4],
                "edited_files": _dedupe_keep_order(record.get("edited_files") or [])[:6],
                "evidence": _dedupe_keep_order(record.get("evidence") or [])[:4],
                "validation_passed": bool(record.get("validation_passed")),
                "patch_apply_status": record.get("patch_apply_status", ""),
                "patch_apply_reason": _compact_text(str(record.get("patch_apply_reason") or ""), 260),
            })
        return top_records
