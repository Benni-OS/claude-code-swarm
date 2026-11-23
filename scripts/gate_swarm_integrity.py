"""CI/Release Gate: Verify all core swarm invariants end-to-end."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from claude_code_swarm import (
    Blackboard,
    HookEngine,
    LeaseArbiter,
    MergeGate,
    SwarmAuditLedger,
    SwarmBudget,
    SwarmInjector,
    WorktreeManager,
)
from claude_code_swarm.serve.mcp_server import SwarmMCPServer


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "gate-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "gate@benni-os.net"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Gate Bot"], cwd=repo, check=True)
        (repo / "main.py").write_text("print('ok')\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

        # 1. Injector idempotency
        inj = SwarmInjector(repo)
        inj.inject_all()
        inj.inject_all()
        assert (repo / ".claude" / "settings.json").exists()
        assert (repo / ".mcp.json").exists()

        # 2. Lease arbiter conflict prevention
        db = repo / ".swarm" / "swarm.db"
        arbiter = LeaseArbiter(db, repo_root=repo)
        assert arbiter.acquire_lease("src/a.py", "worker-1").granted
        assert not arbiter.acquire_lease("src/a.py", "worker-2").granted

        # 3. Blackboard DAG ordering
        bb = Blackboard(db)
        t1 = bb.post_task("Step 1")
        t2 = bb.post_task("Step 2", dependencies=[t1.task_id])
        assert bb.claim_task("worker-2", task_id=t2.task_id) is None
        assert bb.claim_task("worker-1", task_id=t1.task_id) is not None
        bb.resolve_task(t1.task_id, "worker-1", "done")
        assert bb.claim_task("worker-2", task_id=t2.task_id) is not None

        # 4. Budget circuit breaker
        budget = SwarmBudget(db, global_cap_usd=1.0, default_worker_cap_usd=0.5)
        assert budget.record_usage("worker-1", usd_delta=0.6).allowed is False

        # 5. Worktree + MergeGate + SHA-256 Audit Ledger
        mgr = WorktreeManager(repo)
        w1 = mgr.spawn_worker("worker-1")
        (w1.path / "feature.py").write_text("def feat() -> int:\n    return 42\n", encoding="utf-8")
        res = mgr.merge_worker("worker-1")
        assert res.merged and res.gate_report and res.gate_report.passed

        valid, count, msg = SwarmAuditLedger(repo / ".swarm" / "audit_ledger.jsonl").verify_chain()
        assert valid and count >= 1, msg

        # 6. MCP Server JSON-RPC 2.0
        mcp = SwarmMCPServer(repo)
        init_resp = mcp.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        assert init_resp and init_resp["result"]["serverInfo"]["name"] == "claude-code-swarm"

        # 7. HookEngine
        HookEngine(repo, worker_id="worker-1").dispatch("Stop", {"worker_id": "worker-1"})

    print("GATE PASSED: All claude-code-swarm integrity invariants verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
