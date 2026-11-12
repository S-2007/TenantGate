"""Process-local counters exposed at GET /metrics in Prometheus text format (0.0.4).

Labels never carry user, tenant, query or document identifiers: only fixed reason strings.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable


class Counter:
    """Monotonic counter with an optional single label."""

    def __init__(self, name: str, help_text: str, *, label: str | None = None,
                 initial_labels: Iterable[str] = ()) -> None:  # fmt: skip
        self.name, self.help, self.label = name, help_text, label
        self._values: dict[str, float] = {v: 0.0 for v in initial_labels} if label else {"": 0.0}
        self._lock = threading.Lock()

    def inc(self, label_value: str = "", amount: float = 1.0) -> None:
        """Add ``amount`` (must be >= 0)."""
        if amount < 0:
            raise ValueError("counters only go up")
        with self._lock:
            self._values[label_value] = self._values.get(label_value, 0.0) + amount

    def value(self, label_value: str = "") -> float:
        with self._lock:
            return self._values.get(label_value, 0.0)

    def total(self) -> float:
        with self._lock:
            return sum(self._values.values())

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        with self._lock:
            items = sorted(self._values.items())
        for lv, v in items:
            labels = f'{{{self.label}="{lv}"}}' if self.label else ""
            lines.append(f"{self.name}{labels} {v:g}")
        return lines


VIOLATION_REASONS = (
    "policy_denied",
    "document_missing",
    "tenant_mismatch",
    "document_policy_denied",
    "document_not_ready",
)

ACL_VIOLATIONS = Counter(
    "acl_violations_total",
    "Chunks that reached the post-retrieval check but failed it (should stay 0).",
    label="reason",
    initial_labels=VIOLATION_REASONS,
)
ACL_STALE = Counter(
    "acl_stale_chunks_total",
    "Chunks dropped because their acl_version differs from their document's (needs re-sync).",
)
AUDIT_WRITE_FAILURES = Counter(
    "audit_write_failures_total",
    "Audit rows that could not be written (the request was failed closed).",
)
ASK_REQUESTS = Counter(
    "ask_requests_total",
    "POST /ask requests by audit status.",
    label="status",
    initial_labels=("ok", "denied", "error"),
)

ALL = (ACL_VIOLATIONS, ACL_STALE, AUDIT_WRITE_FAILURES, ASK_REQUESTS)


def render_all() -> str:
    """Prometheus exposition text for every counter."""
    return "\n".join(line for c in ALL for line in c.render()) + "\n"
