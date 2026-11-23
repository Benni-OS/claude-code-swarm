"""Pre-merge AST conflict & syntax verifier + SHA-256 swarm audit ledger."""

from __future__ import annotations

import ast
import hashlib
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


GENESIS_HASH = "0" * 64
CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>")


@dataclass(frozen=True)
class GateIssue:
    """Single verification failure detected by MergeGate."""

    path: str
    kind: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "message": self.message,
            "line": self.line,
        }


@dataclass(frozen=True)
class GateReport:
    """Result of pre-merge verification."""

    passed: bool
    files_checked: int
    issues: list[GateIssue] = field(default_factory=list)
    tests_ran: bool = False
    tests_passed: bool | None = None
    test_output: str = ""
    ledger_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "files_checked": self.files_checked,
            "issues": [i.to_dict() for i in self.issues],
            "tests_ran": self.tests_ran,
            "tests_passed": self.tests_passed,
            "test_output": self.test_output,
            "ledger_hash": self.ledger_hash,
        }


class SwarmAuditLedger:
    """Append-only SHA-256 hash-chained audit ledger for swarm operations."""

    def __init__(self, ledger_path: Path | str) -> None:
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def _last_hash(self) -> str:
        if not self.ledger_path.exists():
            return GENESIS_HASH
        last_line = ""
        with self.ledger_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    last_line = stripped
        if not last_line:
            return GENESIS_HASH
        record = json.loads(last_line)
        return str(record["entry_hash"])

    @staticmethod
    def _compute_hash(record_body: dict[str, Any]) -> str:
        canonical = json.dumps(record_body, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def append(
        self,
        event_type: str,
        worker_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a cryptographically chained event to the JSONL ledger."""
        prev_hash = self._last_hash()
        body = {
            "timestamp": time.time(),
            "event_type": event_type,
            "worker_id": worker_id,
            "payload": payload or {},
            "prev_hash": prev_hash,
        }
        entry_hash = self._compute_hash(body)
        record = {**body, "entry_hash": entry_hash}
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def read_entries(self) -> list[dict[str, Any]]:
        """Read all ledger entries."""
        if not self.ledger_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with self.ledger_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    entries.append(json.loads(stripped))
        return entries

    def verify_chain(self) -> tuple[bool, int, str]:
        """Verify the entire SHA-256 hash chain from GENESIS_HASH."""
        entries = self.read_entries()
        expected_prev = GENESIS_HASH
        for idx, entry in enumerate(entries):
            if entry.get("prev_hash") != expected_prev:
                return (
                    False,
                    idx,
                    f"Chain break at entry {idx}: expected prev_hash={expected_prev}, got {entry.get('prev_hash')}",
                )
            body = {
                "timestamp": entry["timestamp"],
                "event_type": entry["event_type"],
                "worker_id": entry["worker_id"],
                "payload": entry["payload"],
                "prev_hash": entry["prev_hash"],
            }
            computed = self._compute_hash(body)
            if entry.get("entry_hash") != computed:
                return (
                    False,
                    idx,
                    f"Tampered entry at index {idx}: expected {computed}, got {entry.get('entry_hash')}",
                )
            expected_prev = computed
        return (True, len(entries), "OK")


class MergeGate:
    """Pre-merge AST conflict & syntax verifier + optional test runner."""

    def __init__(
        self,
        repo_root: Path | str,
        ledger: SwarmAuditLedger | None = None,
    ) -> None:
        self.repo_root = Path(repo_root)
        self.ledger = ledger or SwarmAuditLedger(self.repo_root / ".swarm" / "audit_ledger.jsonl")

    def verify_files(
        self,
        paths: list[Path | str],
        worker_id: str = "coordinator",
        run_tests: bool = False,
        test_cmd: list[str] | None = None,
        cwd: Path | str | None = None,
    ) -> GateReport:
        """Scan files for conflict markers, parse Python ASTs, and optionally run tests."""
        issues: list[GateIssue] = []
        checked = 0

        for raw in paths:
            file_path = Path(raw)
            if not file_path.is_absolute():
                file_path = (Path(cwd) if cwd else self.repo_root) / file_path
            if not file_path.exists() or not file_path.is_file():
                continue
            checked += 1

            try:
                text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue

            for lineno, line in enumerate(text.splitlines(), start=1):
                stripped = line.lstrip()
                if any(stripped.startswith(marker) for marker in CONFLICT_MARKERS):
                    issues.append(
                        GateIssue(
                            path=str(file_path),
                            kind="conflict_marker",
                            message=f"Git merge conflict marker '{stripped[:7]}' detected",
                            line=lineno,
                        )
                    )

            if file_path.suffix == ".py":
                try:
                    ast.parse(text, filename=str(file_path))
                except SyntaxError as exc:
                    issues.append(
                        GateIssue(
                            path=str(file_path),
                            kind="ast_syntax_error",
                            message=f"{exc.msg}",
                            line=exc.lineno,
                        )
                    )

        tests_ran = False
        tests_passed: bool | None = None
        test_output = ""
        if run_tests and not issues:
            tests_ran = True
            cmd = test_cmd or ["pytest", "-q"]
            proc = subprocess.run(
                cmd,
                cwd=str(cwd or self.repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            tests_passed = proc.returncode == 0
            test_output = (proc.stdout + "\n" + proc.stderr).strip()
            if not tests_passed:
                issues.append(
                    GateIssue(
                        path="<tests>",
                        kind="test_failure",
                        message=f"Test command {cmd!r} exited with code {proc.returncode}",
                    )
                )

        passed = len(issues) == 0
        entry = self.ledger.append(
            event_type="merge_gate",
            worker_id=worker_id,
            payload={
                "passed": passed,
                "files_checked": checked,
                "issue_count": len(issues),
                "tests_ran": tests_ran,
                "tests_passed": tests_passed,
            },
        )
        return GateReport(
            passed=passed,
            files_checked=checked,
            issues=issues,
            tests_ran=tests_ran,
            tests_passed=tests_passed,
            test_output=test_output,
            ledger_hash=entry["entry_hash"],
        )

    def verify_worktree(
        self,
        worktree_path: Path | str,
        worker_id: str = "worker",
        run_tests: bool = False,
        test_cmd: list[str] | None = None,
    ) -> GateReport:
        """Verify all non-ignored source files inside a worker worktree."""
        wt = Path(worktree_path)
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(wt),
            capture_output=True,
            text=True,
            check=False,
        )
        changed: list[Path | str] = []
        if proc.returncode == 0 and proc.stdout.strip():
            for line in proc.stdout.splitlines():
                if len(line) >= 4:
                    rel = line[3:].strip().strip('"')
                    if " -> " in rel:
                        rel = rel.split(" -> ", 1)[1]
                    changed.append(wt / rel)

        if not changed:
            changed = [
                p
                for p in wt.rglob("*")
                if p.is_file()
                and ".git" not in p.relative_to(wt).parts
                and ".swarm" not in p.relative_to(wt).parts
            ]

        return self.verify_files(
            changed,
            worker_id=worker_id,
            run_tests=run_tests,
            test_cmd=test_cmd,
            cwd=wt,
        )
