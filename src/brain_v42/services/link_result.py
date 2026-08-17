"""4-bucket result dataclass for idempotent graph-link jobs.

Learning fd39a4f9: reporting "operations attempted" instead of net state change
is an anti-pattern — it hides whether the graph already had the edges. The four
buckets (created / matched / skipped / errors) make the distinction explicit and
the freshness_ratio gives a single 0–1 signal: 1.0 on a fresh corpus, 0.0 at
link equilibrium.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LinkJobResult:
    """Accounting container for one idempotent graph-link job run.

    Buckets:
        created  — MERGE inserted a new edge.
        matched  — MERGE matched an existing edge (already existed).
        skipped  — below similarity threshold OR dropped by max_links cap.
        errors   — write failed; exception was logged and swallowed.
    """

    created: list[dict] = field(default_factory=list)
    matched: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def extend(self, other: LinkJobResult) -> None:
        """Append each bucket from *other* onto self (in-place merge)."""
        self.created.extend(other.created)
        self.matched.extend(other.matched)
        self.skipped.extend(other.skipped)
        self.errors.extend(other.errors)

    @property
    def freshness_ratio(self) -> float:
        """Ratio of new edges over all resolved edges (created + matched).

        Returns 0.0 when denominator is zero to avoid ZeroDivisionError.
        """
        denominator = len(self.created) + len(self.matched)
        if denominator == 0:
            return 0.0
        return len(self.created) / denominator

    def as_summary(self) -> str:
        """One-line human-readable summary of the job result."""
        return (
            f"created={len(self.created)} "
            f"matched={len(self.matched)} "
            f"skipped={len(self.skipped)} "
            f"errors={len(self.errors)} "
            f"freshness={self.freshness_ratio:.2f}"
        )
