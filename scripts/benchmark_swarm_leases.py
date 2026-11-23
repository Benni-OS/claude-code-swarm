"""Benchmark SQLite3 WAL lease contention across parallel worker threads."""

from __future__ import annotations

from claude_code_swarm.tools.bench_matrix import run_matrix


def main() -> None:
    run_matrix(workers_list=[2, 4, 8], ops_per_worker=50, as_json=False)


if __name__ == "__main__":
    main()
