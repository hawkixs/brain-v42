"""Migration 049 — la série du sweep, le rail sous-déclaré, deux mots de vocabulaire.

Un regroupement sous le critère (c) de la décision signée 9d22bc6a : trois
objets d'une même famille (ADD COLUMN nullable + élargissement de CHECK) dont
les downgrades échouent INDÉPENDAMMENT — c'est cette indépendance, testée ici,
qui rend la tête multi-objets légitime. M-C (checkpoint) n'y entre pas : son
approbation produit de livraison est encore due (d04dc588). M-D reste isolée
et prend la tête suivante.

Gabarit statique de la 045 : ces tests lisent le fichier de migration — ils
prouvent sa FORME (chaîne, idempotence, refus nommés), pas son exécution ;
l'exécution est prouvée par la suite d'intégration, qui applique la chaîne
entière sur brain_test à chaque run.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[3]
MIGRATION = ROOT / "alembic" / "versions" / "049_dream_run_series_and_freshness_vocabulary.py"
TABLES = ROOT / "src" / "brain_v42" / "db" / "tables.py"
PIN = ROOT / "src" / "brain_v42" / "maintenance" / "plan_index_repair_store.py"

_SIX_TABLES = ("adrs", "decisions", "indexed_plans", "learnings", "runbooks", "snippets")


def _text() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_049_chains_from_048() -> None:
    text = _text()
    assert 'revision = "049"' in text
    assert 'down_revision = "048"' in text


def test_the_pin_moves_in_the_same_commit() -> None:
    """La règle du couloir : migration et pin voyagent ensemble, jamais séparés."""
    assert '_REQUIRED_ALEMBIC_HEAD = "049"' in PIN.read_text(encoding="utf-8")


def test_both_columns_are_added_idempotently_and_nullable() -> None:
    """Rejouable À LA MAIN (gabarit 048) : quelqu'un rejouera ces lignes pendant
    la bascule. Et nullable sans défaut — NULL veut dire « écrit avant la 049 »,
    jamais backfillé : un zéro rétroactif mentirait sur des nuits non comptées."""
    text = _text()
    assert "ADD COLUMN IF NOT EXISTS closed_inactive_count INTEGER" in text
    assert "ADD COLUMN IF NOT EXISTS thinking_tokens INTEGER" in text
    assert "DEFAULT" not in text.upper().replace("SANS DÉFAUT", ""), (
        "aucun défaut : NULL est porteur de sens (pré-049 / non mesuré)"
    )


def test_the_vocabulary_gains_exactly_two_words_on_all_six_tables() -> None:
    text = _text()
    assert '_NEW_SOURCES = ("manual_update", "plan_reindex")' in text
    assert '_SOURCES_BEFORE = ("merge", "judgment", "score", "revive")' in text
    for table in _SIX_TABLES:
        assert f'"{table}"' in text, f"{table} absent des six tables du decay"


def test_tables_py_mirrors_the_extended_vocabulary() -> None:
    """La métadonnée applicative et la base doivent dire le MÊME vocabulaire —
    la 041 a atterri un jour sans que rien ne le signale, plus jamais."""
    tables = TABLES.read_text(encoding="utf-8")
    extended = (
        "freshness_source IS NULL OR freshness_source IN "
        "('merge', 'judgment', 'score', 'revive', 'manual_update', 'plan_reindex')"
    )
    assert tables.count(extended) == 6
    assert '"closed_inactive_count"' in tables
    assert '"thinking_tokens"' in tables


def test_the_downgrade_carries_three_independent_named_refusals() -> None:
    """C'est l'indépendance des refus qui a rendu le regroupement légitime
    (9d22bc6a, critère (c)) : trois destructions, trois opt-ins NOMMÉS, trois
    blocs DO séparés — jamais un drapeau générique qui se recopie sans relire."""
    text = _text()
    for opt_in in (
        "allow_sweep_series_downgrade",
        "allow_thinking_tokens_downgrade",
        "allow_freshness_vocabulary_downgrade",
    ):
        assert opt_in in text, f"opt-in absent : {opt_in}"
    assert len(re.findall(r"DO \$\$", text)) >= 3, "chaque destruction porte son propre bloc DO"
    assert "RAISE EXCEPTION" in text


def test_the_vocabulary_downgrade_nulls_before_restoring_the_old_check() -> None:
    """Re-poser le CHECK de la 043 contre des lignes qui portent les valeurs
    nouvelles échouerait tout court. L'opt-in les remet à NULL — une provenance
    ABSENTE se voit, une provenance effacée en silence se croit (les mots de la
    043) — puis seulement restaure l'ancien vocabulaire."""
    text = _text()
    assert "SET freshness_source = NULL" in text
    restore_at = text.rfind("_SOURCES_BEFORE")
    null_at = text.find("SET freshness_source = NULL")
    assert 0 < null_at < restore_at, "le NULL doit précéder la restauration du CHECK"


def test_the_thinking_column_is_fed_separately_never_summed() -> None:
    """Le point du ticket 76e11c9f : additionner le thinking à output_tokens
    rendrait les rails incomparables dans l'autre sens. Colonne séparée,
    alimentée par le seul rail qui la mesure, NULL partout ailleurs."""
    parser = (ROOT / "src" / "brain_v42" / "metrics" / "agy_dream_parser.py").read_text(
        encoding="utf-8"
    )
    shared = (ROOT / "src" / "brain_v42" / "metrics" / "dream_parser.py").read_text(
        encoding="utf-8"
    )
    assert 'if "thinking_tokens" in result_usage:' in parser, (
        "absent du flux = NULL (pas mesuré), jamais 0 (mesuré nul)"
    )
    assert "thinking_tokens: int | None = None" in shared
    assert "telemetry.thinking_tokens if telemetry else None" in shared
    assert "output_tokens + telemetry.thinking" not in shared
    assert "thinking_tokens + telemetry.output" not in shared
