"""Post-run report for failed or incomplete Dream phases.

Closes the silent-crash observability gap (2026-05-02 / 05-03 PROMOTE crashes
went undetected for 2 days). Invoked by dream.sh at the end of a run when
FAIL_TOTAL > 0. Session briefings read failures directly from ``dream_runs``.

CLI:
    python -m scripts.dream.post_run_alert --date 2026-05-08 [--manifest PATH]

Exit codes:
    0  → rien à signaler, ou anomalie DÉJÀ rapportée ailleurs (dream.sh sort
         lui-même en 1 sur ses phases en échec)
    1  → outil ou base cassé
    2  → trou de couverture SILENCIEUX, écriture `dream_runs` déclarée en échec,
         ou manifeste incohérent/interrompu. Jamais rendu en mode repli.
         Ce 2 est PARTAGÉ avec l'erreur d'usage d'argparse — un `SystemExit` que
         le `except Exception` de `main()` ne peut pas intercepter — et avec
         celle de `uv` comme de l'interpréteur. `dream.sh` ne le croit donc que
         si la ligne machine `COVERAGE …` a été imprimée ; elle l'est pour TOUS
         les codes de sortie (`test_exit_codes_follow_the_verdict`), y compris
         celui-ci, ce qui rend la preuve positive toujours disponible.

Couverture (ticket 0a9c067e). Ce module compare depuis longtemps l'observé au
produit cartésien `{phase activée} × {projet du pool}` lu dans le drop-in
systemd, et il a tiré trois nuits de suite. Le défaut n'était pas son absence
mais sa TAILLE : `collector_dream.LOOP_PHASES` ne porte que `promote` et
`reorg`, et les quatre core phases n'ont aucune clé dans `_KS_KEYS` — les
ajouter là-bas serait un no-op. La nuit du 2026-08-16 a donc annoncé 20 phases
manquantes quand il en manquait 60.

L'attendu vient désormais du MANIFESTE que la nuit écrit elle-même, au site de
chaque décision (`scripts/dream.sh`, `scripts.dream.run_manifest`). Le drop-in
reste le chemin de REPLI, explicitement étiqueté, avec des noms de champs
différents : 23 paires attendues depuis le drop-in et 62 paires écrites la même
nuit ne sont pas comparables, et les poser côte à côte reproduirait le défaut
même que ce ticket dénonce.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from brain_v42.config import Settings
from brain_v42.db.tables import dream_runs
from brain_v42.metrics.collector_dream import (
    expected_dream_phase_pairs,
    expected_dream_phases,
)
from scripts.dream.run_manifest import (
    CoverageVerdict,
    Pair,
    RunManifest,
    classify_coverage,
    format_fallback_line,
    format_machine_line,
    format_silent_line,
    load_run_manifest,
)

MAX_REPORTED_FAILURES = 20
# Le plafond de FETCH est en amont du groupement, donc un plafond par projet ne
# le rattrape pas : à `MAX_REPORTED_FAILURES + 1`, un projet bruyant en fin de
# nuit remplirait les 21 lignes remontées et les projets calmes n'atteindraient
# jamais le groupeur. L'ordre est `created_at DESC`, donc ce sont les DERNIERS
# projets servis qui gagnaient. 200 lignes d'une seule journée ne coûtent rien.
MAX_FETCHED_FAILURES = 200
# §11 — `MAX_REPORTED_FAILURES = 20` avait été dimensionné pour les 9 phases
# d'une nuit à un projet. À dix projets la nuit en compte 63, et un plafond
# GLOBAL laisserait le premier projet consommer les vingt lignes : « N
# additional records omitted » masquerait alors des projets ENTIERS. Le plafond
# devient donc par projet dès que les lignes portent une clé.
MAX_REPORTED_FAILURES_PER_PROJECT = 8
FAILED_STATUSES = {"fail", "partial", "timeout"}
# Rendu de la sentinelle des phases sans projet. `*` en tête d'une ligne de
# rapport se lit comme une puce ou un joker, pas comme « les trois globales ».
GLOBAL_GROUP_LABEL = "global"
UNLABELLED_GROUP_LABEL = "unlabelled"

# Quatre wordings, parce que quatre PREMIERS GESTES distincts. Les confondre
# renvoie l'opérateur au mauvais endroit, ce qui use une alerte aussi sûrement
# que de ne pas l'émettre.
MISSING_EXPECTED_MESSAGE = "expected enabled phase missing from dream_runs"
COVERAGE_SILENT_MESSAGE = "counted OK by dream.sh but wrote no dream_runs row"
COVERAGE_WRITEFAIL_MESSAGE = (
    "dream.sh reported the dream_runs write FAILED for this phase "
    "(see the WARN line in the dated log)"
)
NO_ROW_AT_ALL_MESSAGE = (
    "not one expected phase wrote a dream_runs row — check the DB connection "
    "(DSN, credentials, schema) BEFORE opening any phase report"
)
INTERRUPTED_MESSAGE = (
    "the night never reached its closing block — this manifest is PARTIAL, "
    "so no green verdict is possible for it"
)
FALLBACK_WARNING = (
    "manifest absent — expectations derived from the drop-in, coverage limited to promote/reorg"
)
COVERAGE_HEADING = "### Couverture dream_runs"


def _detail_line(row: dict) -> str:
    phase = row.get("phase", "?")
    status = row.get("status", "?")
    err = row.get("error_message")
    err_line = err.splitlines()[0][:240] if err else "(no error_message captured)"
    return f"- {phase} [{status}]: {err_line}"


def _group_label(row: dict) -> str:
    from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY  # noqa: PLC0415

    project = row.get("project_key")
    if not project:
        # Ligne écrite avant la 042, ou par un écrivain non migré. `NULL` veut
        # dire « écrit avant la colonne », pas « projet inconnu à corriger ».
        return UNLABELLED_GROUP_LABEL
    if project == GLOBAL_PHASE_PROJECT_KEY:
        return GLOBAL_GROUP_LABEL
    return str(project)


def build_alert_insight(
    run_date: dt.date,
    failed: list[dict],
    *,
    total_failures: int | None = None,
) -> str:
    if not failed:
        raise ValueError("build_alert_insight requires a non-empty list of failed phases")

    total = len(failed) if total_failures is None else total_failures
    lines = [f"Dream run on {run_date.isoformat()} had {total} non-OK phase(s):", ""]

    if not any(row.get("project_key") for row in failed):
        # Aucune ligne ne porte de projet : nuit à un projet, ou corpus
        # antérieur à la 042. Rendu HISTORIQUE, à l'identique — grouper une
        # liste dont tous les éléments tomberaient dans le même seau ne
        # rendrait rien plus lisible et changerait un format déjà lu.
        lines.extend(_detail_line(row) for row in failed[:MAX_REPORTED_FAILURES])
        omitted = total - min(len(failed), MAX_REPORTED_FAILURES)
        if omitted:
            record_label = "record" if omitted == 1 else "records"
            lines.append(f"{omitted} additional failure {record_label} omitted")
    else:
        grouped: dict[str, list[dict]] = {}
        for row in failed:
            grouped.setdefault(_group_label(row), []).append(row)

        omitted = total - len(failed)
        for label in sorted(grouped):
            rows = grouped[label]
            lines.append(f"{label}:")
            lines.extend(
                f"  {_detail_line(row)}" for row in rows[:MAX_REPORTED_FAILURES_PER_PROJECT]
            )
            hidden = len(rows) - MAX_REPORTED_FAILURES_PER_PROJECT
            if hidden > 0:
                record_label = "record" if hidden == 1 else "records"
                lines.append(f"  {hidden} additional failure {record_label} omitted")
                omitted += hidden
            lines.append("")
        if omitted:
            record_label = "record" if omitted == 1 else "records"
            lines.append(f"{omitted} additional failure {record_label} omitted in total")

    lines.extend(
        [
            "",
            "Inspect logs at logs/dream/" + run_date.isoformat() + ".log",
            "Auto-generated by scripts.dream.post_run_alert.",
        ]
    )
    return "\n".join(lines)


def include_missing_expected_phases(
    rows: list[dict],
    expected: set[str],
    persisted_failures: list[dict] | None = None,
    *,
    expected_pairs: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Return lexical synthetic partials followed by persisted non-OK rows.

    ``expected_pairs`` porte le produit cartésien ``{phase} × {projet du pool}``
    quand le drop-in déclare un pool. Comparer sur les noms de phase seuls se
    désarme tout seul à plusieurs projets : qu'un projet saute `promote` et les
    autres le rendent « observé ». Quand les paires sont absentes — pool non
    déclaré — la comparaison par nom reste en place, à l'identique.
    """
    failed = (
        [row for row in rows if row.get("status") in FAILED_STATUSES]
        if persisted_failures is None
        else persisted_failures
    )

    if expected_pairs:
        observed_pairs = {(str(row["phase"]), str(row.get("project_key") or "")) for row in rows}
        missing = [
            {
                "phase": phase,
                "project_key": project,
                "status": "partial",
                "error_message": MISSING_EXPECTED_MESSAGE,
            }
            for phase, project in sorted(expected_pairs - observed_pairs)
        ]
        return missing + failed

    observed = {str(row["phase"]) for row in rows}
    missing = [
        {
            "phase": phase,
            "status": "partial",
            "error_message": MISSING_EXPECTED_MESSAGE,
        }
        for phase in sorted(expected - observed)
    ]
    return missing + failed


@dataclass(frozen=True)
class CoverageReport:
    """Le verdict de couverture, prêt à être imprimé.

    `verdict` est `None` en mode repli, et ce n'est pas un oubli : sans
    manifeste, `silent` n'est pas CALCULABLE. Un objet à moitié rempli
    inviterait à lire un zéro là où il n'y a pas de mesure.
    """

    block: tuple[str, ...]
    machine_line: str
    silent_line: str | None
    synthetic: tuple[dict, ...]
    escalates: bool
    verdict: CoverageVerdict | None


@dataclass(frozen=True)
class NightReport:
    """Le rapport opérationnel et son verdict de couverture, côte à côte."""

    report: str | None
    coverage: CoverageReport


def missing_rows_from_verdict(verdict: CoverageVerdict) -> list[dict]:
    """Traduit les trois classes ABSENTES qui méritent une ligne de rapport.

    `skipped` n'en produit aucune : la nuit a déclaré que personne n'avait tenté
    d'écrire, donc il n'y a rien à instruire. C'est exactement ce qui rend la
    couverture à six phases possible sans fabriquer de faux positif.
    """
    rows: list[dict] = []
    for pairs, message in (
        (verdict.silent, COVERAGE_SILENT_MESSAGE),
        (verdict.writefail, COVERAGE_WRITEFAIL_MESSAGE),
        (verdict.declared, MISSING_EXPECTED_MESSAGE),
    ):
        for phase, project in sorted(pairs):
            rows.append(
                {
                    "phase": phase,
                    "project_key": project,
                    "status": "partial",
                    "error_message": message,
                }
            )
    return rows


def coverage_from_manifest(
    observed_pairs: set[Pair],
    manifest: RunManifest,
) -> CoverageReport:
    """Le chemin nominal : l'attendu est ce que la NUIT a déclaré."""
    verdict = classify_coverage(observed_pairs, manifest)
    block = [
        COVERAGE_HEADING,
        "",
        f"expected {len(verdict.expected)} · written {len(verdict.written)} "
        f"· skipped {len(verdict.skipped)} · declared {len(verdict.declared)} "
        f"· write-failed {len(verdict.writefail)} · silent {len(verdict.silent)} "
        f"· extra {len(verdict.extra)}",
    ]
    if verdict.expected and not verdict.written:
        block.append(NO_ROW_AT_ALL_MESSAGE)
    if not verdict.complete:
        block.append(INTERRUPTED_MESSAGE)
    if not verdict.consistent:
        block.append(
            "manifest counters disagree — planned_phases="
            f"{manifest.meta.get('planned_phases', '-')} "
            f"total_phases={manifest.meta.get('total_phases', '-')} "
            f"reached={len(verdict.expected)}"
        )
    if manifest.warnings:
        block.append(f"{len(manifest.warnings)} malformed manifest line(s) ignored")
    block.append("")
    return CoverageReport(
        block=tuple(block),
        machine_line=format_machine_line(verdict),
        silent_line=format_silent_line(verdict),
        synthetic=tuple(missing_rows_from_verdict(verdict)),
        escalates=verdict.escalates,
        verdict=verdict,
    )


def coverage_fallback(*, expected: int, observed: int, missing: int) -> CoverageReport:
    """Le repli — le chemin d'aujourd'hui, dit en toutes lettres.

    Il ne rend JAMAIS 2. Sans manifeste, une paire absente peut aussi bien être
    un trou qu'une phase que la nuit n'a jamais tentée, et rougir l'unité sur
    une indécidable est la meilleure façon de rendre l'alarme illisible.
    """
    return CoverageReport(
        block=(COVERAGE_HEADING, "", FALLBACK_WARNING, ""),
        machine_line=format_fallback_line(expected=expected, observed=observed, missing=missing),
        silent_line=None,
        synthetic=(),
        escalates=False,
        verdict=None,
    )


async def fetch_failed_runs(
    session: AsyncSession,
    run_date: dt.date,
    *,
    manifest: RunManifest | None = None,
) -> tuple[list[dict], int, CoverageReport]:
    observed_statement = sa.select(
        dream_runs.c.phase,
        dream_runs.c.status,
        dream_runs.c.project_key,
    ).where(dream_runs.c.run_date == run_date)
    observed_result = await session.execute(observed_statement)
    observed_rows = [dict(row._mapping) for row in observed_result.all()]

    failures_filter = dream_runs.c.status.in_(FAILED_STATUSES)
    failures_statement = (
        sa.select(
            dream_runs.c.id,
            dream_runs.c.phase,
            dream_runs.c.status,
            dream_runs.c.project_key,
            dream_runs.c.error_message,
            dream_runs.c.created_at,
        )
        .where(dream_runs.c.run_date == run_date, failures_filter)
        .order_by(dream_runs.c.created_at.desc(), dream_runs.c.id.desc())
        .limit(MAX_FETCHED_FAILURES)
    )
    failures_result = await session.execute(failures_statement)
    persisted_failures = [dict(row._mapping) for row in failures_result.all()]
    failures_count_statement = (
        sa.select(sa.func.count())
        .select_from(dream_runs)
        .where(dream_runs.c.run_date == run_date, failures_filter)
    )
    failures_count_result = await session.execute(failures_count_statement)
    persisted_failure_count = int(failures_count_result.scalar_one())

    observed_pairs = {
        (str(row["phase"]), str(row.get("project_key") or "")) for row in observed_rows
    }

    if manifest is not None:
        coverage = coverage_from_manifest(observed_pairs, manifest)
        failed = [*coverage.synthetic, *persisted_failures]
        synthetic_count = len(coverage.synthetic)
    else:
        expected_pairs = expected_dream_phase_pairs()
        failed = include_missing_expected_phases(
            observed_rows,
            expected_dream_phases(),
            persisted_failures,
            expected_pairs=expected_pairs,
        )
        synthetic_count = len(failed) - len(persisted_failures)
        coverage = coverage_fallback(
            expected=len(expected_pairs),
            observed=len(observed_pairs),
            missing=synthetic_count,
        )

    return failed, synthetic_count + persisted_failure_count, coverage


async def review_night(
    session: AsyncSession,
    run_date: dt.date,
    *,
    manifest: RunManifest | None = None,
) -> NightReport:
    """Lit la nuit UNE fois et rend son rapport ET son verdict de couverture.

    Read-only, comme toujours : trois `SELECT` bornés, aucun `commit`.
    """
    failed, total_failures, coverage = await fetch_failed_runs(session, run_date, manifest=manifest)
    report = (
        build_alert_insight(run_date, failed, total_failures=total_failures) if failed else None
    )
    return NightReport(report=report, coverage=coverage)


async def write_alert_if_failed(
    session: AsyncSession,
    run_date: dt.date,
    *,
    manifest: RunManifest | None = None,
) -> str | None:
    """Compatibilité : le rapport SEUL, sans son verdict de couverture."""
    return (await review_night(session, run_date, manifest=manifest)).report


def render_stdout(
    report: str | None,
    run_date: dt.date,
    coverage: CoverageReport,
) -> str:
    """Le bloc de couverture sous la première ligne, la ligne machine en DERNIER.

    Toujours, y compris les nuits vertes : c'est l'objet même du ticket. Les
    deux nombres que personne ne rapprochait se retrouvent adjacents dans
    journald, sous le résumé « N/M phases OK » que dream.sh vient d'imprimer.
    """
    body = report.splitlines() if report else [f"no failures for {run_date.isoformat()}"]
    lines = [body[0], "", *coverage.block, *body[1:]]
    if coverage.silent_line:
        lines.append(coverage.silent_line)
    lines.append(coverage.machine_line)
    return "\n".join(lines) + "\n"


def default_manifest_path(run_date: dt.date) -> Path:
    """Le manifeste de la nuit, dérivé du dépôt — pour qu'un rejeu soit honnête sans drapeau."""
    repository_root = Path(__file__).resolve().parents[2]
    return repository_root / "logs" / "dream" / f"{run_date.isoformat()}_manifest.tsv"


async def _run(run_date: dt.date, manifest_path: Path | None = None) -> int:
    settings = Settings()
    engine = create_async_engine(settings.postgres_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    manifest = (
        load_run_manifest(manifest_path, run_date=run_date) if manifest_path is not None else None
    )
    try:
        async with factory() as session:
            night = await review_night(session, run_date, manifest=manifest)
            print(render_stdout(night.report, run_date, night.coverage), end="")
        return 2 if night.coverage.escalates else 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    # `--project-key` a été retiré. Il était DÉCORATIF depuis toujours :
    # déclaré, passé, reçu dans la signature… et jamais lu, `fetch_failed_runs`
    # ne le recevant même pas et ses trois requêtes ne filtrant que sur
    # `run_date`. Il devenait en plus trompeur : avec la rotation du pool, la
    # valeur transmise par dream.sh aurait changé chaque nuit sans que rien ne
    # la lise. Le projet vit maintenant dans le CORPS du rapport, groupé, ce que
    # la 042 rend possible.
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Run date (YYYY-MM-DD)")
    parser.add_argument(
        "--manifest",
        default=None,
        help=(
            "Manifeste écrit par dream.sh. Défaut : logs/dream/<date>_manifest.tsv "
            "dans le dépôt, pour qu'un rejeu à la main soit honnête sans drapeau."
        ),
    )
    args = parser.parse_args(argv)
    run_date = dt.date.fromisoformat(args.date)
    manifest_path = Path(args.manifest) if args.manifest else default_manifest_path(run_date)
    try:
        return asyncio.run(_run(run_date, manifest_path))
    except Exception as exc:  # noqa: BLE001
        print(f"post_run_alert failed: {exc!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
