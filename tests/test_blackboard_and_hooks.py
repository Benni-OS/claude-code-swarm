"""Tests for Blackboard DAG and HookEngine."""

from __future__ import annotations

from pathlib import Path

from claude_code_swarm.core.blackboard import Blackboard
from claude_code_swarm.core.hook_engine import HookEngine


def test_blackboard_dag_dependency_gating(tmp_path: Path) -> None:
    bb = Blackboard(tmp_path / "swarm.db")
    t1 = bb.post_task("Build schema", target_paths=["schema.py"])
    t2 = bb.post_task("Build API", dependencies=[t1.task_id], target_paths=["api.py"])

    # Cannot claim t2 while t1 is pending
    assert bb.claim_task("worker-2", task_id=t2.task_id) is None

    c1 = bb.claim_task("worker-1")
    assert c1 is not None and c1.task_id == t1.task_id

    bb.resolve_task(t1.task_id, "worker-1", "Schema ready")
    c2 = bb.claim_task("worker-2")
    assert c2 is not None and c2.task_id == t2.task_id


def test_blackboard_priority_ordering(tmp_path: Path) -> None:
    bb = Blackboard(tmp_path / "swarm.db")
    bb.post_task("Low priority", priority=1)
    high = bb.post_task("High priority", priority=10)

    claimed = bb.claim_task("worker-1")
    assert claimed is not None
    assert claimed.task_id == high.task_id


def test_blackboard_handoffs_and_fail_task(tmp_path: Path) -> None:
    bb = Blackboard(tmp_path / "swarm.db")
    t1 = bb.post_task("Lint code")
    bb.claim_task("worker-1", task_id=t1.task_id)
    failed = bb.fail_task(t1.task_id, "worker-1", "Flake8 error")
    assert failed.status == "failed"

    bb.post_handoff("worker-1", "worker-2", "Fix lint in app.py", task_id=t1.task_id, artifacts=["app.py"])
    handoffs = bb.list_handoffs(to_worker="worker-2")
    assert len(handoffs) == 1
    assert handoffs[0].artifacts == ["app.py"]


def test_hook_engine_pretooluse_enforces_write_lease(tmp_path: Path) -> None:
    e1 = HookEngine(tmp_path, worker_id="worker-1")
    e2 = HookEngine(tmp_path, worker_id="worker-2")

    d1 = e1.dispatch(
        "PreToolUse",
        {"worker_id": "worker-1", "tool_name": "Write", "tool_input": {"file_path": "src/main.py"}},
    )
    assert d1.allowed is True

    d2 = e2.dispatch(
        "PreToolUse",
        {"worker_id": "worker-2", "tool_name": "Edit", "tool_input": {"file_path": "src/main.py"}},
    )
    assert d2.allowed is False
    assert d2.hook_output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_engine_pretooluse_bash_redirection_lease(tmp_path: Path) -> None:
    e1 = HookEngine(tmp_path, worker_id="worker-1")
    e2 = HookEngine(tmp_path, worker_id="worker-2")

    assert e1.dispatch(
        "PreToolUse",
        {"worker_id": "worker-1", "tool_name": "Edit", "tool_input": {"file_path": "config.json"}},
    ).allowed is True

    d2 = e2.dispatch(
        "PreToolUse",
        {"worker_id": "worker-2", "tool_name": "Bash", "tool_input": {"command": "echo '{}' > config.json"}},
    )
    assert d2.allowed is False


def test_hook_engine_stop_releases_leases_and_posttooluse_tracks_budget(tmp_path: Path) -> None:
    e1 = HookEngine(tmp_path, worker_id="worker-1")
    e2 = HookEngine(tmp_path, worker_id="worker-2")
    e1.budget.configure(global_cap_usd=0.5)

    e1.dispatch(
        "PreToolUse",
        {"worker_id": "worker-1", "tool_name": "Write", "tool_input": {"file_path": "a.py"}},
    )
    stop_dec = e1.dispatch("Stop", {"worker_id": "worker-1"})
    assert stop_dec.allowed is True
    assert "Released 1" in stop_dec.reason

    # Now worker-2 can acquire a.py
    assert e2.dispatch(
        "PreToolUse",
        {"worker_id": "worker-2", "tool_name": "Write", "tool_input": {"file_path": "a.py"}},
    ).allowed is True

    # PostToolUse exceeding global budget trips subsequent PreToolUse
    e1.dispatch(
        "PostToolUse",
        {"worker_id": "worker-1", "usage": {"cost_usd": 0.6, "input_tokens": 100, "output_tokens": 50}},
    )
    blocked = e2.dispatch(
        "PreToolUse",
        {"worker_id": "worker-2", "tool_name": "Write", "tool_input": {"file_path": "b.py"}},
    )
    assert blocked.allowed is False
