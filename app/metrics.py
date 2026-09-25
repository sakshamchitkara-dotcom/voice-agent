"""In-process metrics rendered in the Prometheus text exposition format (version 0.0.4).

ponytail: per-process state and no multiprocess support; switch to prometheus_client
if you run several workers.
"""
from __future__ import annotations

import threading
from bisect import bisect_left
from collections import defaultdict

# Seconds. Voice tools should land well under 1s; Vapi times out at 20s.
BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0)

_lock = threading.Lock()
_metrics: dict[str, "_Metric"] = {}


def _labels(names: tuple[str, ...], values: tuple[str, ...], extra: str = "") -> str:
    def esc(v: str) -> str:
        return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    parts = [f'{n}="{esc(v)}"' for n, v in zip(names, values)]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


class _Metric:
    kind = ""

    def __init__(self, name: str, help_: str, labels: tuple[str, ...] = ()):
        self.name, self.help, self.label_names = name, help_, labels
        _metrics[name] = self

    def _key(self, labels: dict[str, str]) -> tuple[str, ...]:
        return tuple(str(labels.get(n, "")) for n in self.label_names)

    def header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]


class Counter(_Metric):
    kind = "counter"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.values: dict[tuple[str, ...], float] = defaultdict(float)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        with _lock:
            self.values[self._key(labels)] += amount

    def render(self) -> list[str]:
        return self.header() + [f"{self.name}{_labels(self.label_names, k)} {v:g}"
                                for k, v in sorted(self.values.items())]


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, *a, buckets: tuple[float, ...] = BUCKETS, **kw):
        super().__init__(*a, **kw)
        self.buckets = buckets
        # per label set: [count per bucket (+Inf last), sum]
        self.values: dict[tuple[str, ...], list] = {}

    def observe(self, seconds: float, **labels: str) -> None:
        key = self._key(labels)
        with _lock:
            counts, total = self.values.get(key) or ([0] * (len(self.buckets) + 1), 0.0)
            counts[bisect_left(self.buckets, seconds)] += 1
            self.values[key] = [counts, total + seconds]

    def render(self) -> list[str]:
        out = self.header()
        for key, (counts, total) in sorted(self.values.items()):
            running = 0
            for bound, n in zip([*map(str, self.buckets), "+Inf"], counts):
                running += n
                le = f'le="{bound}"'
                out.append(f"{self.name}_bucket{_labels(self.label_names, key, le)} {running}")
            out.append(f"{self.name}_sum{_labels(self.label_names, key)} {total:.6f}")
            out.append(f"{self.name}_count{_labels(self.label_names, key)} {running}")
        return out


def render() -> str:
    with _lock:
        lines = [line for m in _metrics.values() for line in m.render()]
    return "\n".join(lines) + "\n"


def reset() -> None:
    """Tests only."""
    with _lock:
        for m in _metrics.values():
            m.values.clear()


http_requests = Counter("voice_agent_http_requests_total", "HTTP requests by route and status.",
                        ("method", "route", "status"))
http_latency = Histogram("voice_agent_http_request_duration_seconds", "HTTP request latency.",
                         ("method", "route"))
webhook_messages = Counter("voice_agent_webhook_messages_total", "Vapi server messages by type.",
                           ("type",))
