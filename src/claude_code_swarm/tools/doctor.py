"""Full Claude Code swarm environment & lease pipeline diagnostic (`claude-swarm doctor`)."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from claude_code_swarm.core.lease_arbiter import LeaseArbiter
from claude_code_swarm.serve.health_monitor import HealthMonitor


def _check_git() -> dict[str, Any]:
    proc = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False)
    return {
        "ok": proc.returncode == 0,
        "version": proc.stdout.strip() or proc.stderr.strip(),
    }


def _check_sqlite_wal_probe() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "probe.db"
        arbiter = LeaseArbiter(db_path)
        res1 = arbiter.acquire_lease("src/app.py", "probe-1", mode="exclusive")
        res2 = arbiter.acquire_lease("src/app.py", "probe-2", mode="exclusive")
        released = arbiter.release_lease("src/app.py", "probe-1")
        return {
            "ok": res1.granted and (not res2.granted) and released,
            "sqlite_version": sqlite3.sqlite_version,
            "conflict_blocked": not res2.granted,
        }


def run_doctor(repo_root: Path | str = ".", as_json: bool = False) -> dict[str, Any]:
    """Run full environment, SQLite WAL lease probe, and repository health checks."""
    root = Path(repo_root).resolve()
    git_info = _check_git()
    wal_probe = _check_sqlite_wal_probe()
    health = HealthMonitor(root).report()

    settings_ok = (root / ".claude" / "settings.json").exists()
    mcp_ok = (root / ".mcp.json").exists()
    claude_md_ok = (root / "CLAUDE.md").exists()

    overall_ok = (
        sys.version_info >= (3, 10)
        and git_info["ok"]
        and wal_probe["ok"]
        and health["healthy"]
    )

    report = {
        "ok": overall_ok,
        "python": sys.version.split()[0],
        "git": git_info,
        "sqlite_wal_probe": wal_probe,
        "repo_root": str(root),
        "injected": {
            "claude_settings": settings_ok,
            "mcp_json": mcp_ok,
            "claude_md": claude_md_ok,
        },
        "health": health,
    }

    if as_json:
        print(json.dumps(report, indent=2))
    else:
        status_icon = "[PASS]" if overall_ok else "[FAIL]"
        print(f"claude-code-swarm doctor {status_icon}")
        print(f"  Python          : {report['python']}")
        print(f"  Git             : {git_info['version']} ({'ok' if git_info['ok'] else 'missing'})")
        print(
            f"  SQLite WAL Probe: sqlite {wal_probe['sqlite_version']} | "
            f"conflict_blocked={wal_probe['conflict_blocked']} ({'ok' if wal_probe['ok'] else 'fail'})"
        )
        print(
            f"  Repo Injection  : settings={settings_ok} | mcp={mcp_ok} | CLAUDE.md={claude_md_ok}"
        )
        print(
            f"  Audit Ledger    : entries={health['audit_ledger']['entries']} | "
            f"valid={health['audit_ledger']['valid']}"
        )
        print(f"  Active Worktrees: {health['worktrees']['active_workers']}")
    return report
