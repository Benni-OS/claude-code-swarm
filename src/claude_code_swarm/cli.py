"""CLI entrypoint (`claude-swarm`) for claude-code-swarm."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from claude_code_swarm.core.blackboard import Blackboard
from claude_code_swarm.core.hook_engine import HookEngine
from claude_code_swarm.core.injector import SwarmInjector
from claude_code_swarm.core.lease_arbiter import LeaseArbiter
from claude_code_swarm.core.merge_gate import SwarmAuditLedger
from claude_code_swarm.core.swarm_budget import SwarmBudget
from claude_code_swarm.core.worktree_manager import WorktreeManager
from claude_code_swarm.serve.mcp_server import SwarmMCPServer
from claude_code_swarm.tools.bench_matrix import run_matrix
from claude_code_swarm.tools.doctor import run_doctor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-swarm",
        description="Zero-dependency multi-agent orchestration kernel for parallel Claude Code sessions.",
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Target git repository root (default: current directory)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # init
    p_init = sub.add_parser("init", help="Idempotently inject Claude Code hooks, .mcp.json, CLAUDE.md, and /swarm-* commands")
    p_init.add_argument("--json", action="store_true", help="Output JSON summary")

    # spawn
    p_spawn = sub.add_parser("spawn", help="Spawn an isolated git worktree for a worker")
    p_spawn.add_argument("worker_id", help="Unique worker ID (e.g. worker-1)")
    p_spawn.add_argument("--base", default="HEAD", help="Base git ref (default: HEAD)")
    p_spawn.add_argument("--task-id", default=None, help="Optional blackboard task ID")
    p_spawn.add_argument("--json", action="store_true", help="Output JSON")

    # status
    p_status = sub.add_parser("status", help="Show active worktrees, SQLite WAL leases, tasks, and USD budget")
    p_status.add_argument("--json", action="store_true", help="Output JSON")

    # merge
    p_merge = sub.add_parser("merge", help="Run pre-merge AST/conflict gate and merge swarm/<worker_id>")
    p_merge.add_argument("worker_id", help="Worker ID to merge")
    p_merge.add_argument("--target", default=None, help="Target branch (default: current branch)")
    p_merge.add_argument("--no-gate", action="store_true", help="Skip pre-merge AST/conflict gate")
    p_merge.add_argument("--run-tests", action="store_true", help="Run pytest inside worktree before merging")
    p_merge.add_argument("--json", action="store_true", help="Output JSON")

    # cleanup
    p_cleanup = sub.add_parser("cleanup", help="Remove a worker worktree and branch")
    p_cleanup.add_argument("worker_id", help="Worker ID to clean up")
    p_cleanup.add_argument("--force", action="store_true", help="Force removal of dirty worktree/branch")
    p_cleanup.add_argument("--keep-branch", action="store_true", help="Keep swarm/<worker_id> branch")

    # hook
    p_hook = sub.add_parser("hook", help="Dispatch an Anthropic Claude Code hook event from stdin")
    p_hook.add_argument("event", choices=["PreToolUse", "PostToolUse", "Stop", "SubagentStop"])
    p_hook.add_argument("--worker-id", default=None, help="Override worker ID")

    # mcp
    sub.add_parser("mcp", help="Run the zero-dependency JSON-RPC 2.0 stdio MCP server")

    # doctor
    p_doc = sub.add_parser("doctor", help="Run full swarm environment & SQLite WAL lease diagnostics")
    p_doc.add_argument("--json", action="store_true", help="Output JSON")

    # matrix
    p_mat = sub.add_parser("matrix", help="Run multi-worker lease contention & hook latency benchmark matrix")
    p_mat.add_argument("--workers", nargs="+", type=int, default=[2, 4, 8], help="Worker counts to benchmark")
    p_mat.add_argument("--ops", type=int, default=40, help="Operations per worker")
    p_mat.add_argument("--json", action="store_true", help="Output JSON")

    # audit
    p_aud = sub.add_parser("audit", help="Verify the SHA-256 swarm audit ledger")
    p_aud.add_argument("--json", action="store_true", help="Output JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = Path(args.repo).resolve()
    state_dir = repo_root / ".swarm"

    if args.command == "init":
        res = SwarmInjector(repo_root).inject_all()
        if args.json:
            print(json.dumps(res.to_dict(), indent=2))
        else:
            print(f"Initialized claude-code-swarm in {repo_root}")
            print(f"  .claude/settings.json : {res.settings_path}")
            print(f"  .mcp.json             : {res.mcp_path}")
            print(f"  CLAUDE.md             : {res.claude_md_path}")
            print(f"  Slash commands        : {len(res.command_paths)} installed")
        return 0

    if args.command == "spawn":
        mgr = WorktreeManager(repo_root, state_dir=state_dir)
        wt = mgr.spawn_worker(args.worker_id, base_ref=args.base, task_id=args.task_id)
        if args.json:
            print(json.dumps(wt.to_dict(), indent=2))
        else:
            print(f"Spawned worker '{wt.worker_id}' on branch '{wt.branch}' at {wt.path}")
        return 0

    if args.command == "status":
        db_path = state_dir / "swarm.db"
        workers = WorktreeManager(repo_root, state_dir=state_dir).list_workers()
        leases = LeaseArbiter(db_path, repo_root=repo_root).list_leases()
        tasks = Blackboard(db_path).list_tasks()
        budget = SwarmBudget(db_path).check_budget()
        payload = {
            "workers": [w.to_dict() for w in workers],
            "leases": [l.to_dict() for l in leases],
            "tasks": [t.to_dict() for t in tasks],
            "budget": budget.to_dict(),
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Swarm Status ({repo_root})")
            print(f"  Active Workers : {len(workers)}")
            print(f"  Active Leases  : {len(leases)}")
            print(f"  Tasks          : {len(tasks)}")
            print(
                f"  Budget         : ${budget.global_usd_spent:.4f} / ${budget.global_cap_usd:.2f} "
                f"({'OK' if budget.allowed else 'TRIPPED'})"
            )
        return 0

    if args.command == "merge":
        mgr = WorktreeManager(repo_root, state_dir=state_dir)
        res = mgr.merge_worker(
            args.worker_id,
            target_branch=args.target,
            run_gate=not args.no_gate,
            run_tests=args.run_tests,
        )
        if args.json:
            print(json.dumps(res.to_dict(), indent=2))
        else:
            print(f"{'MERGED' if res.merged else 'BLOCKED'}: {res.message}")
        return 0 if res.merged else 1

    if args.command == "cleanup":
        mgr = WorktreeManager(repo_root, state_dir=state_dir)
        ok = mgr.cleanup_worker(
            args.worker_id,
            force=args.force,
            delete_branch=not args.keep_branch,
        )
        print(f"Cleanup '{args.worker_id}': {'ok' if ok else 'failed'}")
        return 0 if ok else 1

    if args.command == "hook":
        engine = HookEngine(repo_root, worker_id=args.worker_id, state_dir=state_dir)
        return engine.handle_stdin(args.event)

    if args.command == "mcp":
        SwarmMCPServer(repo_root, state_dir=state_dir).serve_stdio()
        return 0

    if args.command == "doctor":
        report = run_doctor(repo_root, as_json=args.json)
        return 0 if report["ok"] else 1

    if args.command == "matrix":
        run_matrix(workers_list=args.workers, ops_per_worker=args.ops, as_json=args.json)
        return 0

    if args.command == "audit":
        ledger = SwarmAuditLedger(state_dir / "audit_ledger.jsonl")
        valid, count, msg = ledger.verify_chain()
        payload = {"valid": valid, "entries": count, "message": msg}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print(f"Audit Ledger: {'VALID' if valid else 'INVALID'} ({count} entries) — {msg}")
        return 0 if valid else 1

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
