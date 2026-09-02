"""Phase 0 anchor — the project-key regex has EIGHT guardians, and nothing linked them.

`models/project_key.py` declares itself the "single source of truth" for the key format.
It is not: the same pattern is copied by hand into eight live enforcement surfaces,
spread over three registers that never read each other —

  1. `_KEBAB`, the Python write path (Pydantic, through `ProjectKeyCanonicalMixin`);
  2. the `CheckConstraint` `projects_key_format_valid` declared in `db/tables.py`;
  3. two SQL `CHECK`s in migrations: **012** (`chk_project_key_format` on
     `project_contexts`) and **033** (`projects_key_format_valid` on `projects`);
  4. **five recovery attestation assets** (`ops/recovery/*.sql`), where the pattern
     appears in its NEGATED form (`!~`) to count non-conforming keys.

**No test linked them** — surveyed on 2026-08-20 through three independent angles:
`_KEBAB` appears in `tests/` only in docstring prose, never imported; the tests that
cite the constraints do so by NAME; and
`test_migration_trims_project_references_and_rejects_noncanonical_keys`
(`test_schema_data_foundation_033.py:373`) pins the migration source against a literal
**retyped inside the test**, which proves nothing about `_KEBAB`.

The measurable consequence, and it is the drift `docs/PROJECTS_SYSTEM.md` §8 names
without guarding: widening the Python regex would let through, on the Pydantic side,
keys the database would refuse — **and nothing would turn red before the INSERT**. In
the other direction, touching an `ops/recovery/` asset breaks the restoration proof
without breaking any test.

This test is therefore BIDIRECTIONAL by construction: it writes the pattern nowhere. It
imports `_KEBAB` and **extracts** the other eight from the tree — the metadata
constraint is read off the SQLAlchemy object, the other seven off their file. Mutating a
single side turns it red; mutating all nine consistently stays green, which is the
intended behaviour — the guarded property is AGREEMENT, not a frozen value.

Phase 0 = photograph what exists. This test expresses no preference about the key format
and anticipates no decision of the redesign.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from brain_v42.models.project_key import _KEBAB

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: `project_key ~ '<pattern>'` or, in the attestation assets, `project_key !~ '<pattern>'`.
_SQL_PREDICATE = re.compile(r"project_key\s*!?~\s*'([^']*)'")

#: The two migrations that install a format CHECK, and the name of the constraint set.
_MIGRATIONS = {
    "012_project_groups_and_normalization.py": "chk_project_key_format",
    "033_graph_relation_ledger.py": "projects_key_format_valid",
}

#: The five attestation assets that carry the predicate today. Pinned DELIBERATELY: a v5
#: asset must either reuse the pattern or force an explicit decision here. The sixth
#: `.sql` of the directory (`brain-v42-v1.sql`) predates the constraint and does not
#: carry it.
_RECOVERY_ASSETS_WITH_PREDICATE = frozenset(
    {
        "brain-v42-v2.sql",
        "brain-v42-v3.sql",
        "brain-v42-v3-pgrestore.sql",
        "brain-v42-v4.sql",
        "brain-v42-v4-pgrestore.sql",
        # Added on 2026-08-21 by the v5 mint. The friction worked as written:
        # these two assets were born by deriving v4, so they inherited the
        # predicate — and this test turned red to require it, instead of letting
        # them in with nobody checking that they carried it.
        "brain-v42-v5.sql",
        "brain-v42-v5-pgrestore.sql",
        # Added on 2026-08-29 by the v6 mint (047/048): derived from v5, they
        # inherit the predicate — and this friction turned red a third time,
        # exactly as written.
        "brain-v42-v6.sql",
        "brain-v42-v6-pgrestore.sql",
        # Added on 2026-09-02 by the v7 mint (049): derived from v6 bar seven
        # fingerprints, they inherit the predicate — the fourth reddening of this
        # friction, still exactly as written.
        "brain-v42-v7.sql",
        "brain-v42-v7-pgrestore.sql",
    }
)


def _patterns_in(text: str) -> list[str]:
    return _SQL_PREDICATE.findall(text)


def _recovery_assets() -> dict[str, list[str]]:
    """Return, for each `.sql` asset of `ops/recovery/`, the patterns it carries."""
    return {
        path.name: _patterns_in(path.read_text(encoding="utf-8"))
        for path in sorted((PROJECT_ROOT / "ops" / "recovery").glob("*.sql"))
    }


def test_the_sqlalchemy_check_constraint_uses_the_python_source_of_truth() -> None:
    """The constraint declared in the metadata is read off the object, not the source."""
    from brain_v42.db.tables import projects

    constraint = next(
        c
        for c in projects.constraints
        if isinstance(c, sa.CheckConstraint) and c.name == "projects_key_format_valid"
    )
    patterns = _patterns_in(str(constraint.sqltext))

    assert patterns, "la contrainte ne porte plus de prédicat `project_key ~ '...'` extractible"
    assert patterns == [_KEBAB.pattern], (
        f"`db/tables.py` déclare {patterns} là où `_KEBAB` vaut {_KEBAB.pattern!r}. "
        f"Les métadonnées SQLAlchemy et le chemin d'écriture Pydantic ne s'accordent plus : "
        f"Pydantic accepterait des clés que la base refuse, et rien ne rougirait avant l'INSERT."
    )


@pytest.mark.parametrize(("filename", "constraint_name"), sorted(_MIGRATIONS.items()))
def test_both_format_migrations_check_the_python_source_of_truth(
    filename: str, constraint_name: str
) -> None:
    source = (PROJECT_ROOT / "alembic" / "versions" / filename).read_text(encoding="utf-8")

    assert constraint_name in source, (
        f"{filename} ne pose plus la contrainte {constraint_name} : cet ancrage vise la "
        f"mauvaise migration, ou le CHECK de format a été déplacé sans le dire."
    )
    patterns = _patterns_in(source)
    assert patterns, f"{filename} ne porte plus de prédicat `project_key ~ '...'` extractible"
    assert set(patterns) == {_KEBAB.pattern}, (
        f"{filename} pose {sorted(set(patterns))} là où `_KEBAB` vaut {_KEBAB.pattern!r}. "
        f"Une migration appliquée fait loi en base ; la divergence ne se verrait qu'à "
        f"l'INSERT, en production."
    )


def test_the_set_of_recovery_assets_carrying_the_predicate_is_pinned() -> None:
    """Intended friction: a new attestation asset forces a decision here.

    Without this pin, a v5 asset OMITTING the predicate would go unnoticed — and the
    recovery attestation would silently stop counting non-conforming keys.
    """
    carrying = {name for name, patterns in _recovery_assets().items() if patterns}

    assert carrying == set(_RECOVERY_ASSETS_WITH_PREDICATE), (
        f"les assets `ops/recovery/` portant le prédicat sont {sorted(carrying)}, "
        f"le pin dit {sorted(_RECOVERY_ASSETS_WITH_PREDICATE)}. Un asset ajouté doit "
        f"reprendre le motif (et rejoindre ce pin) ; un asset qui le perd casse la "
        f"preuve de restauration sans casser autre chose."
    )


@pytest.mark.parametrize("filename", sorted(_RECOVERY_ASSETS_WITH_PREDICATE))
def test_every_recovery_attestation_asset_checks_the_python_source_of_truth(
    filename: str,
) -> None:
    patterns = _recovery_assets()[filename]

    assert patterns, f"{filename} ne porte plus de prédicat extractible"
    assert set(patterns) == {_KEBAB.pattern}, (
        f"{filename} compte les clés non conformes avec {sorted(set(patterns))} là où "
        f"`_KEBAB` vaut {_KEBAB.pattern!r}. L'attestation de récupération mesurerait un "
        f"autre format que celui que le code impose : la preuve de restauration mentirait."
    )


def test_the_anchor_covers_every_live_enforcement_surface() -> None:
    """Non-vacuity guard: count the surfaces, so none escapes in silence.

    FOURTEEN guardians besides `_KEBAB` (1 metadata + 2 migrations + 11 assets) since
    the v7 mint of 2026-09-02 — it was eight when this anchor was written, ten at the v5
    mint, twelve at the v6 mint. This count deliberately includes NO document:
    `docs/design/` is not tracked, and a test counting prose would fail depending on the
    working tree.
    """
    from brain_v42.db.tables import projects

    metadata_sites = sum(
        len(_patterns_in(str(c.sqltext)))
        for c in projects.constraints
        if isinstance(c, sa.CheckConstraint)
    )
    migration_sites = sum(
        len(
            _patterns_in((PROJECT_ROOT / "alembic" / "versions" / name).read_text(encoding="utf-8"))
        )
        for name in _MIGRATIONS
    )
    recovery_sites = sum(len(patterns) for patterns in _recovery_assets().values())

    assert (metadata_sites, migration_sites, recovery_sites) == (1, 2, 11), (
        "la ventilation des surfaces d'application a changé "
        f"(métadonnées={metadata_sites}, migrations={migration_sites}, "
        f"attestation={recovery_sites} ; attendu 1/2/11). Recenser avant de corriger le "
        "compte : c'est ce recensement qui a été faux trois fois."
    )
