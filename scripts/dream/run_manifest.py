"""Le manifeste d'une nuit Dream : ce qu'elle DÉCLARE, relu au matin.

Ticket `0a9c067e`, recadré par son fil. Le comparateur de fin de nuit existe
déjà (`post_run_alert.include_missing_expected_phases`, appelé par `dream.sh`)
et il a tiré trois nuits de suite. Il est SOUS-DIMENSIONNÉ, pas absent :
`collector_dream.LOOP_PHASES` ne porte que `promote` et `reorg`, si bien que la
nuit du 2026-08-16 a annoncé 20 phases manquantes quand il en manquait 60.

Élargir l'attendu depuis le drop-in ne marcherait pas — `_KS_KEYS` n'a aucune
clé pour `scan`, `clean`, `connect` et `synth`, donc le filtre les exclut quoi
qu'on mette dans `LOOP_PHASES`. Et l'élargir « naïvement » fabriquerait des faux
positifs : une phase sautée par le pré-flight ou par un killswitch n'écrit
aucune ligne et n'en doit aucune.

D'où ce transport : `dream.sh` écrit un TSV à quatre colonnes, AU SITE DE CHAQUE
DÉCISION, et ce module le relit. Écrire au site plutôt qu'en fin de nuit n'est
pas un détail : une nuit tuée par `TimeoutStartSec`, un OOM ou un `set -e` non
gardé n'atteindrait jamais un vidage final, et le rejeu du matin retomberait
sur l'attendu du drop-in — c'est-à-dire sur le trou que ce module ferme.

Le format, quatre champs séparés par TAB, colonnes de queue vides admises :

    meta        run_date        2026-08-18
    meta        planned_phases  63
    expected    scan            red
    skipped     sweep           *               killswitch
    skipped     promote         red-lab         empty-pool-unrecorded
    failed      connect         brain-v42
    timeout     clean           red
    meta        finished        2026-08-18T07:09:32+02:00

QUATRE classes d'absence, pas trois. La quatrième existe parce que
`scripts/dream.sh` pousse `SKIPPED_PHASES+=("$PROJECT_KEY/promote")` HORS du
`if (( record_rc == 0 ))` : « sautée » et « sa ligne est écrite » sont deux
faits INDÉPENDANTS. Soustraire tous les skips rendrait vert un chemin où une
ligne `dream_runs` est réellement perdue — mesuré en production, 1 à 6 lignes
`empty candidate pool` par nuit du 2026-08-08 au 08-13.

Ce module ne fait AUCUNE entrée-sortie de base et n'importe rien du paquet
`brain_v42` — surtout pas `canonicalize_project_key`, qui rejette la sentinelle
`*` des phases globales et lèverait sur les trois d'entre elles chaque nuit.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

Pair = tuple[str, str]
"""Une paire ``(phase, project_key)`` — même ordre que `expected_pairs`."""

# Les DEUX seules raisons qui promettent qu'aucune ligne `dream_runs` n'est due.
# Table CLOSE, et fail-closed : une raison inconnue n'est jamais soustraite, donc
# un huitième site de skip ajouté demain avec un vocabulaire neuf rend le
# détecteur bruyant, jamais aveugle. C'est le seul sens de marche acceptable pour
# un détecteur dont le ticket dit qu'il a rétréci en silence.
NO_ROW_SKIP_REASONS = frozenset({"preflight", "killswitch"})

# Skip dont dream.sh a DIT que l'écriture de la ligne avait échoué (`record_rc`
# non nul). `_promote_helpers.main` rend 1 sur exception et 0 seulement après
# `commit()`, donc ce code est un proxy fiable de « la ligne a été écrite ».
WRITE_FAILED_SKIP_REASON = "empty-pool-unrecorded"

# Skip dont dream.sh a dit que l'écriture avait RÉUSSI. La paire reste attendue :
# si la ligne manque quand même, c'est la classe DSN du 2026-08-15, pas un skip.
WRITE_RECORDED_SKIP_REASON = "empty-pool-recorded"

_KINDS = frozenset({"meta", "expected", "skipped", "failed", "timeout"})

# Plafond d'énumération des paires fautives. `error_message` est du `text` non
# borné, mais un rapport illisible n'est pas lu — c'est le défaut d'origine.
MAX_LISTED_PAIRS = 10


@dataclass(frozen=True)
class RunManifest:
    """Ce que la nuit a déclaré, tel qu'elle l'a déclaré."""

    expected: frozenset[Pair]
    skipped: Mapping[Pair, str]
    failed: frozenset[Pair]
    timed_out: frozenset[Pair]
    meta: Mapping[str, str]
    warnings: tuple[str, ...]

    @property
    def complete(self) -> bool:
        """La nuit a-t-elle atteint son bloc de clôture ?

        Son absence EST le marqueur d'interruption, et c'est voulu : c'est la
        seule partie non incrémentale du manifeste.
        """
        return "finished" in self.meta


@dataclass(frozen=True)
class CoverageVerdict:
    """La partition close de l'attendu, plus les deux drapeaux de structure."""

    expected: frozenset[Pair]
    written: frozenset[Pair]
    skipped: frozenset[Pair]
    writefail: frozenset[Pair]
    declared: frozenset[Pair]
    silent: frozenset[Pair]
    extra: frozenset[Pair]
    consistent: bool
    complete: bool
    planned: int | None

    @property
    def mode(self) -> str:
        return "manifest" if self.complete else "manifest-partial"

    @property
    def escalates(self) -> bool:
        """rc 2 : un trou, une écriture déclarée en échec, ou une structure douteuse.

        Le seuil est 0, mais sur `silent + writefail`, jamais sur `silent` seul.
        Le corps du ticket demandait d'arbitrer un nombre (« un écart de 1 est
        normal ») ; avec quatre classes le nombre disparaît, parce que toute
        absence légitime est désormais DÉCLARÉE ou déjà rapportée par dream.sh.
        """
        return bool(self.silent or self.writefail) or not self.consistent or not self.complete


def _warn(warnings: list[str], raw: str) -> None:
    warnings.append(f"malformed manifest line: {raw!r}")


def parse_run_manifest(text: str) -> RunManifest:
    """Lit le TSV. Ne lève jamais : un manifeste douteux se RAPPORTE."""
    expected: set[Pair] = set()
    skipped: dict[Pair, str] = {}
    failed: set[Pair] = set()
    timed_out: set[Pair] = set()
    meta: dict[str, str] = {}
    warnings: list[str] = []

    for raw in text.splitlines():
        if not raw.strip():
            continue
        fields = [field.strip() for field in raw.split("\t")]
        kind = fields[0]
        rest = fields[1:]
        if kind not in _KINDS:
            # Compatibilité AVANT : un `kind` neuf écrit par une version plus
            # récente de dream.sh est ignoré, pas signalé. Une ligne qui n'a même
            # pas de second champ, elle, est cassée.
            if len(rest) < 1:
                _warn(warnings, raw)
            continue

        if kind == "meta":
            if len(rest) < 2 or not rest[0]:
                _warn(warnings, raw)
                continue
            meta[rest[0]] = rest[1]
            continue

        if len(rest) < 2 or not rest[0] or not rest[1]:
            _warn(warnings, raw)
            continue
        pair: Pair = (rest[0], rest[1])
        if kind == "expected":
            expected.add(pair)
        elif kind == "skipped":
            skipped[pair] = rest[2] if len(rest) > 2 else ""
        elif kind == "failed":
            failed.add(pair)
        else:
            timed_out.add(pair)

    return RunManifest(
        expected=frozenset(expected),
        skipped=dict(skipped),
        failed=frozenset(failed),
        timed_out=frozenset(timed_out),
        meta=dict(meta),
        warnings=tuple(warnings),
    )


def load_run_manifest(path: Path, *, run_date: dt.date) -> RunManifest | None:
    """Rend `None` — donc le REPLI — sur les quatre portes de sortie honnêtes.

    Absent, illisible, sans aucun `expected`, ou daté d'une autre nuit. Un
    attendu vide désarmerait tout en silence ; un manifeste d'une autre nuit
    rendrait un rejeu malhonnête. Repli explicite, jamais accord muet.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    manifest = parse_run_manifest(text)
    if not manifest.expected:
        return None
    if manifest.meta.get("run_date") != run_date.isoformat():
        return None
    return manifest


def _counter(meta: Mapping[str, str], key: str) -> tuple[int | None, bool]:
    """Rend (valeur, lisible). Un compteur illisible est un FAIT, pas un détail."""
    raw = meta.get(key)
    if raw is None:
        return None, True
    try:
        return int(raw), True
    except ValueError:
        return None, False


def classify_coverage(
    observed_pairs: Iterable[Pair],
    manifest: RunManifest,
) -> CoverageVerdict:
    """Partitionne l'attendu déclaré en cinq classes disjointes.

        written   = expected ∩ observed
        missing   = expected − observed
        skipped   = { p ∈ missing : raison(p) ∈ NO_ROW_SKIP_REASONS }
        writefail = { p ∈ missing : raison(p) = empty-pool-unrecorded }
        declared  = (missing − skipped − writefail) ∩ (failed ∪ timeout)
        silent    = missing − skipped − writefail − declared

    La somme des cinq égale `expected` par CONSTRUCTION : `written` est une
    intersection et les quatre autres partitionnent `missing`. Une paire à la
    fois sautée et observée tombe dans `written` et dans rien d'autre — le
    double comptage disparaît sans arbitrage.
    """
    expected = manifest.expected
    observed = frozenset(observed_pairs)

    written = expected & observed
    missing = expected - observed
    skipped = frozenset(
        pair for pair in missing if manifest.skipped.get(pair, "") in NO_ROW_SKIP_REASONS
    )
    writefail = frozenset(
        pair for pair in missing if manifest.skipped.get(pair, "") == WRITE_FAILED_SKIP_REASON
    )
    unexplained = missing - skipped - writefail
    declared = unexplained & (manifest.failed | manifest.timed_out)
    silent = unexplained - declared
    extra = observed - expected

    assert len(written) + len(skipped) + len(writefail) + len(declared) + len(silent) == len(
        expected
    ), "les cinq classes doivent partitionner l'attendu"

    planned, planned_readable = _counter(manifest.meta, "planned_phases")
    total, total_readable = _counter(manifest.meta, "total_phases")
    complete = manifest.complete

    # Trois nombres, trois instants, trois chemins de code. `planned_phases` est
    # calculé EN TÊTE de nuit, `len(expected)` est ce qu'elle a réellement
    # atteint, `total_phases` est son propre compteur en fin de nuit. Comparer
    # `expected` à un `expected` recalculé depuis les mêmes tableaux ne mesurerait
    # rien : `TOTAL_PHASES = |PHASES| × |POOL| + 3` par construction.
    consistent = planned_readable and total_readable
    if total is not None and total != len(expected):
        consistent = False
    if complete and planned is not None and planned != len(expected):
        consistent = False

    return CoverageVerdict(
        expected=expected,
        written=written,
        skipped=skipped,
        writefail=writefail,
        declared=declared,
        silent=silent,
        extra=extra,
        consistent=consistent,
        complete=complete,
        planned=planned,
    )


def format_machine_line(verdict: CoverageVerdict) -> str:
    """La ligne que `dream.sh` ré-émet par `log()`, donc dans journald.

    Premier champ TOUJOURS `mode=` : c'est lui qui interdit de lire une forme
    pour l'autre. Le mode `manifest` s'additionne exactement à `expected`.
    """
    tail = (
        f"written={len(verdict.written)} skipped={len(verdict.skipped)} "
        f"declared={len(verdict.declared)} writefail={len(verdict.writefail)} "
        f"silent={len(verdict.silent)} extra={len(verdict.extra)}"
    )
    if verdict.complete:
        return f"COVERAGE mode=manifest expected={len(verdict.expected)} {tail}"
    planned = "unknown" if verdict.planned is None else str(verdict.planned)
    return (
        f"COVERAGE mode=manifest-partial planned={planned} reached={len(verdict.expected)} {tail}"
    )


def format_fallback_line(*, expected: int, observed: int, missing: int) -> str:
    """Le repli, avec des NOMS DE CHAMPS différents — parce que les ensembles le sont.

    `observed` n'est pas inclus dans `expected` (23 paires attendues depuis le
    drop-in contre 62 écrites le 2026-08-18) et `silent` n'est pas calculable.
    Écrire `expected=23 written=62` reproduirait le défaut même que ce ticket
    dénonce : deux nombres côte à côte que rien ne réconcilie.
    """
    return (
        f"COVERAGE mode=fallback expected={expected} observed={observed} "
        f"missing={missing} silent=unknown"
    )


def _render_pairs(pairs: Iterable[Pair]) -> str:
    ordered = sorted(pairs, key=lambda pair: (pair[1], pair[0]))
    shown = ordered[:MAX_LISTED_PAIRS]
    rendered = ", ".join(f"{project}/{phase}" for phase, project in shown)
    hidden = len(ordered) - len(shown)
    return f"{rendered} and {hidden} more" if hidden else rendered


def format_silent_line(verdict: CoverageVerdict) -> str | None:
    """Nomme les paires fautives — les deux classes qui escaladent, distinguées.

    « dream.sh a dit que l'écriture avait échoué » n'appelle pas le même premier
    geste que « personne ne sait pourquoi la ligne manque ».
    """
    parts = []
    if verdict.silent:
        parts.append(f"silent={_render_pairs(verdict.silent)}")
    if verdict.writefail:
        parts.append(f"writefail={_render_pairs(verdict.writefail)}")
    if not parts:
        return None
    return "COVERAGE_SILENT " + " | ".join(parts)
