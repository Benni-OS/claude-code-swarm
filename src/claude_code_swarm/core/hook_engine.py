"""Official Anthropic Claude Code hook dispatcher (PreToolUse, PostToolUse, Stop, SubagentStop)."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_code_swarm.core.lease_arbiter import LeaseArbiter
from claude_code_swarm.core.merge_gate import SwarmAuditLedger
from claude_code_swarm.core.swarm_budget import SwarmBudget


WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
BASH_WRITE_RE = re.compile(
    r"(?:>>?\s*|tee\s+(?:-a\s+)?|cp\s+\S+\s+|mv\s+\S+\s+|rm\s+(?:-[rf]+\s+)?|touch\s+)([^\s;&|]+)"
)


@dataclass(frozen=True)
class HookDecision:
    """Structured decision returned by HookEngine.dispatch()."""

    event_name: str
    allowed: bool
    reason: str = ""
    worker_id: str = "default"
    latency_ms: float = 0.0
    hook_output: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_name": self.event_name,
            "allowed": self.allowed,
            "reason": self.reason,
            "worker_id": self.worker_id,
            "latency_ms": round(self.latency_ms, 3),
            "hook_output": self.hook_output or {},
        }


class HookEngine:
    """Dispatches Claude Code lifecycle hooks to enforce file leases and swarm budgets."""

    def __init__(
        self,
        repo_root: Path | str,
        worker_id: str | None = None,
        state_dir: Path | str | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.state_dir = Path(state_dir).resolve() if state_dir else (self.repo_root / ".swarm")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.default_worker_id = worker_id or os.environ.get("CLAUDE_SWARM_WORKER_ID", "coordinator")
        self.db_path = self.state_dir / "swarm.db"
        self.arbiter = LeaseArbiter(self.db_path, repo_root=self.repo_root)
        self.budget = SwarmBudget(self.db_path)
        self.ledger = SwarmAuditLedger(self.state_dir / "audit_ledger.jsonl")

    def _resolve_worker(self, payload: dict[str, Any]) -> str:
        return str(
            payload.get("worker_id")
            or payload.get("session_id")
            or self.default_worker_id
        )

    @staticmethod
    def _extract_target_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
        paths: list[str] = []
        if tool_name in WRITE_TOOLS:
            for key in ("file_path", "path", "notebook_path"):
                val = tool_input.get(key)
                if isinstance(val, str) and val.strip():
                    paths.append(val.strip())
        elif tool_name == "Bash":
            cmd = str(tool_input.get("command", ""))
            for match in BASH_WRITE_RE.finditer(cmd):
                candidate = match.group(1).strip("'\"")
                if candidate and not candidate.startswith("-") and candidate != "/dev/null":
                    paths.append(candidate)
        return paths

    def dispatch(self, event_name: str, payload: dict[str, Any]) -> HookDecision:
        """Process a Claude Code hook event and return a HookDecision."""
        t0 = time.perf_counter()
        worker_id = self._resolve_worker(payload)

        if event_name == "PreToolUse":
            budget_status = self.budget.check_budget(worker_id)
            if not budget_status.allowed:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                self.ledger.append(
                    "hook_budget_block",
                    worker_id,
                    {"reason": budget_status.tripped_reason},
                )
                return HookDecision(
                    event_name=event_name,
                    allowed=False,
                    reason=budget_status.tripped_reason,
                    worker_id=worker_id,
                    latency_ms=latency_ms,
                    hook_output={
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": budget_status.tripped_reason,
                        }
                    },
                )

            tool_name = str(payload.get("tool_name", ""))
            tool_input = payload.get("tool_input") or {}
            if isinstance(tool_input, dict):
                targets = self._extract_target_paths(tool_name, tool_input)
                for target in targets:
                    lease = self.arbiter.acquire_lease(
                        target, worker_id=worker_id, mode="exclusive"
                    )
                    if not lease.granted:
                        latency_ms = (time.perf_counter() - t0) * 1000.0
                        self.ledger.append(
                            "hook_lease_block",
                            worker_id,
                            {
                                "tool": tool_name,
                                "path": lease.path,
                                "conflict_holder": lease.conflict_holder,
                            },
                        )
                        return HookDecision(
                            event_name=event_name,
                            allowed=False,
                            reason=lease.reason,
                            worker_id=worker_id,
                            latency_ms=latency_ms,
                            hook_output={
                                "hookSpecificOutput": {
                                    "hookEventName": "PreToolUse",
                                    "permissionDecision": "deny",
                                    "permissionDecisionReason": lease.reason,
                                }
                            },
                        )

            latency_ms = (time.perf_counter() - t0) * 1000.0
            return HookDecision(
                event_name=event_name,
                allowed=True,
                reason="PreToolUse approved",
                worker_id=worker_id,
                latency_ms=latency_ms,
                hook_output={
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "allow",
                    }
                },
            )

        if event_name == "PostToolUse":
            usage = payload.get("usage") or {}
            if isinstance(usage, dict):
                usd = float(usage.get("cost_usd", 0.0) or 0.0)
                in_tok = int(usage.get("input_tokens", 0) or 0)
                out_tok = int(usage.get("output_tokens", 0) or 0)
                if usd > 0 or in_tok > 0 or out_tok > 0:
                    self.budget.record_usage(
                        worker_id, usd_delta=usd, input_tokens=in_tok, output_tokens=out_tok
                    )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return HookDecision(
                event_name=event_name,
                allowed=True,
                reason="PostToolUse recorded",
                worker_id=worker_id,
                latency_ms=latency_ms,
            )

        if event_name in ("Stop", "SubagentStop"):
            released = self.arbiter.release_all(worker_id)
            self.ledger.append(
                f"hook_{event_name.lower()}",
                worker_id,
                {"leases_released": released},
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return HookDecision(
                event_name=event_name,
                allowed=True,
                reason=f"Released {released} lease(s) for '{worker_id}'",
                worker_id=worker_id,
                latency_ms=latency_ms,
            )

        latency_ms = (time.perf_counter() - t0) * 1000.0
        return HookDecision(
            event_name=event_name,
            allowed=True,
            reason=f"Unhandled hook event '{event_name}' passed through",
            worker_id=worker_id,
            latency_ms=latency_ms,
        )

    def handle_stdin(self, event_name: str) -> int:
        """Read Claude Code hook JSON from stdin, print decision JSON, and return exit code."""
        raw = sys.stdin.read().strip()
        payload = json.loads(raw) if raw else {}
        decision = self.dispatch(event_name, payload)
        if decision.hook_output:
            print(json.dumps(decision.hook_output))
        if not decision.allowed:
            print(decision.reason, file=sys.stderr)
            return 2
        return 0
