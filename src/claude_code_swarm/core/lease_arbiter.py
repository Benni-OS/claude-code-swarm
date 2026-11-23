"""SQLite3 WAL pessimistic/optimistic file & directory lease arbiter."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


VALID_MODES = ("exclusive", "shared")


def normalize_lease_path(raw_path: str, repo_root: Path | None = None) -> str:
    """Normalize a file or directory path into a canonical POSIX relative key."""
    text = raw_path.strip().replace("\\", "/")
    if repo_root is not None:
        try:
            candidate = Path(raw_path)
            if candidate.is_absolute():
                rel = candidate.resolve().relative_to(repo_root.resolve())
                text = rel.as_posix()
        except ValueError:
            pass
    is_dir_hint = text.endswith("/")
    cleaned = str(PurePosixPath(text))
    if cleaned in ("", "."):
        return "."
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if is_dir_hint and not cleaned.endswith("/"):
        cleaned += "/"
    return cleaned


def paths_overlap(path_a: str, path_b: str) -> bool:
    """Return True if two normalized paths are identical or one is a parent directory of the other."""
    a = path_a.rstrip("/")
    b = path_b.rstrip("/")
    if a == "." or b == ".":
        return True
    if a == b:
        return True
    return b.startswith(a + "/") or a.startswith(b + "/")


@dataclass(frozen=True)
class LeaseRecord:
    """Represents an active or historical file/directory lease."""

    path: str
    worker_id: str
    mode: str
    acquired_at: float
    expires_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "worker_id": self.worker_id,
            "mode": self.mode,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class LeaseResult:
    """Result of an acquire_lease attempt."""

    granted: bool
    path: str
    worker_id: str
    mode: str
    expires_at: float | None = None
    conflict_holder: str | None = None
    conflict_path: str | None = None
    conflict_mode: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "granted": self.granted,
            "path": self.path,
            "worker_id": self.worker_id,
            "mode": self.mode,
            "expires_at": self.expires_at,
            "conflict_holder": self.conflict_holder,
            "conflict_path": self.conflict_path,
            "conflict_mode": self.conflict_mode,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AccessCheckResult:
    """Result of a non-mutating access check."""

    allowed: bool
    path: str
    worker_id: str
    mode: str
    conflict_holder: str | None = None
    conflict_path: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "path": self.path,
            "worker_id": self.worker_id,
            "mode": self.mode,
            "conflict_holder": self.conflict_holder,
            "conflict_path": self.conflict_path,
            "reason": self.reason,
        }


class LeaseArbiter:
    """SQLite3 WAL pessimistic/optimistic file & directory lock arbiter.

    Prevents parallel Claude Code sessions from overwriting each other's files
    by enforcing hierarchical path leases (`exclusive` or `shared`) with TTL
    expiration and automatic stale-lease sweeping.
    """

    def __init__(self, db_path: Path | str, repo_root: Path | str | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.repo_root = Path(repo_root) if repo_root is not None else None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leases (
                    path TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK (mode IN ('exclusive', 'shared')),
                    acquired_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (path, worker_id)
                );
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leases_expires ON leases (expires_at);"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lease_metrics (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    conflicts_prevented INTEGER NOT NULL DEFAULT 0,
                    leases_granted INTEGER NOT NULL DEFAULT 0
                );
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO lease_metrics (id, conflicts_prevented, leases_granted) VALUES (1, 0, 0);"
            )

    def _norm(self, path: str) -> str:
        return normalize_lease_path(path, self.repo_root)

    def sweep_expired(self, now: float | None = None) -> int:
        """Remove all expired leases and return the number of rows purged."""
        ts = time.time() if now is None else now
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute("DELETE FROM leases WHERE expires_at <= ?;", (ts,))
                deleted = cur.rowcount or 0
                conn.execute("COMMIT;")
                return deleted
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def acquire_lease(
        self,
        path: str,
        worker_id: str,
        mode: str = "exclusive",
        ttl_seconds: float = 300.0,
    ) -> LeaseResult:
        """Attempt to acquire an exclusive or shared lease on `path` for `worker_id`."""
        if mode not in VALID_MODES:
            raise ValueError(f"Unsupported lease mode {mode!r}; expected one of {VALID_MODES}")
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")

        norm_path = self._norm(path)
        now = time.time()
        expires_at = now + ttl_seconds

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute("DELETE FROM leases WHERE expires_at <= ?;", (now,))
                rows = conn.execute(
                    "SELECT path, worker_id, mode, acquired_at, expires_at FROM leases;"
                ).fetchall()

                for row in rows:
                    holder = row["worker_id"]
                    if holder == worker_id:
                        continue
                    held_path = row["path"]
                    held_mode = row["mode"]
                    if not paths_overlap(norm_path, held_path):
                        continue
                    if mode == "exclusive" or held_mode == "exclusive":
                        conn.execute(
                            "UPDATE lease_metrics SET conflicts_prevented = conflicts_prevented + 1 WHERE id = 1;"
                        )
                        conn.execute("COMMIT;")
                        return LeaseResult(
                            granted=False,
                            path=norm_path,
                            worker_id=worker_id,
                            mode=mode,
                            conflict_holder=holder,
                            conflict_path=held_path,
                            conflict_mode=held_mode,
                            reason=(
                                f"Lease conflict: {held_mode} lock on '{held_path}' "
                                f"held by '{holder}'"
                            ),
                        )

                conn.execute(
                    """
                    INSERT INTO leases (path, worker_id, mode, acquired_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(path, worker_id) DO UPDATE SET
                        mode = excluded.mode,
                        acquired_at = excluded.acquired_at,
                        expires_at = excluded.expires_at;
                    """,
                    (norm_path, worker_id, mode, now, expires_at),
                )
                conn.execute(
                    "UPDATE lease_metrics SET leases_granted = leases_granted + 1 WHERE id = 1;"
                )
                conn.execute("COMMIT;")
                return LeaseResult(
                    granted=True,
                    path=norm_path,
                    worker_id=worker_id,
                    mode=mode,
                    expires_at=expires_at,
                    reason="Lease granted",
                )
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def release_lease(self, path: str, worker_id: str) -> bool:
        """Release a specific lease held by `worker_id` on `path`."""
        norm_path = self._norm(path)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    "DELETE FROM leases WHERE path = ? AND worker_id = ?;",
                    (norm_path, worker_id),
                )
                conn.execute("COMMIT;")
                return (cur.rowcount or 0) > 0
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def release_all(self, worker_id: str) -> int:
        """Release all leases held by `worker_id`."""
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                cur = conn.execute(
                    "DELETE FROM leases WHERE worker_id = ?;",
                    (worker_id,),
                )
                conn.execute("COMMIT;")
                return cur.rowcount or 0
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def check_access(
        self,
        path: str,
        worker_id: str,
        mode: str = "exclusive",
    ) -> AccessCheckResult:
        """Check whether `worker_id` could access `path` in `mode` without mutating leases."""
        if mode not in VALID_MODES:
            raise ValueError(f"Unsupported lease mode {mode!r}; expected one of {VALID_MODES}")

        norm_path = self._norm(path)
        now = time.time()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT path, worker_id, mode FROM leases WHERE expires_at > ?;",
                (now,),
            ).fetchall()

        for row in rows:
            holder = row["worker_id"]
            if holder == worker_id:
                continue
            held_path = row["path"]
            held_mode = row["mode"]
            if not paths_overlap(norm_path, held_path):
                continue
            if mode == "exclusive" or held_mode == "exclusive":
                return AccessCheckResult(
                    allowed=False,
                    path=norm_path,
                    worker_id=worker_id,
                    mode=mode,
                    conflict_holder=holder,
                    conflict_path=held_path,
                    reason=(
                        f"Blocked by {held_mode} lease on '{held_path}' held by '{holder}'"
                    ),
                )

        return AccessCheckResult(
            allowed=True,
            path=norm_path,
            worker_id=worker_id,
            mode=mode,
            reason="Access allowed",
        )

    def list_leases(self, active_only: bool = True) -> list[LeaseRecord]:
        """Return currently registered leases."""
        now = time.time()
        with closing(self._connect()) as conn:
            if active_only:
                rows = conn.execute(
                    "SELECT path, worker_id, mode, acquired_at, expires_at FROM leases WHERE expires_at > ? ORDER BY acquired_at ASC;",
                    (now,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT path, worker_id, mode, acquired_at, expires_at FROM leases ORDER BY acquired_at ASC;"
                ).fetchall()
        return [
            LeaseRecord(
                path=r["path"],
                worker_id=r["worker_id"],
                mode=r["mode"],
                acquired_at=r["acquired_at"],
                expires_at=r["expires_at"],
            )
            for r in rows
        ]

    def metrics(self) -> dict[str, int]:
        """Return cumulative lease grant and conflict prevention counters."""
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT conflicts_prevented, leases_granted FROM lease_metrics WHERE id = 1;"
            ).fetchone()
        if row is None:
            return {"conflicts_prevented": 0, "leases_granted": 0}
        return {
            "conflicts_prevented": int(row["conflicts_prevented"]),
            "leases_granted": int(row["leases_granted"]),
        }
