"""Quickstart: SQLite3 WAL Lease Conflict Prevention Demo."""

from __future__ import annotations

import tempfile
from pathlib import Path

from claude_code_swarm import HookEngine, LeaseArbiter


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        engine_1 = HookEngine(repo, worker_id="worker-1")
        engine_2 = HookEngine(repo, worker_id="worker-2")

        d1 = engine_1.dispatch(
            "PreToolUse",
            {"worker_id": "worker-1", "tool_name": "Edit", "tool_input": {"file_path": "src/core/engine.py"}},
        )
        print(f"[worker-1] Edit src/core/engine.py -> allowed={d1.allowed} ({d1.reason})")

        d2 = engine_2.dispatch(
            "PreToolUse",
            {"worker_id": "worker-2", "tool_name": "Write", "tool_input": {"file_path": "src/core/engine.py"}},
        )
        print(f"[worker-2] Write src/core/engine.py -> allowed={d2.allowed} ({d2.reason})")

        engine_1.dispatch("Stop", {"worker_id": "worker-1"})
        d2_retry = engine_2.dispatch(
            "PreToolUse",
            {"worker_id": "worker-2", "tool_name": "Write", "tool_input": {"file_path": "src/core/engine.py"}},
        )
        print(f"[worker-2] Retry after worker-1 Stop -> allowed={d2_retry.allowed}")

        arbiter = LeaseArbiter(repo / ".swarm" / "swarm.db", repo_root=repo)
        print("Arbiter metrics:", arbiter.metrics())


if __name__ == "__main__":
    main()
