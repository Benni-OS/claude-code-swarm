"""Real git worktree lifecycle manager with safe swarm/<worker_id> branch isolation."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_code_swarm.core.merge_gate import GateReport, MergeGate


WORKER_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass(frozen=True)
class WorkerWorktree:
    """Represents an isolated git worktree for a parallel Claude Code worker."""

    worker_id: str
    branch: str
    path: Path
    base_ref: str
    task_id: str | None = None
    created_at: float = 0.0
    head_sha: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "branch": self.branch,
            "path": str(self.path),
            "base_ref": self.base_ref,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "head_sha": self.head_sha,
        }


@dataclass(frozen=True)
class MergeResult:
    """Outcome of merging a worker's isolated branch back into the target branch."""

    merged: bool
    worker_id: str
    branch: str
    target_branch: str
    gate_report: GateReport | None = None
    commit_sha: str = ""
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "merged": self.merged,
            "worker_id": self.worker_id,
            "branch": self.branch,
            "target_branch": self.target_branch,
            "gate_report": self.gate_report.to_dict() if self.gate_report else None,
            "commit_sha": self.commit_sha,
            "message": self.message,
        }


class WorktreeManager:
    """Manages isolated `git worktree` sandboxes under `.swarm/worktrees/<worker_id>`."""

    def __init__(
        self,
        repo_root: Path | str,
        state_dir: Path | str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.state_dir = Path(state_dir).resolve() if state_dir else (self.repo_root / ".swarm")
        self.worktrees_dir = self.state_dir / "worktrees"
        self.registry_path = self.state_dir / "workers.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_worker_id(worker_id: str) -> None:
        if not WORKER_ID_RE.match(worker_id):
            raise ValueError(
                f"Invalid worker_id {worker_id!r}; must match {WORKER_ID_RE.pattern}"
            )

    def _git(self, args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo_root),
            capture_output=True,
            text=True,
            check=False,
        )

    def _load_registry(self) -> dict[str, dict[str, Any]]:
        if not self.registry_path.exists():
            return {}
        return json.loads(self.registry_path.read_text(encoding="utf-8"))

    def _save_registry(self, data: dict[str, dict[str, Any]]) -> None:
        self.registry_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def current_branch(self) -> str:
        proc = self._git(["rev-parse", "--abbrev-ref", "HEAD"])
        if proc.returncode != 0:
            return "main"
        return proc.stdout.strip() or "main"

    def spawn_worker(
        self,
        worker_id: str,
        base_ref: str = "HEAD",
        task_id: str | None = None,
    ) -> WorkerWorktree:
        """Create an isolated git worktree on branch `swarm/<worker_id>`."""
        self._validate_worker_id(worker_id)
        branch = f"swarm/{worker_id}"
        wt_path = self.worktrees_dir / worker_id

        if wt_path.exists():
            raise FileExistsError(f"Worktree for worker '{worker_id}' already exists at {wt_path}")

        proc = self._git(["worktree", "add", "-b", branch, str(wt_path), base_ref])
        if proc.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {proc.stderr.strip()}")

        sha_proc = self._git(["rev-parse", "HEAD"], cwd=wt_path)
        head_sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else ""

        record = WorkerWorktree(
            worker_id=worker_id,
            branch=branch,
            path=wt_path,
            base_ref=base_ref,
            task_id=task_id,
            created_at=time.time(),
            head_sha=head_sha,
        )
        reg = self._load_registry()
        reg[worker_id] = record.to_dict()
        self._save_registry(reg)
        return record

    def list_workers(self) -> list[WorkerWorktree]:
        """List active swarm worker worktrees from git and local registry."""
        proc = self._git(["worktree", "list", "--porcelain"])
        reg = self._load_registry()
        workers: list[WorkerWorktree] = []

        if proc.returncode != 0:
            return workers

        blocks = proc.stdout.strip().split("\n\n")
        for block in blocks:
            lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
            wt_path: Path | None = None
            head_sha = ""
            branch = ""
            for ln in lines:
                if ln.startswith("worktree "):
                    wt_path = Path(ln[len("worktree "):]).resolve()
                elif ln.startswith("HEAD "):
                    head_sha = ln[len("HEAD "):]
                elif ln.startswith("branch refs/heads/"):
                    branch = ln[len("branch refs/heads/"):]

            if wt_path is None or not branch.startswith("swarm/"):
                continue

            worker_id = branch[len("swarm/"):]
            meta = reg.get(worker_id, {})
            workers.append(
                WorkerWorktree(
                    worker_id=worker_id,
                    branch=branch,
                    path=wt_path,
                    base_ref=str(meta.get("base_ref", "HEAD")),
                    task_id=meta.get("task_id"),
                    created_at=float(meta.get("created_at", 0.0)),
                    head_sha=head_sha,
                )
            )
        return workers

    def diff_worker(self, worker_id: str, base_ref: str | None = None) -> str:
        """Return unified git diff for the worker's worktree (committed + working tree)."""
        self._validate_worker_id(worker_id)
        reg = self._load_registry()
        meta = reg.get(worker_id, {})
        wt_path = Path(meta.get("path", self.worktrees_dir / worker_id))
        if not wt_path.exists():
            raise FileNotFoundError(f"Worktree for worker '{worker_id}' not found")

        ref = base_ref or str(meta.get("base_ref", "HEAD"))
        committed = self._git(["diff", f"{ref}...HEAD"], cwd=wt_path).stdout
        working = self._git(["diff", "HEAD"], cwd=wt_path).stdout
        combined = "\n".join(part for part in (committed.strip(), working.strip()) if part)
        return combined

    def merge_worker(
        self,
        worker_id: str,
        target_branch: str | None = None,
        commit_message: str | None = None,
        run_gate: bool = True,
        run_tests: bool = False,
    ) -> MergeResult:
        """Verify worker changes through MergeGate and merge `swarm/<worker_id>`."""
        self._validate_worker_id(worker_id)
        branch = f"swarm/{worker_id}"
        wt_path = self.worktrees_dir / worker_id
        target = target_branch or self.current_branch()

        if not wt_path.exists():
            return MergeResult(
                merged=False,
                worker_id=worker_id,
                branch=branch,
                target_branch=target,
                message=f"Worktree not found for '{worker_id}'",
            )

        status_proc = self._git(["status", "--porcelain"], cwd=wt_path)
        if status_proc.returncode == 0 and status_proc.stdout.strip():
            self._git(["add", "-A"], cwd=wt_path)
            msg = commit_message or f"swarm({worker_id}): commit worker changes"
            commit_proc = self._git(["commit", "-m", msg], cwd=wt_path)
            if commit_proc.returncode != 0:
                return MergeResult(
                    merged=False,
                    worker_id=worker_id,
                    branch=branch,
                    target_branch=target,
                    message=f"Failed to commit worker changes: {commit_proc.stderr.strip()}",
                )

        gate_report: GateReport | None = None
        if run_gate:
            gate = MergeGate(self.repo_root)
            gate_report = gate.verify_worktree(
                wt_path,
                worker_id=worker_id,
                run_tests=run_tests,
            )
            if not gate_report.passed:
                return MergeResult(
                    merged=False,
                    worker_id=worker_id,
                    branch=branch,
                    target_branch=target,
                    gate_report=gate_report,
                    message="Pre-merge AST/conflict gate failed",
                )

        if target != self.current_branch():
            co = self._git(["checkout", target])
            if co.returncode != 0:
                return MergeResult(
                    merged=False,
                    worker_id=worker_id,
                    branch=branch,
                    target_branch=target,
                    gate_report=gate_report,
                    message=f"Failed to checkout target branch '{target}': {co.stderr.strip()}",
                )

        merge_proc = self._git(
            ["merge", "--no-ff", branch, "-m", f"Merge {branch} into {target}"]
        )
        if merge_proc.returncode != 0:
            self._git(["merge", "--abort"])
            return MergeResult(
                merged=False,
                worker_id=worker_id,
                branch=branch,
                target_branch=target,
                gate_report=gate_report,
                message=f"Git merge conflict: {merge_proc.stderr.strip()}",
            )

        sha_proc = self._git(["rev-parse", "HEAD"])
        commit_sha = sha_proc.stdout.strip() if sha_proc.returncode == 0 else ""
        return MergeResult(
            merged=True,
            worker_id=worker_id,
            branch=branch,
            target_branch=target,
            gate_report=gate_report,
            commit_sha=commit_sha,
            message=f"Merged {branch} into {target}",
        )

    def cleanup_worker(
        self,
        worker_id: str,
        force: bool = False,
        delete_branch: bool = True,
    ) -> bool:
        """Remove a worker's git worktree and optionally delete `swarm/<worker_id>`."""
        self._validate_worker_id(worker_id)
        branch = f"swarm/{worker_id}"
        wt_path = self.worktrees_dir / worker_id

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(wt_path))
        proc = self._git(args)
        if proc.returncode != 0 and wt_path.exists() and force:
            shutil.rmtree(wt_path, ignore_errors=True)
            self._git(["worktree", "prune"])

        if delete_branch:
            self._git(["branch", "-D" if force else "-d", branch])

        reg = self._load_registry()
        if worker_id in reg:
            reg.pop(worker_id, None)
            self._save_registry(reg)
        return not wt_path.exists()
