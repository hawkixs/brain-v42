#!/usr/bin/env python3
"""Cross-project resonance detector — Dream v3 Spec C MVP β.

Surfaces pairs of decisions from DIFFERENT projects within the same
knowledge domain whose embeddings are highly similar (cosine >= threshold).
The algorithm does not judge convergence vs drift — a heuristic hint is
attached, the human interprets.

DRY_RUN by default: writes a markdown report only. WET mode (opt-in,
double-gated) additionally writes insulated learnings to the brain.

Usage:
    python -m scripts.dream.cross_project_resonance [--mode dry_run|wet]
        [--domains ml,memory] [--date YYYY-MM-DD]

Killswitch: BRAIN_DREAM_CROSS_PROJECT_ENABLED (default false → exit 0 no-op).
Threshold: thresholds.by_name("cross_project_resonance_min") — no env var.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain_v42 import thresholds
from brain_v42.config import Settings
from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY
from brain_v42.models.learning import LearningCreate
from brain_v42.repositories.pg_decision import PgDecisionRepo
from brain_v42.repositories.pg_learning import PgLearningRepo
from brain_v42.services.graph_service import ALLOWED_DOMAINS, GraphService

logger = structlog.get_logger(__name__)

MIN_DECISIONS_PER_DOMAIN = 5
MAX_DECISIONS_PER_DOMAIN = 200  # hard cap to bound PG pair-compute cost
MAX_PAIRS_PER_NIGHT = 20
PHASE = "RESONANCE"  # dream_runs.phase is VARCHAR(10)

_SF = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class ResonancePair:
    a_id: UUID
    b_id: UUID
    a_project: str
    b_project: str
    a_title: str
    b_title: str
    a_created_at: date
    b_created_at: date
    cosine: float
    domain: str

    @property
    def hint(self) -> str:
        """Heuristic only, never authoritative."""
        nums_a = set(re.findall(r"\d+\.\d+", self.a_title))
        nums_b = set(re.findall(r"\d+\.\d+", self.b_title))
        if nums_a and nums_b and nums_a != nums_b:
            return f"drift candidate (numeric divergence: {sorted(nums_a)} vs {sorted(nums_b)})"
        return "convergence likely (no numeric divergence detected)"

    @property
    def dedup_key(self) -> str:
        """SHA256 of the canonical pair fingerprint — WET idempotency."""
        lo, hi = sorted([str(self.a_id), str(self.b_id)])
        return hashlib.sha256(f"{lo}|{hi}|{self.domain}".encode()).hexdigest()

    def format_insight(self) -> str:
        """Body for the WET-mode learning."""
        return (
            f"Cross-project resonance in domain '{self.domain}' (cosine={self.cosine:.3f}):\n"
            f"- [{self.a_project}] {self.a_title} ({self.a_created_at})\n"
            f"- [{self.b_project}] {self.b_title} ({self.b_created_at})\n"
            f"Hint: {self.hint}"
        )


def build_report_path(report_date: str) -> Path:
    """artifacts/dream/cross_project_resonance_<UTC-ISO-date>.md (repo-relative)."""
    return Path("artifacts") / "dream" / f"cross_project_resonance_{report_date}.md"


def render_markdown_report(
    pairs: list[ResonancePair], *, threshold: float, run_id: int, report_date: str
) -> str:
    domains_with_pairs: dict[str, list[ResonancePair]] = {}
    for p in pairs:
        domains_with_pairs.setdefault(p.domain, []).append(p)
    lines = [
        f"# Cross-Project Resonance — {report_date}",
        "",
        f"Threshold: {threshold:.2f} · Pairs found: {len(pairs)} · "
        f"Domains with pairs: {len(domains_with_pairs)} · Run ID: {run_id}",
        "",
    ]
    if not pairs:
        lines.append("No cross-project resonance pairs above threshold this run.")
        return "\n".join(lines) + "\n"
    for domain in sorted(domains_with_pairs):
        dpairs = domains_with_pairs[domain]
        plural = "pair" if len(dpairs) == 1 else "pairs"
        lines.append(f"## Domain: {domain} ({len(dpairs)} {plural})")
        lines.append("")
        for n, p in enumerate(dpairs, start=1):
            lines.append(f"### Pair {n} — cosine={p.cosine:.2f}")
            lines.append(
                f'- [{p.a_project}] Decision {str(p.a_id)[:8]}… · "{p.a_title}" · {p.a_created_at}'
            )
            lines.append(
                f'- [{p.b_project}] Decision {str(p.b_id)[:8]}… · "{p.b_title}" · {p.b_created_at}'
            )
            lines.append(f"- Hint: {p.hint}")
            lines.append("")
    return "\n".join(lines)


def _load_settings() -> Settings:
    return Settings()


def _build_deps(settings: Settings) -> tuple[_SF, GraphService, PgDecisionRepo]:
    """Construct PG session factory + Neo4j + decision repo. Single test seam.

    Uses the global engine singleton (brain_v42.db.engine.get_session_factory).
    session_factory is injected into PgDecisionRepo so all repos share the same
    DI contract (BasePgRepository vague 3 wiring convention).
    One-shot script + single asyncio.run loop, so the factory/loop-binding
    gotcha (pytest-asyncio singleton learning) does not apply.
    """
    from neo4j import AsyncGraphDatabase  # noqa: PLC0415

    from brain_v42.db.engine import get_session_factory  # noqa: PLC0415

    session_factory = get_session_factory()
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_url or "bolt://localhost:7687",
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    graph = GraphService(driver, timeout=settings.neo4j_timeout)
    return session_factory, graph, PgDecisionRepo(session_factory)


async def _insert_run(session_factory: _SF, *, run_date: date, dry_run: bool) -> int:
    """Insert the RESONANCE dream_runs row up-front; returns run id.

    status='fail' placeholder — flipped to 'done' by _finish_run. A crash
    mid-run therefore leaves an honest 'fail' row (status enum: done|timeout|fail).

    `project_key` gets the global sentinel even though this phase is DEAD — no
    caller anywhere in the repo, and `dream_runs` has never carried a
    'RESONANCE' row. Writing it keeps this file from being the one inconsistent
    writer the day somebody re-wires it. Wiring it, or deleting it, are two
    other decisions (operator, 2026-08-09).
    """
    stmt = sa.text(
        "INSERT INTO dream_runs "
        "(run_date, phase, status, project_key, phase_dry_run, error_message) "
        "VALUES (:rd, :phase, 'fail', :project_key, :dry, 'incomplete') RETURNING id"
    )
    async with session_factory() as session:
        result = await session.execute(
            stmt,
            {
                "rd": run_date,
                "phase": PHASE,
                "project_key": GLOBAL_PHASE_PROJECT_KEY,
                "dry": dry_run,
            },
        )
        await session.commit()
        return int(result.scalar_one())


async def _finish_run(
    session_factory: _SF,
    run_id: int,
    *,
    status: str,
    duration_s: float,
    error: str | None = None,
) -> None:
    stmt = sa.text(
        "UPDATE dream_runs SET status = :st, duration_s = :du, error_message = :err WHERE id = :id"
    )
    async with session_factory() as session:
        await session.execute(stmt, {"st": status, "du": duration_s, "err": error, "id": run_id})
        await session.commit()


async def _learning_exists(session_factory: _SF, dedup_key: str) -> bool:
    stmt = sa.text("SELECT 1 FROM learnings WHERE metadata->>'dedup_key' = :dk LIMIT 1")
    async with session_factory() as session:
        result = await session.execute(stmt, {"dk": dedup_key})
        return result.scalar() is not None


async def _write_learning(session_factory: _SF, pair: ResonancePair, run_id: int) -> None:
    repo = PgLearningRepo(session_factory)
    await repo.create(
        LearningCreate(
            topic=f"cross_project_resonance/{pair.domain}",
            insight=pair.format_insight(),
            source="scripts/dream/cross_project_resonance.py",
            source_type="automated",
            confidence="low",
            project_key="brain-v42",
            tags=["dream", "cross_project_resonance", pair.domain, "EXCLUDE_FROM_PROMOTE"],
            metadata={"dedup_key": pair.dedup_key, "dream_run_id": run_id},
        )
    )


def _write_report_file(
    path: Path,
    pairs: list[ResonancePair],
    *,
    threshold: float,
    run_id: int,
    report_date: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_markdown_report(pairs, threshold=threshold, run_id=run_id, report_date=report_date),
        encoding="utf-8",
    )


async def run(*, mode: str, domains: list[str] | None, date_str: str | None) -> int:
    settings = _load_settings()
    if not settings.brain_dream_cross_project_enabled:
        logger.info("cross_project_resonance_disabled")
        return 0

    spec = thresholds.by_name("cross_project_resonance_min")
    if spec is None:
        logger.error("threshold_missing", name="cross_project_resonance_min")
        return 1
    threshold = spec.value

    report_date = date_str or datetime.now(tz=UTC).date().isoformat()
    run_date = date.fromisoformat(report_date)
    target_domains = domains or sorted(ALLOWED_DOMAINS)

    session_factory, graph, repo = _build_deps(settings)
    run_id = await _insert_run(session_factory, run_date=run_date, dry_run=(mode != "wet"))
    started = datetime.now(tz=UTC)

    try:
        all_pairs: list[ResonancePair] = []
        for domain in target_domains:
            ids = await graph.fetch_decision_ids_in_domain(domain)
            if len(ids) < MIN_DECISIONS_PER_DOMAIN:
                continue
            rows = await repo.fetch_cross_project_resonance_pairs(
                ids=ids[:MAX_DECISIONS_PER_DOMAIN],  # UUID strings, cast in SQL
                threshold=threshold,
            )
            all_pairs.extend(ResonancePair(domain=domain, **row) for row in rows)

        all_pairs.sort(key=lambda p: p.cosine, reverse=True)
        all_pairs = all_pairs[:MAX_PAIRS_PER_NIGHT]

        _write_report_file(
            build_report_path(report_date),
            all_pairs,
            threshold=threshold,
            run_id=run_id,
            report_date=report_date,
        )

        if mode == "wet":
            # Safeguard 3c (spec): fresh env re-read right before writes —
            # a stale `settings` object would make this check dead code.
            if not _load_settings().brain_dream_cross_project_enabled:
                await _finish_run(
                    session_factory,
                    run_id,
                    status="fail",
                    duration_s=(datetime.now(tz=UTC) - started).total_seconds(),
                    error="WET blocked: env disabled",
                )
                return 1
            written = 0
            for pair in all_pairs:
                if await _learning_exists(session_factory, pair.dedup_key):
                    continue
                await _write_learning(session_factory, pair, run_id)
                written += 1
            logger.info("resonance_wet_written", count=written)

        await _finish_run(
            session_factory,
            run_id,
            status="done",
            duration_s=(datetime.now(tz=UTC) - started).total_seconds(),
        )
        logger.info("resonance_done", pairs=len(all_pairs), mode=mode, run_id=run_id)
        return 0
    except Exception as exc:
        await _finish_run(
            session_factory,
            run_id,
            status="fail",
            duration_s=(datetime.now(tz=UTC) - started).total_seconds(),
            error=str(exc)[:500],
        )
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["dry_run", "wet"], default="dry_run")
    parser.add_argument(
        "--domains",
        default=None,
        help="Comma-separated domain subset (default: all 9)",
    )
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: today UTC)")
    args = parser.parse_args(argv)
    domains = args.domains.split(",") if args.domains else None
    return asyncio.run(run(mode=args.mode, domains=domains, date_str=args.date))


if __name__ == "__main__":
    sys.exit(main())
