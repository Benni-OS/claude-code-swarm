"""Tests for LeaseArbiter and SwarmBudget."""

from __future__ import annotations

import time
from pathlib import Path

from claude_code_swarm.core.lease_arbiter import LeaseArbiter
from claude_code_swarm.core.swarm_budget import SwarmBudget


def test_exclusive_lease_blocks_second_worker(tmp_path: Path) -> None:
    arbiter = LeaseArbiter(tmp_path / "swarm.db", repo_root=tmp_path)
    r1 = arbiter.acquire_lease("src/engine.py", "worker-1", mode="exclusive")
    r2 = arbiter.acquire_lease("src/engine.py", "worker-2", mode="exclusive")

    assert r1.granted is True
    assert r2.granted is False
    assert r2.conflict_holder == "worker-1"
    assert arbiter.metrics()["conflicts_prevented"] == 1


def test_shared_leases_allow_concurrent_readers(tmp_path: Path) -> None:
    arbiter = LeaseArbiter(tmp_path / "swarm.db")
    r1 = arbiter.acquire_lease("README.md", "worker-1", mode="shared")
    r2 = arbiter.acquire_lease("README.md", "worker-2", mode="shared")
    r3 = arbiter.acquire_lease("README.md", "worker-3", mode="exclusive")

    assert r1.granted is True
    assert r2.granted is True
    assert r3.granted is False


def test_directory_prefix_conflict_detection(tmp_path: Path) -> None:
    arbiter = LeaseArbiter(tmp_path / "swarm.db")
    assert arbiter.acquire_lease("src/core/", "worker-1", mode="exclusive").granted is True

    child = arbiter.acquire_lease("src/core/arbiter.py", "worker-2", mode="exclusive")
    assert child.granted is False
    assert child.conflict_path == "src/core/"

    # Reverse: file lock blocks parent directory lock
    arbiter.release_all("worker-1")
    assert arbiter.acquire_lease("src/core/arbiter.py", "worker-2", mode="exclusive").granted is True
    parent = arbiter.acquire_lease("src/core/", "worker-1", mode="exclusive")
    assert parent.granted is False


def test_ttl_expiration_and_sweep(tmp_path: Path) -> None:
    arbiter = LeaseArbiter(tmp_path / "swarm.db")
    r1 = arbiter.acquire_lease("src/a.py", "worker-1", ttl_seconds=0.05)
    assert r1.granted is True

    time.sleep(0.08)
    purged = arbiter.sweep_expired()
    assert purged >= 1

    r2 = arbiter.acquire_lease("src/a.py", "worker-2", ttl_seconds=10.0)
    assert r2.granted is True


def test_reentrant_lease_and_check_access(tmp_path: Path) -> None:
    arbiter = LeaseArbiter(tmp_path / "swarm.db")
    assert arbiter.acquire_lease("src/x.py", "worker-1").granted is True
    assert arbiter.acquire_lease("src/x.py", "worker-1").granted is True

    chk1 = arbiter.check_access("src/x.py", "worker-1", mode="exclusive")
    chk2 = arbiter.check_access("src/x.py", "worker-2", mode="exclusive")
    assert chk1.allowed is True
    assert chk2.allowed is False


def test_global_budget_circuit_breaker(tmp_path: Path) -> None:
    budget = SwarmBudget(
        tmp_path / "swarm.db",
        global_cap_usd=1.0,
        default_worker_cap_usd=0.8,
        max_tokens=10_000,
    )
    s1 = budget.record_usage("worker-1", usd_delta=0.6, input_tokens=2000, output_tokens=500)
    assert s1.allowed is True

    s2 = budget.record_usage("worker-2", usd_delta=0.5, input_tokens=1000, output_tokens=500)
    assert s2.allowed is False
    assert "Global USD cap reached" in s2.tripped_reason


def test_per_worker_quota_cap(tmp_path: Path) -> None:
    budget = SwarmBudget(tmp_path / "swarm.db", global_cap_usd=10.0, default_worker_cap_usd=1.0)
    budget.set_worker_quota("worker-1", 0.25)

    st = budget.record_usage("worker-1", usd_delta=0.30)
    assert st.allowed is False
    assert "worker-1" in st.tripped_reason
    # worker-2 still allowed
    assert budget.check_budget("worker-2").allowed is True
