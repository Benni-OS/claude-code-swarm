"""Shared typed task DAG & handoff blackboard persisted in SQLite3 WAL."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TaskRecord:
    """Typed task node in the swarm DAG."""

    task_id: str
    title: str
    description: str
    status: str
    priority: int
    dependencies: list[str]
    target_paths: list[str]
    claimed_by: str | None
    result_summary: str
    created_at: float
    updated_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "dependencies": self.dependencies,
            "target_paths": self.target_paths,
            "claimed_by": self.claimed_by,
            "result_summary": self.result_summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class HandoffRecord:
    """Typed inter-worker handoff message."""

    handoff_id: str
    from_worker: str
    to_worker: str
    task_id: str | None
    summary: str
    artifacts: list[str]
    created_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "handoff_id": self.handoff_id,
            "from_worker": self.from_worker,
            "to_worker": self.to_worker,
            "task_id": self.task_id,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "created_at": self.created_at,
        }


class Blackboard:
    """Shared typed task DAG & handoff blackboard backed by SQLite3 WAL."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN ('pending', 'claimed', 'resolved', 'failed')),
                    priority INTEGER NOT NULL DEFAULT 0,
                    dependencies_json TEXT NOT NULL DEFAULT '[]',
                    target_paths_json TEXT NOT NULL DEFAULT '[]',
                    claimed_by TEXT,
                    result_summary TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS handoffs (
                    handoff_id TEXT PRIMARY KEY,
                    from_worker TEXT NOT NULL,
                    to_worker TEXT NOT NULL,
                    task_id TEXT,
                    summary TEXT NOT NULL,
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    created_at REAL NOT NULL
                );
                """
            )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            title=row["title"],
            description=row["description"],
            status=row["status"],
            priority=int(row["priority"]),
            dependencies=json.loads(row["dependencies_json"]),
            target_paths=json.loads(row["target_paths_json"]),
            claimed_by=row["claimed_by"],
            result_summary=row["result_summary"],
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    @staticmethod
    def _row_to_handoff(row: sqlite3.Row) -> HandoffRecord:
        return HandoffRecord(
            handoff_id=row["handoff_id"],
            from_worker=row["from_worker"],
            to_worker=row["to_worker"],
            task_id=row["task_id"],
            summary=row["summary"],
            artifacts=json.loads(row["artifacts_json"]),
            created_at=float(row["created_at"]),
        )

    def post_task(
        self,
        title: str,
        description: str = "",
        dependencies: list[str] | None = None,
        target_paths: list[str] | None = None,
        priority: int = 0,
        task_id: str | None = None,
    ) -> TaskRecord:
        """Create a new task node in the DAG."""
        tid = task_id or f"task-{uuid.uuid4().hex[:8]}"
        deps = list(dependencies or [])
        paths = list(target_paths or [])
        now = time.time()

        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    INSERT INTO tasks (
                        task_id, title, description, status, priority,
                        dependencies_json, target_paths_json, claimed_by,
                        result_summary, created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', ?, ?, ?, NULL, '', ?, ?);
                    """,
                    (
                        tid,
                        title,
                        description,
                        priority,
                        json.dumps(deps),
                        json.dumps(paths),
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?;", (tid,)
                ).fetchone()
                conn.execute("COMMIT;")
                return self._row_to_task(row)
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def claim_task(
        self,
        worker_id: str,
        task_id: str | None = None,
    ) -> TaskRecord | None:
        """Atomically claim a ready task (all DAG dependencies resolved) for `worker_id`."""
        if not worker_id or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")

        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                all_rows = conn.execute("SELECT task_id, status FROM tasks;").fetchall()
                status_map = {r["task_id"]: r["status"] for r in all_rows}

                if task_id is not None:
                    candidates = conn.execute(
                        "SELECT * FROM tasks WHERE task_id = ? AND status = 'pending';",
                        (task_id,),
                    ).fetchall()
                else:
                    candidates = conn.execute(
                        "SELECT * FROM tasks WHERE status = 'pending' ORDER BY priority DESC, created_at ASC;"
                    ).fetchall()

                chosen: sqlite3.Row | None = None
                for cand in candidates:
                    deps = json.loads(cand["dependencies_json"])
                    if all(status_map.get(dep) == "resolved" for dep in deps):
                        chosen = cand
                        break

                if chosen is None:
                    conn.execute("COMMIT;")
                    return None

                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'claimed', claimed_by = ?, updated_at = ?
                    WHERE task_id = ? AND status = 'pending';
                    """,
                    (worker_id, now, chosen["task_id"]),
                )
                updated = conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?;", (chosen["task_id"],)
                ).fetchone()
                conn.execute("COMMIT;")
                return self._row_to_task(updated)
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def resolve_task(
        self,
        task_id: str,
        worker_id: str,
        result_summary: str = "",
    ) -> TaskRecord:
        """Mark a claimed task as resolved."""
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?;", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown task_id: {task_id}")
                if row["claimed_by"] not in (None, worker_id):
                    raise PermissionError(
                        f"Task '{task_id}' is claimed by '{row['claimed_by']}', not '{worker_id}'"
                    )
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'resolved', claimed_by = ?, result_summary = ?, updated_at = ?
                    WHERE task_id = ?;
                    """,
                    (worker_id, result_summary, now, task_id),
                )
                updated = conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?;", (task_id,)
                ).fetchone()
                conn.execute("COMMIT;")
                return self._row_to_task(updated)
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def fail_task(
        self,
        task_id: str,
        worker_id: str,
        reason: str = "",
    ) -> TaskRecord:
        """Mark a task as failed with diagnostic reason."""
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                row = conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?;", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(f"Unknown task_id: {task_id}")
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'failed', claimed_by = ?, result_summary = ?, updated_at = ?
                    WHERE task_id = ?;
                    """,
                    (worker_id, reason, now, task_id),
                )
                updated = conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?;", (task_id,)
                ).fetchone()
                conn.execute("COMMIT;")
                return self._row_to_task(updated)
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def post_handoff(
        self,
        from_worker: str,
        to_worker: str,
        summary: str,
        task_id: str | None = None,
        artifacts: list[str] | None = None,
    ) -> HandoffRecord:
        """Post a typed handoff message from one worker to another (or 'broadcast')."""
        hid = f"handoff-{uuid.uuid4().hex[:8]}"
        arts = list(artifacts or [])
        now = time.time()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    INSERT INTO handoffs (
                        handoff_id, from_worker, to_worker, task_id,
                        summary, artifacts_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (hid, from_worker, to_worker, task_id, summary, json.dumps(arts), now),
                )
                row = conn.execute(
                    "SELECT * FROM handoffs WHERE handoff_id = ?;", (hid,)
                ).fetchone()
                conn.execute("COMMIT;")
                return self._row_to_handoff(row)
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def list_tasks(self, status: str | None = None) -> list[TaskRecord]:
        """List tasks ordered by priority and creation time."""
        with closing(self._connect()) as conn:
            if status is not None:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY priority DESC, created_at ASC;",
                    (status,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY priority DESC, created_at ASC;"
                ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def list_handoffs(self, to_worker: str | None = None) -> list[HandoffRecord]:
        """List handoffs, optionally filtered by recipient worker."""
        with closing(self._connect()) as conn:
            if to_worker is not None:
                rows = conn.execute(
                    "SELECT * FROM handoffs WHERE to_worker IN (?, 'broadcast', '*') ORDER BY created_at ASC;",
                    (to_worker,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM handoffs ORDER BY created_at ASC;"
                ).fetchall()
        return [self._row_to_handoff(r) for r in rows]
