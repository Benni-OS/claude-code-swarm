"""Quickstart: Parallel Claude Code Swarm with DAG Blackboard & Worktree Isolation."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from claude_code_swarm import Blackboard, HookEngine, SwarmBudget, WorktreeManager


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "demo-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "swarm@benni-os.net"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Swarm Bot"], cwd=repo, check=True)
        (repo / "app.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        db_path = repo / ".swarm" / "swarm.db"
        bb = Blackboard(db_path)
        t1 = bb.post_task("Implement auth module", target_paths=["auth.py"], priority=10)
        t2 = bb.post_task("Add integration tests", dependencies=[t1.task_id], target_paths=["test_auth.py"])

        mgr = WorktreeManager(repo)
        w1 = mgr.spawn_worker("worker-1", task_id=t1.task_id)
        w2 = mgr.spawn_worker("worker-2", task_id=t2.task_id)
        print(f"Spawned {w1.worker_id} ({w1.branch}) and {w2.worker_id} ({w2.branch})")

        claimed_1 = bb.claim_task("worker-1")
        blocked_2 = bb.claim_task("worker-2")
        print(f"worker-1 claimed: {claimed_1.title if claimed_1 else None}")
        print(f"worker-2 before dependency resolved: {blocked_2}")

        (w1.path / "auth.py").write_text("def authenticate(user: str) -> bool:\n    return bool(user)\n", encoding="utf-8")
        bb.resolve_task(t1.task_id, "worker-1", "auth.py implemented")
        bb.post_handoff("worker-1", "worker-2", "Auth API ready", task_id=t1.task_id, artifacts=["auth.py"])

        claimed_2 = bb.claim_task("worker-2")
        print(f"worker-2 after dependency resolved: {claimed_2.title if claimed_2 else None}")

        merge_res = mgr.merge_worker("worker-1")
        print(f"worker-1 merge gate passed={merge_res.gate_report.passed if merge_res.gate_report else None}, merged={merge_res.merged}")

        budget = SwarmBudget(db_path)
        HookEngine(repo, worker_id="worker-1").dispatch("Stop", {"worker_id": "worker-1"})
        print(f"Remaining budget OK: {budget.check_budget().allowed}")


if __name__ == "__main__":
    main()
