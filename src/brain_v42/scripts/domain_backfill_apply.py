#!/usr/bin/env python3
"""Étape C du domain backfill : applique un rapport jsonl REVIEWÉ au graph.

Consomme logs/domain_backfill/<date>.jsonl (produit par
scripts.domain_backfill, proposer-only) et écrit les edges
BELONGS_TO_DOMAIN via GraphService (upsert_domain + link_entity_to_domain).

Garde-fous :
- DRY-RUN par défaut — l'écriture exige --wet (pattern killswitch maison).
- Le gate de qualité est la REVIEW HUMAINE du rapport .md, pas
  --min-confidence : deepseek rend tout en "high" (calibration plate,
  learning 6dfb9064), donc le filtre confidence ne filtre rien en pratique.
- `unknown` n'est JAMAIS appliqué ; domaine re-validé contre ALLOWED_DOMAINS.
- Idempotent : un edge existant ressort "matched" (pas d'erreur).

Usage:
    python -m scripts.domain_backfill_apply --report logs/domain_backfill/2026-07-03.jsonl
    python -m scripts.domain_backfill_apply --report ... --wet
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from brain_v42.services.graph_service import ALLOWED_DOMAINS

_CONFIDENCE_RANK: dict[str, int] = {"high": 0, "medium": 1, "low": 2}


class GraphWriterLike(Protocol):
    async def upsert_domain(self, name: str) -> str: ...

    async def link_entity_to_domain(self, entity_id: uuid.UUID, domain_name: str) -> str: ...


@dataclass(frozen=True)
class ApplyOutcome:
    entity_id: str
    domain: str
    outcome: str
    detail: str = ""


def load_proposals(path: Path) -> list[dict]:
    """Parse le rapport jsonl. Ligne malformée = ValueError avec le numéro
    (le fichier est machine-généré : une corruption doit stopper net)."""
    proposals: list[dict] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            proposals.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"rapport corrompu ligne {lineno}: {exc}") from exc
    return proposals


def filter_appliable(
    proposals: list[dict], min_confidence: str
) -> tuple[list[dict], list[ApplyOutcome]]:
    """Sépare les propositions applicables des skips (tous visibles)."""
    max_rank = _CONFIDENCE_RANK[min_confidence]
    kept: list[dict] = []
    skipped: list[ApplyOutcome] = []
    for p in proposals:
        entity_id = str(p.get("entity_id", ""))
        domain = str(p.get("domain", ""))
        confidence = str(p.get("confidence", ""))
        if domain == "unknown":
            skipped.append(ApplyOutcome(entity_id, domain, "skipped_unknown"))
            continue
        if domain not in ALLOWED_DOMAINS:
            skipped.append(ApplyOutcome(entity_id, domain, "skipped_invalid_domain", domain))
            continue
        if _CONFIDENCE_RANK.get(confidence, 99) > max_rank:
            skipped.append(ApplyOutcome(entity_id, domain, "skipped_confidence", confidence))
            continue
        try:
            uuid.UUID(entity_id)
        except ValueError:
            skipped.append(ApplyOutcome(entity_id, domain, "skipped_bad_id"))
            continue
        kept.append(p)
    return kept, skipped


async def apply_proposals(
    graph: GraphWriterLike, proposals: list[dict], *, wet: bool
) -> list[ApplyOutcome]:
    """Applique (ou simule) les propositions déjà filtrées.

    Un domaine est upserté UNE fois ; si son upsert échoue, toutes les lignes
    de ce domaine sortent en domain_upsert_<result> sans tentative de link.
    """
    if not wet:
        return [ApplyOutcome(str(p["entity_id"]), str(p["domain"]), "dry_run") for p in proposals]

    upsert_results: dict[str, str] = {}
    outcomes: list[ApplyOutcome] = []
    for p in proposals:
        entity_id = str(p["entity_id"])
        domain = str(p["domain"])
        if domain not in upsert_results:
            upsert_results[domain] = await graph.upsert_domain(domain)
        if upsert_results[domain] != "ok":
            outcomes.append(
                ApplyOutcome(entity_id, domain, f"domain_upsert_{upsert_results[domain]}")
            )
            continue
        result = await graph.link_entity_to_domain(uuid.UUID(entity_id), domain)
        outcomes.append(ApplyOutcome(entity_id, domain, result))
    return outcomes


def write_apply_report(report_path: Path, outcomes: list[ApplyOutcome], *, wet: bool) -> Path:
    """Écrit <rapport>-apply.jsonl à côté du rapport d'entrée."""
    out_path = report_path.with_name(f"{report_path.stem}-apply.jsonl")
    with out_path.open("w") as fh:
        for o in outcomes:
            fh.write(json.dumps({"wet": wet, **asdict(o)}, ensure_ascii=False) + "\n")
    return out_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="domain_backfill_apply",
        description="Apply reviewed domain-backfill proposals to the graph.",
    )
    parser.add_argument("--report", type=Path, required=True, help="rapport jsonl du backfill")
    parser.add_argument(
        "--min-confidence",
        choices=sorted(_CONFIDENCE_RANK, key=_CONFIDENCE_RANK.get),  # type: ignore[arg-type]
        default="high",
        help="gate de confiance (rappel : deepseek rend tout high)",
    )
    parser.add_argument(
        "--wet",
        action="store_true",
        help="ÉCRIT réellement les edges (défaut : dry-run)",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    from neo4j import AsyncGraphDatabase  # import local : dep runtime du serveur
    from pydantic import ValidationError

    from brain_v42.config import Settings
    from brain_v42.db.engine import get_session_factory
    from brain_v42.services.durable_graph_service import build_durable_graph_stack
    from brain_v42.services.graph_service import GraphService

    try:
        # pydantic-settings lit l'env ; mypy ne le sait pas (pattern config.py).
        settings = Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(f"Config invalide (env/.env manquant ?) : {exc}", file=sys.stderr)
        return 2
    if not settings.neo4j_url:
        print("NEO4J_URL absent — requis pour écrire le graph.", file=sys.stderr)
        return 1

    proposals = load_proposals(args.report)
    kept, skipped = filter_appliable(proposals, args.min_confidence)

    driver = AsyncGraphDatabase.driver(
        settings.neo4j_url, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    graph_service = GraphService(driver, timeout=settings.neo4j_timeout)
    session_factory = get_session_factory() if settings.graph_ledger_write_enabled else None
    durable_stack = build_durable_graph_stack(
        graph_service,
        session_factory,
        settings,
        neo4j_driver=driver,
    )
    try:
        if args.wet and durable_stack.ledger is not None:
            await durable_stack.ledger.assert_schema_ready()
        applied = await apply_proposals(durable_stack.service, kept, wet=args.wet)
    finally:
        await driver.close()

    outcomes = [*applied, *skipped]
    out_path = write_apply_report(args.report, outcomes, wet=args.wet)

    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.outcome] = counts.get(o.outcome, 0) + 1
    mode = "WET" if args.wet else "DRY-RUN"
    summary = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    print(f"[{mode}] proposals={len(proposals)} {summary}")
    print(f"apply-report: {out_path}")

    errors = sum(
        v
        for k, v in counts.items()
        if k in {"error", "missing_node"} or k.startswith("domain_upsert_")
    )
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.report.is_file():
        print(f"Rapport introuvable : {args.report}", file=sys.stderr)
        return 2
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
