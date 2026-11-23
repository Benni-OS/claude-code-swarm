"""Rolling P50/P90/P99 lease/hook latency, conflict prevention & swarm USD telemetry."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_vals[lo]
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Point-in-time telemetry summary."""

    samples: int
    p50_ms: float
    p90_ms: float
    p99_ms: float
    conflicts_prevented: int
    total_usd_spent: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "p50_ms": round(self.p50_ms, 3),
            "p90_ms": round(self.p90_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "conflicts_prevented": self.conflicts_prevented,
            "total_usd_spent": round(self.total_usd_spent, 6),
        }


class SwarmTelemetry:
    """In-memory rolling window telemetry collector for lease & hook operations."""

    def __init__(self, max_samples: int = 1000) -> None:
        self._latencies_ms: deque[float] = deque(maxlen=max_samples)
        self.conflicts_prevented: int = 0
        self.total_usd_spent: float = 0.0

    def record_latency(self, latency_ms: float) -> None:
        self._latencies_ms.append(float(latency_ms))

    def record_conflict_prevented(self, count: int = 1) -> None:
        self.conflicts_prevented += int(count)

    def record_usd(self, usd_delta: float) -> None:
        self.total_usd_spent += float(usd_delta)

    def snapshot(self) -> TelemetrySnapshot:
        ordered = sorted(self._latencies_ms)
        return TelemetrySnapshot(
            samples=len(ordered),
            p50_ms=_percentile(ordered, 50.0),
            p90_ms=_percentile(ordered, 90.0),
            p99_ms=_percentile(ordered, 99.0),
            conflicts_prevented=self.conflicts_prevented,
            total_usd_spent=self.total_usd_spent,
        )
