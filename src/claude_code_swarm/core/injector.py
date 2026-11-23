"""Idempotent injector for .claude/settings.json, .mcp.json, CLAUDE.md, and /swarm-* commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SWARM_CLAUDE_START = "<!-- CLAUDE-CODE-SWARM:START -->"
SWARM_CLAUDE_END = "<!-- CLAUDE-CODE-SWARM:END -->"

SWARM_CLAUDE_BLOCK = f"""{SWARM_CLAUDE_START}
## Multi-Agent Swarm Governance (`claude-code-swarm`)

This repository is orchestrated by **claude-code-swarm**:
- **Isolated Worktrees**: Parallel workers operate in `.swarm/worktrees/<worker_id>` on branch `swarm/<worker_id>`.
- **SQLite3 WAL File Leases**: `PreToolUse` hooks automatically acquire exclusive file leases before `Write`/`Edit`/`Bash` mutations. Never bypass a lease block.
- **Shared DAG Blackboard**: Claim ready tasks via MCP `swarm_claim_task` and pass structured handoffs via `swarm_post_handoff`.
- **Pre-Merge AST Gate**: All worker branches must pass conflict-marker, Python AST, and test verification before merging.
{SWARM_CLAUDE_END}"""

SLASH_COMMANDS: dict[str, str] = {
    "swarm-spawn.md": """---
description: Spawn an isolated parallel Claude Code worker worktree and assign a task
---
Run `claude-swarm spawn $ARGUMENTS` to create an isolated `git worktree` on branch `swarm/<worker_id>` and register it in the swarm blackboard.
""",
    "swarm-status.md": """---
description: Inspect active swarm worktrees, SQLite WAL file leases, tasks, and USD budget
---
Run `claude-swarm status` (or call the `swarm_status` MCP tool) to view active workers, held file leases, task DAG progress, and remaining USD budget.
""",
    "swarm-merge.md": """---
description: Run the pre-merge AST/conflict gate and merge a worker branch
---
Run `claude-swarm merge $ARGUMENTS` to validate Python ASTs, scan for merge conflict markers, append to the SHA-256 audit ledger, and merge `swarm/<worker_id>`.
""",
}


@dataclass(frozen=True)
class InjectionResult:
    """Summary of files created or updated by SwarmInjector."""

    settings_path: Path
    mcp_path: Path
    claude_md_path: Path
    command_paths: list[Path]

    def to_dict(self) -> dict[str, Any]:
        return {
            "settings_path": str(self.settings_path),
            "mcp_path": str(self.mcp_path),
            "claude_md_path": str(self.claude_md_path),
            "command_paths": [str(p) for p in self.command_paths],
        }


class SwarmInjector:
    """Idempotently configures Claude Code hooks, MCP server, CLAUDE.md, and slash commands."""

    def __init__(self, repo_root: Path | str) -> None:
        self.repo_root = Path(repo_root).resolve()

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        return json.loads(text)

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def inject_settings(self) -> Path:
        """Idempotently merge claude-swarm hooks into .claude/settings.json."""
        settings_path = self.repo_root / ".claude" / "settings.json"
        data = self._read_json(settings_path)
        hooks = data.setdefault("hooks", {})

        desired_hooks = {
            "PreToolUse": ("Write|Edit|MultiEdit|Bash", "claude-swarm hook PreToolUse"),
            "PostToolUse": ("Write|Edit|MultiEdit|Bash", "claude-swarm hook PostToolUse"),
            "Stop": (None, "claude-swarm hook Stop"),
            "SubagentStop": (None, "claude-swarm hook SubagentStop"),
        }

        for event, (matcher, command) in desired_hooks.items():
            entries = hooks.setdefault(event, [])
            already_present = any(
                any(
                    h.get("command") == command
                    for h in entry.get("hooks", [])
                    if isinstance(h, dict)
                )
                for entry in entries
                if isinstance(entry, dict)
            )
            if not already_present:
                new_entry: dict[str, Any] = {
                    "hooks": [{"type": "command", "command": command}]
                }
                if matcher is not None:
                    new_entry["matcher"] = matcher
                entries.append(new_entry)

        self._write_json(settings_path, data)
        return settings_path

    def inject_mcp(self) -> Path:
        """Idempotently register claude-code-swarm in .mcp.json."""
        mcp_path = self.repo_root / ".mcp.json"
        data = self._read_json(mcp_path)
        servers = data.setdefault("mcpServers", {})
        servers["claude-code-swarm"] = {
            "command": "claude-swarm",
            "args": ["mcp"],
        }
        self._write_json(mcp_path, data)
        return mcp_path

    def inject_claude_md(self) -> Path:
        """Idempotently insert or update the swarm section in CLAUDE.md."""
        claude_md = self.repo_root / "CLAUDE.md"
        if not claude_md.exists():
            claude_md.write_text(SWARM_CLAUDE_BLOCK + "\n", encoding="utf-8")
            return claude_md

        content = claude_md.read_text(encoding="utf-8")
        if SWARM_CLAUDE_START in content and SWARM_CLAUDE_END in content:
            before = content.split(SWARM_CLAUDE_START, 1)[0]
            after = content.split(SWARM_CLAUDE_END, 1)[1]
            updated = before + SWARM_CLAUDE_BLOCK + after
        else:
            sep = "\n\n" if content and not content.endswith("\n\n") else ""
            updated = content.rstrip() + sep + SWARM_CLAUDE_BLOCK + "\n"

        claude_md.write_text(updated, encoding="utf-8")
        return claude_md

    def inject_commands(self) -> list[Path]:
        """Idempotently write /swarm-* slash commands under .claude/commands/."""
        cmd_dir = self.repo_root / ".claude" / "commands"
        cmd_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for filename, body in SLASH_COMMANDS.items():
            target = cmd_dir / filename
            target.write_text(body, encoding="utf-8")
            written.append(target)
        return written

    def inject_all(self) -> InjectionResult:
        """Run all idempotent injections into the target repository."""
        return InjectionResult(
            settings_path=self.inject_settings(),
            mcp_path=self.inject_mcp(),
            claude_md_path=self.inject_claude_md(),
            command_paths=self.inject_commands(),
        )
