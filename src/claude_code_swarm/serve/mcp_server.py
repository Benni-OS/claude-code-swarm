"""Zero-dependency JSON-RPC 2.0 stdio MCP server for claude-code-swarm."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, TextIO

from claude_code_swarm.core.blackboard import Blackboard
from claude_code_swarm.core.lease_arbiter import LeaseArbiter
from claude_code_swarm.core.merge_gate import MergeGate
from claude_code_swarm.core.swarm_budget import SwarmBudget
from claude_code_swarm.core.worktree_manager import WorktreeManager
from claude_code_swarm.serve.telemetry import SwarmTelemetry


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "swarm_claim_task",
        "description": "Atomically claim the next ready task (all DAG dependencies resolved) for a worker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "description": "Unique worker identifier"},
                "task_id": {"type": "string", "description": "Optional specific task ID to claim"},
            },
            "required": ["worker_id"],
        },
    },
    {
        "name": "swarm_acquire_lock",
        "description": "Acquire an exclusive or shared SQLite3 WAL file/directory lease.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory path to lock"},
                "worker_id": {"type": "string", "description": "Worker requesting the lease"},
                "mode": {"type": "string", "enum": ["exclusive", "shared"], "default": "exclusive"},
                "ttl_seconds": {"type": "number", "default": 300.0},
            },
            "required": ["path", "worker_id"],
        },
    },
    {
        "name": "swarm_release_lock",
        "description": "Release a held file/directory lease for a worker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File or directory path to unlock"},
                "worker_id": {"type": "string", "description": "Worker releasing the lease"},
            },
            "required": ["path", "worker_id"],
        },
    },
    {
        "name": "swarm_post_handoff",
        "description": "Post a typed handoff summary and artifact list between workers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_worker": {"type": "string"},
                "to_worker": {"type": "string"},
                "summary": {"type": "string"},
                "task_id": {"type": "string"},
                "artifacts": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["from_worker", "to_worker", "summary"],
        },
    },
    {
        "name": "swarm_budget_check",
        "description": "Check global swarm USD/token cap and optional worker quota.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "description": "Optional worker ID"},
            },
        },
    },
    {
        "name": "swarm_merge_ready",
        "description": "Run the pre-merge AST & conflict-marker gate on a worker worktree or file list.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "run_tests": {"type": "boolean", "default": False},
            },
            "required": ["worker_id"],
        },
    },
    {
        "name": "swarm_status",
        "description": "Return full swarm status: active worktrees, leases, tasks, budget, and telemetry.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class SwarmMCPServer:
    """Zero-dependency JSON-RPC 2.0 stdio MCP server."""

    def __init__(self, repo_root: Path | str, state_dir: Path | str | None = None) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.state_dir = Path(state_dir).resolve() if state_dir else (self.repo_root / ".swarm")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "swarm.db"
        self.arbiter = LeaseArbiter(self.db_path, repo_root=self.repo_root)
        self.blackboard = Blackboard(self.db_path)
        self.budget = SwarmBudget(self.db_path)
        self.worktrees = WorktreeManager(self.repo_root, state_dir=self.state_dir)
        self.gate = MergeGate(self.repo_root)
        self.telemetry = SwarmTelemetry()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatch an MCP tool call and return a JSON-serializable result dict."""
        t0 = time.perf_counter()
        try:
            if name == "swarm_claim_task":
                task = self.blackboard.claim_task(
                    worker_id=str(arguments["worker_id"]),
                    task_id=arguments.get("task_id"),
                )
                return {"claimed": task is not None, "task": task.to_dict() if task else None}

            if name == "swarm_acquire_lock":
                res = self.arbiter.acquire_lease(
                    path=str(arguments["path"]),
                    worker_id=str(arguments["worker_id"]),
                    mode=str(arguments.get("mode", "exclusive")),
                    ttl_seconds=float(arguments.get("ttl_seconds", 300.0)),
                )
                if not res.granted:
                    self.telemetry.record_conflict_prevented()
                return res.to_dict()

            if name == "swarm_release_lock":
                released = self.arbiter.release_lease(
                    path=str(arguments["path"]),
                    worker_id=str(arguments["worker_id"]),
                )
                return {"released": released, "path": str(arguments["path"])}

            if name == "swarm_post_handoff":
                handoff = self.blackboard.post_handoff(
                    from_worker=str(arguments["from_worker"]),
                    to_worker=str(arguments["to_worker"]),
                    summary=str(arguments["summary"]),
                    task_id=arguments.get("task_id"),
                    artifacts=list(arguments.get("artifacts") or []),
                )
                return handoff.to_dict()

            if name == "swarm_budget_check":
                status = self.budget.check_budget(arguments.get("worker_id"))
                return status.to_dict()

            if name == "swarm_merge_ready":
                wid = str(arguments["worker_id"])
                wt_path = self.worktrees.worktrees_dir / wid
                if not wt_path.exists():
                    return {"passed": False, "error": f"Worktree not found for '{wid}'"}
                report = self.gate.verify_worktree(
                    wt_path,
                    worker_id=wid,
                    run_tests=bool(arguments.get("run_tests", False)),
                )
                return report.to_dict()

            if name == "swarm_status":
                return {
                    "workers": [w.to_dict() for w in self.worktrees.list_workers()],
                    "leases": [l.to_dict() for l in self.arbiter.list_leases()],
                    "tasks": [t.to_dict() for t in self.blackboard.list_tasks()],
                    "budget": self.budget.check_budget().to_dict(),
                    "telemetry": self.telemetry.snapshot().to_dict(),
                }

            raise KeyError(f"Unknown MCP tool: {name}")
        finally:
            self.telemetry.record_latency((time.perf_counter() - t0) * 1000.0)

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Handle a single JSON-RPC 2.0 request object."""
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}

        if method == "notifications/initialized":
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "claude-code-swarm",
                        "version": "0.1.0",
                    },
                },
            }

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOL_DEFINITIONS},
            }

        if method == "tools/call":
            tool_name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                result = self.call_tool(tool_name, args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                        "isError": False,
                    },
                }
            except Exception as exc:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {exc}"}],
                        "isError": True,
                    },
                }

        if req_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def serve_stdio(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        """Run the blocking line-delimited JSON-RPC 2.0 stdio loop."""
        in_stream = stdin or sys.stdin
        out_stream = stdout or sys.stdout
        for line in in_stream:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                req = json.loads(stripped)
            except json.JSONDecodeError as exc:
                err = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }
                out_stream.write(json.dumps(err) + "\n")
                out_stream.flush()
                continue
            resp = self.handle_request(req)
            if resp is not None:
                out_stream.write(json.dumps(resp) + "\n")
                out_stream.flush()
