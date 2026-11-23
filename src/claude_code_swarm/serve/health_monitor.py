"""Worktree, SQLite3 WAL & SHA-256 audit ledger health monitor."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from claude_code_swarm.core.merge_gate import SwarmAuditLedger
from claude_code_swarm.core.worktree_manager import WorktreeManager


class HealthMonitor:
    """Inspects git worktrees, SQLite3 WAL health, and SHA-256 ledger integrity."""

    def __init__(self, repo_root: Path | str, state_dir: Path | str | None = None) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.state_dir = Path(state_dir).resolve() if state_dir else (self.repo_root / ".swarm")

    def check_sqlite_wal(self) -> dict[str, Any]:
        db_path = self.state_dir / "swarm.db"
        if not db_path.exists():
            return {"exists": False, "journal_mode": "uninitialized", "integrity": "not_created"}
        with closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            integrity = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        return {
            "exists": True,
            "path": str(db_path),
            "journal_mode": str(mode),
            "integrity": str(integrity),
            "healthy": str(integrity).lower() == "ok",
        }

    def check_ledger(self) -> dict[str, Any]:
        ledger_path = self.state_dir / "audit_ledger.jsonl"
        ledger = SwarmAuditLedger(ledger_path)
        valid, count, message = ledger.verify_chain()
        return {
            "exists": ledger_path.exists(),
            "path": str(ledger_path),
            "entries": count,
            "valid": valid,
            "message": message,
        }

    def check_worktrees(self) -> dict[str, Any]:
        mgr = WorktreeManager(self.repo_root, state_dir=self.state_dir)
        workers = mgr.list_workers()
        missing = [w.worker_id for w in workers if not w.path.exists()]
        return {
            "active_workers": len(workers),
            "worker_ids": [w.worker_id for w in workers],
            "missing_paths": missing,
            "healthy": len(missing) == 0,
        }

    def report(self) -> dict[str, Any]:
        """Return full health report across SQLite WAL, SHA-256 ledger, and worktrees."""
        sqlite_info = self.check_sqlite_wal()
        ledger_info = self.check_ledger()
        wt_info = self.check_worktrees()
        overall = (
            (not sqlite_info["exists"] or sqlite_info.get("healthy", False))
            and ledger_info["valid"]
            and wt_info["healthy"]
        )
        return {
            "healthy": overall,
            "sqlite_wal": sqlite_info,
            "audit_ledger": ledger_info,
            "worktrees": wt_info,
        }
