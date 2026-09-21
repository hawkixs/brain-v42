"""Does the configured embedding model match the one that wrote the corpus?

Nothing in the schema answers that question. Nine tables carry one `embedding`
column each -- adrs, decisions, features, gitlab_events, indexed_plan_chunks,
indexed_plans, learnings, runbooks, snippets -- and not one of them records the
model that produced the vector. Qodo-Embed-1-1.5B and codestral-embed both emit
1536 dimensions, so pointing `BRAIN_EMBEDDING_MODEL` at the other one without a
full reindex raises nothing at all: pgvector keeps answering, every distance
becomes noise, and search quality collapses in silence.

The check is empirical rather than declared: re-embed the CURRENT text of a
sample of already-embedded rows with the CURRENTLY configured backend, and
compare each fresh vector with the stored one. A declared fingerprint would
only prove what a config file claims; this proves what the corpus contains.

This module is the pure half -- the verdict, given similarities. Sampling,
text composition and the embedding call live in the CLI
(`scripts/check_embedding_model_drift.py`) so the decision stays testable
without a database or a provider.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import StrEnum

#: Cosine lives in [-1, 1]; the slack absorbs float error on a self-comparison
#: that should land exactly on 1.0, without admitting a parsing bug.
_COSINE_SLACK = 1e-6

#: Calibrated on the live corpus, 2026-09-21, 24 learnings against the
#: production qodo endpoint -- measured, not guessed:
#:
#:   same model, same text          median 0.9999, min 0.9984
#:   same model, UNRELATED text     median 0.5353, max 0.6176
#:   document-prefix change         median 0.9939, min 0.9888  (tolerated)
#:
#: 0.95 sits in the empty band between 0.62 and 0.998. It therefore catches
#: what destroys comparability -- a foreign model puts vectors in an unrelated
#: basis and scores below the unrelated-content ceiling -- while tolerating
#: what merely nudges them, such as adding an instruction prefix, which was
#: measured at 0.9939 and leaves the corpus usable. Re-measure before moving
#: this number; do not copy it forward.
DEFAULT_THRESHOLD = 0.95


class DriftVerdict(StrEnum):
    """What the sample says about the corpus, never about the config file."""

    MATCH = "match"
    DRIFT = "drift"
    UNMEASURABLE = "unmeasurable"


@dataclass(frozen=True)
class SampleComparison:
    """One stored vector re-measured against a freshly embedded current text."""

    entity_type: str
    entity_id: str
    similarity: float


@dataclass(frozen=True)
class DriftReport:
    """The verdict plus the numbers an operator has to read before switching."""

    verdict: DriftVerdict
    threshold: float
    sampled: int
    outliers: int
    median: float | None
    minimum: float | None
    maximum: float | None

    @property
    def exit_code(self) -> int:
        """0 match, 1 drift, 2 unmeasurable -- an unanswered question is never a pass."""
        if self.verdict is DriftVerdict.MATCH:
            return 0
        if self.verdict is DriftVerdict.DRIFT:
            return 1
        return 2


def final_exit_code(report: DriftReport, problems: list[str]) -> int:
    """The process exit code, once collection problems are taken into account.

    `DriftReport.exit_code` judges the rows that answered. This judges the RUN.
    A sample where one type failed entirely still produces a MATCH on the rest,
    and a provider-switch preflight that exits 0 on four fifths of a question
    is a gate that does not gate -- measured 2026-09-21, when a concurrent
    bench saturated the shim and one whole batch came back 503.

    Proven drift still wins: it is a finding, and downgrading it to "could not
    measure" would discard the one fact worth acting on.
    """
    if report.verdict is DriftVerdict.DRIFT:
        return 1
    if problems:
        return 2
    return report.exit_code


def classify_drift(
    samples: list[SampleComparison],
    threshold: float = DEFAULT_THRESHOLD,
) -> DriftReport:
    """Turn a sample of similarities into a verdict.

    Reads the MEDIAN, not the worst row. Rows are edited between the write of
    their vector and this check -- `content_updated_at` moves on its own
    trigger while nothing rewrites the embedding -- so a handful of genuinely
    stale rows is the normal state of a live corpus. Judging on the minimum
    would report a provider swap after any ordinary edit, and a check that
    cries wolf on a normal Tuesday is a check nobody runs before a real switch.

    An empty sample is UNMEASURABLE, never MATCH: no embedded row means the
    question was not answered, and returning success there would green-light a
    provider switch on no evidence at all.
    """
    for sample in samples:
        if not (-1.0 - _COSINE_SLACK <= sample.similarity <= 1.0 + _COSINE_SLACK):
            raise ValueError(
                f"similarity {sample.similarity!r} for {sample.entity_type} "
                f"{sample.entity_id} is outside the cosine range [-1, 1]"
            )

    if not samples:
        return DriftReport(
            verdict=DriftVerdict.UNMEASURABLE,
            threshold=threshold,
            sampled=0,
            outliers=0,
            median=None,
            minimum=None,
            maximum=None,
        )

    similarities = [s.similarity for s in samples]
    median = statistics.median(similarities)
    verdict = DriftVerdict.MATCH if median >= threshold else DriftVerdict.DRIFT

    return DriftReport(
        verdict=verdict,
        threshold=threshold,
        sampled=len(samples),
        outliers=sum(1 for s in similarities if s < threshold),
        median=median,
        minimum=min(similarities),
        maximum=max(similarities),
    )
