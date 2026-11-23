"""Multi-worker lease contention & hook latency benchmark matrix (`claude-swarm matrix`)."""

from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from claude_code_swarm.core.lease_arbiter import LeaseArbiter
from claude_code_swarm.serve.telemetry import _percentile


def _benchmark_tier(worker_count: int, ops_per_worker: int, shared_hot_ratio: float = 0.25) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "bench.db"
        arbiter = LeaseArbiter(db_path)
        latencies_ms: list[float] = []
        granted_count = 0
        blocked_count = 0
        lock = threading.Lock()
        barrier = threading.Barrier(worker_count)

        def worker_fn(idx: int) -> None:
            nonlocal granted_count, blocked_count
            wid = f"worker-{idx}"
            local_lat: list[float] = []
            local_granted = 0
            local_blocked = 0
            barrier.wait()
            for op in range(ops_per_worker):
                if (op % max(1, int(round(1.0 / shared_hot_ratio)))) == 0:
                    target = f"src/shared/hot_{op % 2}.py"
                else:
                    target = f"src/workers/{wid}/mod_{op}.py"
                t0 = time.perf_counter()
                res = arbiter.acquire_lease(target, wid, mode="exclusive", ttl_seconds=30.0)
                if res.granted:
                    local_granted += 1
                    arbiter.release_lease(target, wid)
                else:
                    local_blocked += 1
                local_lat.append((time.perf_counter() - t0) * 1000.0)
            with lock:
                latencies_ms.extend(local_lat)
                granted_count += local_granted
                blocked_count += local_blocked

        t_start = time.perf_counter()
        threads = [threading.Thread(target=worker_fn, args=(i,)) for i in range(worker_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall_s = max(1e-6, time.perf_counter() - t_start)

        ordered = sorted(latencies_ms)
        total_ops = worker_count * ops_per_worker
        return {
            "workers": worker_count,
            "total_ops": total_ops,
            "granted": granted_count,
            "conflicts_blocked": blocked_count,
            "p50_ms": round(_percentile(ordered, 50.0), 3),
            "p90_ms": round(_percentile(ordered, 90.0), 3),
            "p99_ms": round(_percentile(ordered, 99.0), 3),
            "ops_per_sec": round(total_ops / wall_s, 1),
        }


def run_matrix(
    workers_list: list[int] | None = None,
    ops_per_worker: int = 40,
    as_json: bool = False,
) -> dict[str, Any]:
    """Execute multi-worker SQLite WAL lease contention matrix."""
    tiers = workers_list or [2, 4, 8]
    results = [_benchmark_tier(w, ops_per_worker) for w in tiers]
    payload = {"tiers": results, "ops_per_worker": ops_per_worker}

    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print("claude-code-swarm lease contention matrix (SQLite3 WAL)")
        print(f"{'Workers':>7} | {'Ops':>6} | {'Granted':>7} | {'Blocked':>7} | {'P50(ms)':>8} | {'P90(ms)':>8} | {'P99(ms)':>8} | {'Ops/sec':>9}")
        print("-" * 78)
        for r in results:
            print(
                f"{r['workers']:>7} | {r['total_ops']:>6} | {r['granted']:>7} | "
                f"{r['conflicts_blocked']:>7} | {r['p50_ms']:>8.3f} | {r['p90_ms']:>8.3f} | "
                f"{r['p99_ms']:>8.3f} | {r['ops_per_sec']:>9.1f}"
            )
    return payload
