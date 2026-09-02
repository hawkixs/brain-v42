"""Ancrage Phase 0 — la regex de la clé de projet a HUIT gardiens, et rien ne les reliait.

`models/project_key.py` se déclare « seule source de vérité » du format de clé. Il ne
l'est pas : le même motif est recopié à la main dans huit surfaces d'application
vivantes, réparties sur trois registres qui ne se lisent jamais entre eux —

  1. `_KEBAB`, le chemin d'écriture Python (Pydantic, via `ProjectKeyCanonicalMixin`) ;
  2. le `CheckConstraint` `projects_key_format_valid` déclaré dans `db/tables.py` ;
  3. deux `CHECK` SQL en migration : la **012** (`chk_project_key_format` sur
     `project_contexts`) et la **033** (`projects_key_format_valid` sur `projects`) ;
  4. **cinq assets d'attestation de récupération** (`ops/recovery/*.sql`), où le motif
     apparaît sous sa forme NIÉE (`!~`) pour compter les clés non conformes.

**Aucun test ne les reliait** — recensé le 2026-08-20 par trois motifs indépendants :
`_KEBAB` n'apparaît dans `tests/` que dans de la prose de docstring, jamais importé ;
les tests qui citent les contraintes le font par leur NOM ; et
`test_migration_trims_project_references_and_rejects_noncanonical_keys`
(`test_schema_data_foundation_033.py:373`) épingle la source de la migration contre un
littéral **réécrit dans le test**, ce qui ne prouve rien sur `_KEBAB`.

Conséquence mesurable, et c'est la dérive que `docs/PROJECTS_SYSTEM.md` §8 nomme sans
la garder : élargir la regex Python laisserait passer, côté Pydantic, des clés que la
base refuserait — **et rien ne rougirait avant l'INSERT**. Dans l'autre sens, toucher un
asset `ops/recovery/` casse la preuve de restauration sans casser aucun test.

Ce test est donc BIDIRECTIONNEL par construction : il n'écrit le motif nulle part. Il
importe `_KEBAB` et **extrait** les huit autres de l'arbre — la contrainte des métadonnées
est lue sur l'objet SQLAlchemy, les sept autres sur leur fichier. Muter un seul côté rougit ;
muter les neuf de façon cohérente reste vert, ce qui est le comportement voulu — la
propriété gardée est l'ACCORD, pas une valeur figée.

Phase 0 = photographier l'existant. Ce test n'exprime aucune préférence sur le format de
clé et n'anticipe aucune décision de la refonte.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from brain_v42.models.project_key import _KEBAB

PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: `project_key ~ '<motif>'` ou, dans les assets d'attestation, `project_key !~ '<motif>'`.
_SQL_PREDICATE = re.compile(r"project_key\s*!?~\s*'([^']*)'")

#: Les deux migrations qui posent un CHECK de format, et le nom de la contrainte posée.
_MIGRATIONS = {
    "012_project_groups_and_normalization.py": "chk_project_key_format",
    "033_graph_relation_ledger.py": "projects_key_format_valid",
}

#: Les cinq assets d'attestation qui portent aujourd'hui le prédicat. Pinné VOLONTAIREMENT :
#: un asset v5 doit soit reprendre le motif, soit forcer une décision explicite ici. Le
#: sixième `.sql` du dossier (`brain-v42-v1.sql`) précède la contrainte et ne le porte pas.
_RECOVERY_ASSETS_WITH_PREDICATE = frozenset(
    {
        "brain-v42-v2.sql",
        "brain-v42-v3.sql",
        "brain-v42-v3-pgrestore.sql",
        "brain-v42-v4.sql",
        "brain-v42-v4-pgrestore.sql",
        # Ajoutés le 2026-08-21 par le mint v5. La friction a fonctionné comme
        # écrite : ces deux assets sont nés en dérivant v4, ils ont donc hérité
        # du prédicat — et ce test a rougi pour l'exiger, au lieu de les laisser
        # entrer sans que personne ne vérifie qu'ils le portaient.
        "brain-v42-v5.sql",
        "brain-v42-v5-pgrestore.sql",
        # Ajoutés le 2026-08-29 par le mint v6 (047/048) : dérivés de v5, ils
        # héritent du prédicat — et cette friction a rougi une troisième fois,
        # exactement comme écrite.
        "brain-v42-v6.sql",
        "brain-v42-v6-pgrestore.sql",
        # Ajoutés le 2026-09-02 par le mint v7 (049) : dérivés de v6 à sept
        # empreintes près, ils héritent du prédicat — quatrième rougissement de
        # cette friction, toujours exactement comme écrite.
        "brain-v42-v7.sql",
        "brain-v42-v7-pgrestore.sql",
    }
)


def _patterns_in(text: str) -> list[str]:
    return _SQL_PREDICATE.findall(text)


def _recovery_assets() -> dict[str, list[str]]:
    """Rendre, pour chaque asset `.sql` de `ops/recovery/`, les motifs qu'il porte."""
    return {
        path.name: _patterns_in(path.read_text(encoding="utf-8"))
        for path in sorted((PROJECT_ROOT / "ops" / "recovery").glob("*.sql"))
    }


def test_the_sqlalchemy_check_constraint_uses_the_python_source_of_truth() -> None:
    """La contrainte déclarée dans les métadonnées est lue sur l'objet, pas sur la source."""
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
    """Friction voulue : un nouvel asset d'attestation force une décision ici.

    Sans ce pin, un asset v5 qui OMET le prédicat passerait inaperçu — et l'attestation
    de récupération cesserait silencieusement de compter les clés non conformes.
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
    """Garde de non-vacuité : compter les surfaces, pour qu'aucune n'échappe en silence.

    QUATORZE gardiens hors `_KEBAB` (1 métadonnées + 2 migrations + 11 assets) depuis
    le mint v7 du 2026-09-02 — c'était huit à l'écriture de cet ancrage, dix au mint v5,
    douze au mint v6. Ce compte n'inclut délibérément AUCUN document : `docs/design/`
    n'est pas tracké, et un test qui compterait la prose échouerait selon l'arbre de
    travail.
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
