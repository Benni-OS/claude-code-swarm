"""Global swarm USD/token cap & per-worker quota circuit breaker."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BudgetStatus:
    """Snapshot of global and optional worker budget state."""

    allowed: bool
    global_usd_spent: float
    global_cap_usd: float
    global_tokens: int
    max_tokens: int
    worker_id: str | None = None
    worker_usd_spent: float = 0.0
    worker_cap_usd: float = 0.0
    tripped_reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "global_usd_spent": round(self.global_usd_spent, 6),
            "global_cap_usd": round(self.global_cap_usd, 6),
            "global_tokens": self.global_tokens,
            "max_tokens": self.max_tokens,
            "worker_id": self.worker_id,
            "worker_usd_spent": round(self.worker_usd_spent, 6),
            "worker_cap_usd": round(self.worker_cap_usd, 6),
            "tripped_reason": self.tripped_reason,
        }


class SwarmBudget:
    """Global swarm USD/token cap across all parallel Claude Code workers."""

    def __init__(
        self,
        db_path: Path | str,
        global_cap_usd: float = 10.0,
        default_worker_cap_usd: float = 2.5,
        max_tokens: int = 2_000_000,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema(global_cap_usd, default_worker_cap_usd, max_tokens)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(
        self,
        global_cap_usd: float,
        default_worker_cap_usd: float,
        max_tokens: int,
    ) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS swarm_budget_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    global_cap_usd REAL NOT NULL,
                    default_worker_cap_usd REAL NOT NULL,
                    max_tokens INTEGER NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO swarm_budget_config (id, global_cap_usd, default_worker_cap_usd, max_tokens)
                VALUES (1, ?, ?, ?);
                """,
                (float(global_cap_usd), float(default_worker_cap_usd), int(max_tokens)),
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS worker_usage (
                    worker_id TEXT PRIMARY KEY,
                    usd_spent REAL NOT NULL DEFAULT 0.0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cap_usd REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def configure(
        self,
        global_cap_usd: float | None = None,
        default_worker_cap_usd: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        """Update global swarm budget thresholds."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cfg = conn.execute("SELECT * FROM swarm_budget_config WHERE id = 1;").fetchone()
                new_global = float(global_cap_usd) if global_cap_usd is not None else float(cfg["global_cap_usd"])
                new_worker = (
                    float(default_worker_cap_usd)
                    if default_worker_cap_usd is not None
                    else float(cfg["default_worker_cap_usd"])
                )
                new_tokens = int(max_tokens) if max_tokens is not None else int(cfg["max_tokens"])
                conn.execute(
                    """
                    UPDATE swarm_budget_config
                    SET global_cap_usd = ?, default_worker_cap_usd = ?, max_tokens = ?
                    WHERE id = 1;
                    """,
                    (new_global, new_worker, new_tokens),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def set_worker_quota(self, worker_id: str, cap_usd: float) -> None:
        """Set or update the USD quota for a specific worker."""
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    INSERT INTO worker_usage (worker_id, usd_spent, input_tokens, output_tokens, cap_usd, updated_at)
                    VALUES (?, 0.0, 0, 0, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        cap_usd = excluded.cap_usd,
                        updated_at = excluded.updated_at;
                    """,
                    (worker_id, float(cap_usd), now),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def record_usage(
        self,
        worker_id: str,
        usd_delta: float = 0.0,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> BudgetStatus:
        """Record incremental USD and token spend for `worker_id` and return current status."""
        if usd_delta < 0 or input_tokens < 0 or output_tokens < 0:
            raise ValueError("Usage deltas must be non-negative")
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cfg = conn.execute("SELECT * FROM swarm_budget_config WHERE id = 1;").fetchone()
                default_cap = float(cfg["default_worker_cap_usd"])
                conn.execute(
                    """
                    INSERT INTO worker_usage (worker_id, usd_spent, input_tokens, output_tokens, cap_usd, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(worker_id) DO UPDATE SET
                        usd_spent = worker_usage.usd_spent + excluded.usd_spent,
                        input_tokens = worker_usage.input_tokens + excluded.input_tokens,
                        output_tokens = worker_usage.output_tokens + excluded.output_tokens,
                        updated_at = excluded.updated_at;
                    """,
                    (worker_id, float(usd_delta), int(input_tokens), int(output_tokens), default_cap, now),
                )
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
        return self.check_budget(worker_id)

    def check_budget(self, worker_id: str | None = None) -> BudgetStatus:
        """Evaluate global and per-worker circuit breakers."""
        with closing(self._connect()) as conn:
            cfg = conn.execute("SELECT * FROM swarm_budget_config WHERE id = 1;").fetchone()
            agg = conn.execute(
                """
                SELECT
                    COALESCE(SUM(usd_spent), 0.0) AS total_usd,
                    COALESCE(SUM(input_tokens + output_tokens), 0) AS total_tokens
                FROM worker_usage;
                """
            ).fetchone()
            worker_row = (
                conn.execute(
                    "SELECT usd_spent, cap_usd FROM worker_usage WHERE worker_id = ?;",
                    (worker_id,),
                ).fetchone()
                if worker_id
                else None
            )

        global_cap = float(cfg["global_cap_usd"])
        default_worker_cap = float(cfg["default_worker_cap_usd"])
        max_tokens = int(cfg["max_tokens"])
        total_usd = float(agg["total_usd"])
        total_tokens = int(agg["total_tokens"])

        worker_usd = float(worker_row["usd_spent"]) if worker_row else 0.0
        worker_cap = float(worker_row["cap_usd"]) if worker_row else default_worker_cap

        reasons: list[str] = []
        if total_usd >= global_cap:
            reasons.append(f"Global USD cap reached (${total_usd:.4f} >= ${global_cap:.4f})")
        if total_tokens >= max_tokens:
            reasons.append(f"Global token cap reached ({total_tokens} >= {max_tokens})")
        if worker_id and worker_usd >= worker_cap:
            reasons.append(
                f"Worker '{worker_id}' quota reached (${worker_usd:.4f} >= ${worker_cap:.4f})"
            )

        return BudgetStatus(
            allowed=len(reasons) == 0,
            global_usd_spent=total_usd,
            global_cap_usd=global_cap,
            global_tokens=total_tokens,
            max_tokens=max_tokens,
            worker_id=worker_id,
            worker_usd_spent=worker_usd,
            worker_cap_usd=worker_cap,
            tripped_reason="; ".join(reasons),
        )

    def list_workers(self) -> list[dict[str, object]]:
        """Return per-worker usage records."""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT worker_id, usd_spent, input_tokens, output_tokens, cap_usd, updated_at FROM worker_usage ORDER BY worker_id ASC;"
            ).fetchall()
        return [
            {
                "worker_id": r["worker_id"],
                "usd_spent": round(float(r["usd_spent"]), 6),
                "input_tokens": int(r["input_tokens"]),
                "output_tokens": int(r["output_tokens"]),
                "cap_usd": round(float(r["cap_usd"]), 6),
                "updated_at": float(r["updated_at"]),
            }
            for r in rows
        ]

    def reset_budget(self) -> None:
        """Clear all recorded worker usage."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute("DELETE FROM worker_usage;")
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise
