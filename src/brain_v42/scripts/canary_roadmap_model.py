#!/usr/bin/env python
"""Paired canary: every candidate model on the SAME real batches.

Why this script exists, and why a liveness probe is not enough: on 2026-08-11, a
16-token probe reported `minimaxai/minimax-m3` alive in 2.4 s. The 2026-08-05
canary had already measured it TIMING OUT on the real prompt — the comment at
scripts/roadmap_curate.py:60-67 keeps the trace. A model that answers "ALIVE" at
sixteen tokens can die on a consolidator prompt of several thousand.

This script PERSISTS NOTHING. It reads the batches through the real path
(`fetch_project_batches`) and curates them through the night's real entry point,
`curate_batch`. `persist_proposals` and `apply_proposals` are never called.

It called `_curate_llm_attempt` until 2026-08-17 — the layer BELOW the night's.
It was therefore missing four constraints: the `batch_llm_window` window (60 s at
ten batches, against httpx's 180 s read timeout), the completion cap, batch
compaction, and the CIRCUIT that removes a failing primary from every subsequent
batch. That is what switched production to `mistralai/mistral-nemotron` on
2026-08-16: 3/3 valid batches at the canary, 10/10 on the fallback from the very
first night.

The useful verdict is therefore not "answers / does not answer", nor even JSON
validity alone, but what the CANDIDATE carried itself — separated from what the
fallback rescued under its name. The night budget is finite: at ten projects,
720 s.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from brain_v42.scripts.domain_backfill import DEFAULT_BASE_URL, load_env_file
from brain_v42.scripts.roadmap_curate import (
    _API_KEY_VAR,
    _ENV_FILE,
    DEFAULT_ROADMAP_FALLBACK_MODEL,
    NIGHT_BUDGET_S,
    BatchOutcome,
    ProjectBatch,
    batch_llm_window,
    curate_batch,
    fetch_project_batches,
)

# `NIGHT_BUDGET_S` is IMPORTED, no longer copied. The value used to live here in
# duplicate (720.0), and two sources of truth for one budget can only diverge:
# the day the night changed budget, the canary would keep deriving its windows
# from the old one without a single line changing colour.

# The --batches DEFAULT is the --limit dream.sh passes to the night. At 3, the
# same budget split into 120 s windows (growing to 200) where ten batches bound
# them to 60 s: the 2026-08-29 canary validated, under that gentle regime, a
# fallback at 74.5 s/batch that would have timed out on every production
# attempt. Pinned against dream.sh by
# test_canary_roadmap_matches_night_regime.test_the_cli_default_batch_count_is_the_nights.
DEFAULT_CANARY_BATCHES = 10


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


@dataclass
class ModelMeasurement:
    """What a candidate carried ITSELF, separated from what the fallback rescued."""

    model: str
    durations: list[float] = field(default_factory=list)
    carried_by_primary: int = 0
    rescued_by_fallback: int = 0
    failed: int = 0
    drafts_total: int = 0
    errors: list[str] = field(default_factory=list)
    # 1-based index of the batch that removed the primary for the rest of the run.
    circuit_opened_at: int | None = None
    outcomes: list[BatchOutcome] = field(default_factory=list)

    @property
    def batches(self) -> int:
        return len(self.durations)

    @property
    def mean_duration_s(self) -> float:
        return statistics.mean(self.durations) if self.durations else 0.0

    @property
    def verdict(self) -> str:
        """The verdict is about the CANDIDATE, never about what the fallback rescued.

        That is the whole fix: a run served entirely by the fallback reported
        "10/10 valid" and switched production onto a dead model.
        """
        if self.carried_by_primary == self.batches and self.batches:
            return "OK"
        return "PART" if self.carried_by_primary else "MORT"


async def measure_model(
    client: Any,
    model: str,
    batches: list[ProjectBatch],
    *,
    budget_s: float = NIGHT_BUDGET_S,
    fallback_model: str | None = None,
    curate: Any = curate_batch,
    clock: Any = time.monotonic,
    as_dry_primary: bool = False,
) -> ModelMeasurement:
    """Measure a candidate UNDER the night's constraints, not under its own.

    Goes through `curate_batch`, the entry point the night uses, and not through
    `_curate_llm_attempt` below it: the `batch_llm_window` window, the
    completion cap, batch compaction and the CIRCUIT then come from the same
    code as production. `disabled_models` is a single set shared by every batch
    — it is what reproduces "one lost batch condemns the next nine", the fact
    the original canary could not see.

    `as_dry_primary` measures a candidate IN the regime it would have once
    adopted: `PROPOSER_ONLY_MODELS` being derived from `DEFAULT_ROADMAP_MODEL`, a
    candidate enters it on the day of its switch and changes routing at the same
    instant. Without this flag we would still be measuring a regime production
    will not have.
    """
    measurement = ModelMeasurement(model=model)
    circuit: set[str] = set()
    total = len(batches)
    # Asked of curate_batch EXPLICITLY, never by mutating the global
    # PROPOSER_ONLY_MODELS: a leaking monkeypatch would leave production routed
    # differently from how it was, and `check_container_image_pins` rightly
    # forbids writing a module attribute. None = nominal routing, decided by set
    # membership, strictly unchanged.
    proposer_only = True if as_dry_primary else None
    t0 = clock()

    for index, batch in enumerate(batches, start=1):
        elapsed = clock() - t0
        window = batch_llm_window(budget_s, elapsed, total - index + 1)
        started = clock()
        try:
            outcome = await curate(
                client,
                model,
                batch,
                _sleep,
                llm_timeout_s=window,
                fallback_model=fallback_model,
                disabled_models=circuit,
                proposer_only=proposer_only,
            )
        except Exception as exc:  # noqa: BLE001 — we MEASURE the failure, we do not mask it
            measurement.durations.append(clock() - started)
            measurement.failed += 1
            measurement.errors.append(f"{type(exc).__name__}: {str(exc)[:90]}")
            continue

        measurement.durations.append(clock() - started)
        measurement.outcomes.append(outcome)
        if outcome.primary_error and measurement.circuit_opened_at is None:
            measurement.circuit_opened_at = index
        if outcome.primary_error:
            measurement.errors.append(outcome.primary_error[:90])

        if outcome.failed:
            measurement.failed += 1
            if outcome.error:
                measurement.errors.append(outcome.error[:90])
            continue
        measurement.drafts_total += len(outcome.drafts)
        if outcome.fallback_used:
            measurement.rescued_by_fallback += 1
        else:
            measurement.carried_by_primary += 1

    return measurement


def _proposals_payload(model: str, outcome: BatchOutcome) -> dict[str, Any]:
    """Make the proposals READABLE, without pretending to score them.

    The triplet this script measures — JSON validity, s/batch, NUMBER of
    proposals — says nothing about what is proposed. Two models can each return
    thirty proposals, one archiving live features and the other seeing the real
    duplicates, and the table would rank them equal.

    A bare UUID cannot be judged: every proposal therefore comes out with the
    feature it targets — name, status, pinning — and `merge` names the feature
    into which it would absorb its target. Without that, nobody can say whether
    an `archive` is a good call or the destruction of a commitment.

    No scoring here, deliberately: a score returned by the layer that produces
    the proposals would carry no weight in the decision.
    """
    by_id = {feature.id: feature for feature in outcome.batch.features}

    proposals: list[dict[str, Any]] = []
    for draft in outcome.drafts:
        target = by_id.get(draft.feature_id)
        payload = dict(draft.payload)
        if draft.op == "merge" and "into" in payload:
            winner = by_id.get(UUID(str(payload["into"])))
            payload["into_name"] = winner.name if winner else "(hors batch)"
        proposals.append(
            {
                "op": draft.op,
                "feature_id": str(draft.feature_id),
                "target": {
                    "name": target.name if target else "(hors batch)",
                    "status": target.status if target else None,
                    "pinned": target.pinned if target else None,
                },
                "payload": payload,
                "rationale": draft.rationale,
            }
        )

    return {
        "model": model,
        "project_key": outcome.batch.project_key,
        "features_in_batch": [
            {"name": f.name, "status": f.status, "pinned": f.pinned} for f in outcome.batch.features
        ],
        "proposals": proposals,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", required=True, help="candidats, séparés par des virgules")
    parser.add_argument(
        "--batches",
        type=int,
        default=DEFAULT_CANARY_BATCHES,
        help="nombre de batches réels (défaut : le --limit de la nuit, fenêtres identiques)",
    )
    # `--proposer-only` was REMOVED. It picked the parser by hand, whereas
    # `curate_batch` — the night's entry point, now used here — decides for
    # itself by membership of PROPOSER_ONLY_MODELS. Keeping it would have
    # allowed measuring a parsing regime the night does not apply to this model,
    # that is, reproducing the very failure being fixed.
    parser.add_argument(
        "--fallback-model",
        default=DEFAULT_ROADMAP_FALLBACK_MODEL,
        help=(
            "secours de la nuit, câblé pour reproduire son régime EXACT (fenêtre "
            "de moitié, circuit). Les batches qu'il sauve sont comptés à part et "
            "ne créditent jamais le candidat. `--fallback-model ''` le débranche."
        ),
    )
    parser.add_argument(
        "--as-dry-primary",
        action="store_true",
        help=(
            "mesurer le candidat DANS le régime qu'il aurait une fois primaire "
            "DRY (PROPOSER_ONLY_MODELS est dérivé de DEFAULT_ROADMAP_MODEL, donc "
            "l'adoption change le routage au moment même de la bascule)"
        ),
    )
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=NIGHT_BUDGET_S,
        help=f"budget nuit dont les fenêtres sont dérivées (défaut: {NIGHT_BUDGET_S:.0f}s)",
    )
    parser.add_argument(
        "--dump-proposals",
        metavar="PATH",
        help="écrire le CONTENU des propositions en JSON, pour un jugement de qualité",
    )
    args = parser.parse_args()

    load_env_file(_ENV_FILE)
    api_key = os.environ.get(_API_KEY_VAR)
    if not api_key:
        print(f"! {_API_KEY_VAR} absent de {_ENV_FILE}")
        return 2

    from brain_v42.db.engine import get_session_factory

    session_factory = get_session_factory()
    batches = await fetch_project_batches(session_factory, args.batches)
    if not batches:
        print("! aucun batch — rien à mesurer")
        return 2

    print(f"Batches réels : {len(batches)}")
    for batch in batches:
        print(f"  - {batch.project_key} : {len(batch.features)} features")
    print()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    client = httpx.AsyncClient(
        base_url=os.environ.get("BRAIN_NVIDIA_BASE_URL", DEFAULT_BASE_URL),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0),
    )

    rows = []
    dumped: list[dict[str, Any]] = []
    try:
        for model in models:
            # Sequential and never concurrent: two candidates in parallel would
            # compete for the provider's queue and skew precisely the
            # measurement being sought.
            measurement = await measure_model(
                client,
                model,
                batches,
                budget_s=args.budget_seconds,
                fallback_model=args.fallback_model,
                as_dry_primary=args.as_dry_primary,
            )
            if args.dump_proposals:
                dumped.extend(
                    _proposals_payload(model, outcome)
                    for outcome in measurement.outcomes
                    if not outcome.failed
                )
            rows.append(measurement)
            print(
                f"[{measurement.verdict:4}] {model:42} "
                f"{measurement.carried_by_primary}/{measurement.batches} portés  "
                f"{measurement.mean_duration_s:6.1f} s/batch  "
                f"{measurement.drafts_total} propositions"
            )
            if measurement.rescued_by_fallback:
                # The line the original canary could not write, for want of
                # going through the layer where the fallback exists.
                print(
                    f"         └─ {measurement.rescued_by_fallback} batch(es) "
                    f"sauvés par {args.fallback_model} — NON portés par le candidat"
                )
            if measurement.circuit_opened_at is not None:
                print(
                    f"         └─ circuit ouvert au batch {measurement.circuit_opened_at}: "
                    f"le primaire est écarté de tous les batches suivants"
                )
            for err in measurement.errors[:2]:
                print(f"         └─ {err}")
    finally:
        await client.aclose()

    if args.dump_proposals:
        Path(args.dump_proposals).write_text(
            json.dumps(dumped, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n-> contenu des propositions écrit dans {args.dump_proposals}")

    print()
    print("=" * 100)
    print(
        f"{'MODELE':42} {'car':>3} {'valide':>7} {'prop':>5} {'s/batch':>8} {'10 proj':>8}  verdict"
    )
    print("-" * 100)
    for measurement in sorted(rows, key=lambda m: (-m.carried_by_primary, m.mean_duration_s)):
        model = measurement.model
        valid = measurement.carried_by_primary
        total = measurement.batches
        mean = measurement.mean_duration_s
        drafts = measurement.drafts_total
        projected = mean * 10
        if measurement.circuit_opened_at is not None:
            # Priority over everything else: an open circuit means the night
            # would stop calling this model after this batch. Ranking it on
            # validity alone would let through a candidate production would no
            # longer use.
            verdict = (
                f"ÉCARTÉ — circuit ouvert au batch {measurement.circuit_opened_at}, "
                f"{measurement.rescued_by_fallback} batch(es) servis par le secours"
            )
        elif valid < total:
            verdict = "ÉCARTÉ — pas 100 % porté par le candidat"
        elif projected > NIGHT_BUDGET_S:
            verdict = f"ÉCARTÉ — {projected:.0f} s > budget nuit {NIGHT_BUDGET_S:.0f} s"
        elif drafts == 0:
            # Valid and sterile: a model that never proposes anything passes
            # every shape guard and does none of the expected work.
            verdict = "SUSPECT — 0 proposition sur tous les batches"
        elif len(model) > 30:
            verdict = "retenable, mais migration 045 OBLIGATOIRE (>30 car.)"
        else:
            verdict = "retenable sans migration"
        print(
            f"{model:42} {len(model):>3} {valid}/{total:>5} {drafts:>5} "
            f"{mean:>8.1f} {projected:>7.0f}s  {verdict}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
