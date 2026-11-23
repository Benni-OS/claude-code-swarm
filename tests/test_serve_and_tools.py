"""Tests for MCP server, Telemetry, HealthMonitor, and bench_matrix."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from claude_code_swarm.serve.health_monitor import HealthMonitor
from claude_code_swarm.serve.mcp_server import SwarmMCPServer
from claude_code_swarm.serve.telemetry import SwarmTelemetry
from claude_code_swarm.tools.bench_matrix import run_matrix


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "srv@benni-os.net"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Srv Bot"], cwd=repo, check=True)
    (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)


def test_mcp_initialize_and_tools_list(tmp_path: Path) -> None:
    srv = SwarmMCPServer(tmp_path)
    init = srv.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "claude-code-swarm"

    tools = srv.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {
        "swarm_claim_task",
        "swarm_acquire_lock",
        "swarm_release_lock",
        "swarm_post_handoff",
        "swarm_budget_check",
        "swarm_merge_ready",
        "swarm_status",
    }.issubset(names)


def test_mcp_tools_call_roundtrip(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    srv = SwarmMCPServer(repo)
    srv.blackboard.post_task("Implement parser", target_paths=["parser.py"])

    claim = srv.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "swarm_claim_task", "arguments": {"worker_id": "w1"}},
        }
    )
    claim_data = json.loads(claim["result"]["content"][0]["text"])
    assert claim_data["claimed"] is True

    lock = srv.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "swarm_acquire_lock",
                "arguments": {"path": "parser.py", "worker_id": "w1"},
            },
        }
    )
    assert json.loads(lock["result"]["content"][0]["text"])["granted"] is True

    status = srv.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "swarm_status", "arguments": {}},
        }
    )
    status_data = json.loads(status["result"]["content"][0]["text"])
    assert len(status_data["leases"]) == 1


def test_telemetry_percentiles_and_counters() -> None:
    tel = SwarmTelemetry()
    for val in (1.0, 2.0, 3.0, 4.0, 5.0):
        tel.record_latency(val)
    tel.record_conflict_prevented(2)
    tel.record_usd(0.75)

    snap = tel.snapshot()
    assert snap.samples == 5
    assert snap.p50_ms == 3.0
    assert snap.p90_ms > 4.0
    assert snap.conflicts_prevented == 2
    assert snap.total_usd_spent == 0.75


def test_health_monitor_reports_wal_and_ledger(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    srv = SwarmMCPServer(repo)
    srv.arbiter.acquire_lease("app.py", "w1")

    report = HealthMonitor(repo).report()
    assert report["healthy"] is True
    assert report["sqlite_wal"]["journal_mode"].lower() == "wal"
    assert report["audit_ledger"]["valid"] is True


def test_bench_matrix_returns_metrics() -> None:
    res = run_matrix(workers_list=[2, 4], ops_per_worker=10, as_json=False)
    assert len(res["tiers"]) == 2
    assert res["tiers"][0]["workers"] == 2
    assert res["tiers"][0]["total_ops"] == 20
