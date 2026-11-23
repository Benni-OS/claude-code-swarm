"""Tests for WorktreeManager, MergeGate, and SwarmAuditLedger."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from claude_code_swarm.core.merge_gate import GENESIS_HASH, MergeGate, SwarmAuditLedger
from claude_code_swarm.core.worktree_manager import WorktreeManager


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@benni-os.net"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test Bot"], cwd=repo, check=True)
    (repo / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True)


def test_spawn_list_and_diff_worker(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    mgr = WorktreeManager(repo)

    wt = mgr.spawn_worker("worker-1", task_id="task-01")
    assert wt.branch == "swarm/worker-1"
    assert wt.path.exists()

    workers = mgr.list_workers()
    assert len(workers) == 1
    assert workers[0].worker_id == "worker-1"
    assert workers[0].task_id == "task-01"

    (wt.path / "app.py").write_text("def add(a: int, b: int) -> int:\n    return a + b + 1\n", encoding="utf-8")
    diff = mgr.diff_worker("worker-1")
    assert "return a + b + 1" in diff


def test_merge_worker_passes_ast_gate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    mgr = WorktreeManager(repo)
    wt = mgr.spawn_worker("worker-alpha")

    (wt.path / "feature.py").write_text("def greet(name: str) -> str:\n    return f'hi {name}'\n", encoding="utf-8")
    res = mgr.merge_worker("worker-alpha")

    assert res.merged is True
    assert res.gate_report is not None
    assert res.gate_report.passed is True
    assert (repo / "feature.py").exists()


def test_merge_gate_blocks_python_syntax_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    mgr = WorktreeManager(repo)
    wt = mgr.spawn_worker("worker-bad-ast")

    (wt.path / "broken.py").write_text("def broken(:\n    pass\n", encoding="utf-8")
    res = mgr.merge_worker("worker-bad-ast")

    assert res.merged is False
    assert res.gate_report is not None
    assert res.gate_report.passed is False
    assert any(i.kind == "ast_syntax_error" for i in res.gate_report.issues)


def test_merge_gate_blocks_conflict_markers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    bad_file = repo / "notes.md"
    bad_file.write_text("hello\n<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>> branch\n", encoding="utf-8")

    gate = MergeGate(repo)
    report = gate.verify_files([bad_file], worker_id="worker-conflict")
    assert report.passed is False
    assert any(i.kind == "conflict_marker" for i in report.issues)


def test_cleanup_worker_removes_worktree_and_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    mgr = WorktreeManager(repo)
    wt = mgr.spawn_worker("worker-temp")
    assert wt.path.exists()

    ok = mgr.cleanup_worker("worker-temp", force=True, delete_branch=True)
    assert ok is True
    assert not wt.path.exists()
    assert mgr.list_workers() == []


def test_audit_ledger_chain_and_tamper_detection(tmp_path: Path) -> None:
    ledger_path = tmp_path / ".swarm" / "audit_ledger.jsonl"
    ledger = SwarmAuditLedger(ledger_path)
    e1 = ledger.append("spawn", "worker-1", {"branch": "swarm/worker-1"})
    e2 = ledger.append("merge", "worker-1", {"passed": True})

    assert e1["prev_hash"] == GENESIS_HASH
    assert e2["prev_hash"] == e1["entry_hash"]

    valid, count, _ = ledger.verify_chain()
    assert valid is True
    assert count == 2

    # Tamper with second entry payload
    lines = ledger_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["payload"]["passed"] = False
    lines[1] = json.dumps(tampered)
    ledger_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    valid2, idx, _ = ledger.verify_chain()
    assert valid2 is False
    assert idx == 1
