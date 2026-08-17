"""045 — `dream_runs.model` cesse de refuser les noms de modèles configurés.

Ticket `bcb5e6d8`. La colonne est un `varchar(30)` depuis
`alembic/versions/013_dream_runs.py`. Deux des cinq modèles de phase configurés
n'y entrent pas, et l'un des deux — `nvidia/nemotron-3-super-120b-a12b`, 33 car.
— est le secours WET **déjà configuré** : il perdrait sa ligne le jour de la
bascule WET, c'est-à-dire précisément le jour où l'on voudra mesurer.

Ce qui est perdu n'est pas la colonne `model` : c'est la LIGNE `dream_runs`
entière. `StringDataRightTruncation` remonte dans un `except Exception`
best-effort qui imprime `! warning: could not record dream_run` et continue. Une
nuit qui a réellement tourné n'aurait aucune trace.

Le canary du 2026-08-16 rend ce préalable DUR et non plus prudentiel : les deux
seuls candidats vivants pour remplacer le primaire DRY mort font 34 et 37 car.

PORTÉE DE CE FICHIER. Le test de `tables.py` ci-dessous est DOCUMENTAIRE et il
ne faut pas le vendre autrement : l'écrivain réel est un INSERT en SQL brut, où
la longueur déclarée dans la metadata SQLAlchemy n'a aucun rôle à l'exécution.
Élargir `tables.py` sans appliquer la migration donnerait un vert local sur une
production toujours en `varchar(30)`. Seule la révision mesurée en base fait foi.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[3]
MIGRATION = ROOT / "alembic" / "versions" / "045_dream_run_model_width.py"
DREAM_SH = ROOT / "scripts" / "dream.sh"

# La largeur visée. 120 et non « la plus longue + marge » : un nombre rond
# survit au prochain nom de modèle sans redemander une migration.
TARGET_WIDTH = 120
PREVIOUS_WIDTH = 30


def _load_migration_module() -> object:
    """Charger la révision par chemin : `alembic/versions` n'est pas un package."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("migration_045", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declared_model_width() -> int:
    """Longueur déclarée pour `dream_runs.model` dans la metadata."""
    from brain_v42.db.tables import dream_runs

    length = dream_runs.c.model.type.length
    assert isinstance(length, int)
    return length


def _rail_models() -> list[str]:
    """Modèles de RAIL, lus dans `dream.sh` — jamais retapés.

    Le fil du ticket le demande explicitement : `configured_models()` n'énumère
    que les cinq modèles de phase, aucun modèle de rail (codex, agy, claude).
    Une garde qui ne lirait que l'inventaire de phase resterait aveugle à un
    futur nom de rail long — exactement le mode de panne que la 045 ferme.
    """
    source = DREAM_SH.read_text(encoding="utf-8")
    pattern = re.compile(
        r"^BRAIN_DREAM_(?:CODEX|AGY|CLAUDE)_(?:FAST|DEEP)_MODEL="
        r'"\$\{BRAIN_DREAM_\w+:-(?P<model>[^}"]+)\}"',
        re.MULTILINE,
    )
    models = [match.group("model") for match in pattern.finditer(source)]
    assert models, "aucun modèle de rail lu dans dream.sh — le motif a dérivé"
    return models


def _all_configured_models() -> list[str]:
    from scripts.probe_model_liveness import configured_models

    return [entry.model for entry in configured_models()] + _rail_models()


def test_migration_045_chains_from_044() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert 'revision = "045"' in source
    assert 'down_revision = "044"' in source


def test_migration_045_widens_the_model_column_and_can_narrow_it_back() -> None:
    """Lire les largeurs réelles du module, pas leur orthographe dans le source.

    Une assertion textuelle sur `length=120` casserait sur une constante nommée
    — c'est-à-dire sur une écriture meilleure que celle qu'elle impose.
    """
    module = _load_migration_module()

    assert module._TARGET_WIDTH == TARGET_WIDTH
    assert module._PREVIOUS_WIDTH == PREVIOUS_WIDTH
    assert MIGRATION.read_text(encoding="utf-8").count("alter_column") == 2


def test_migration_045_touches_only_the_model_column() -> None:
    """Une migration de largeur n'ajoute rien, ne supprime rien, ne remplit rien."""
    source = MIGRATION.read_text(encoding="utf-8")

    assert "op.add_column" not in source
    assert "op.drop_column" not in source
    assert "UPDATE" not in source.upper().replace("UPDATED", "")


def test_migration_045_downgrade_refuses_to_truncate_existing_rows() -> None:
    """Rétrécir en silence effacerait la preuve que la colonne existe pour porter.

    Postgres refuse déjà un `varchar(30)` sur une valeur de 34 car. — la
    migration doit le dire au lieu de laisser remonter une erreur de driver nue.
    """
    source = MIGRATION.read_text(encoding="utf-8")

    assert "char_length(model)" in source
    assert "raise" in source


def test_migration_045_drops_and_recreates_the_view_that_blocks_the_alter() -> None:
    """Mesuré en production le 2026-08-16, pas anticipé : l'ALTER est refusé.

        FeatureNotSupportedError: cannot alter type of a column used by a view
        DETAIL: rule _RETURN on view codex_dream_run_v1 depends on column "model"

    Postgres refuse de retyper une colonne qu'une vue projette. La vue doit
    tomber avant, et revenir après — dans les deux sens de la migration.
    """
    source = MIGRATION.read_text(encoding="utf-8")
    module = _load_migration_module()

    # Les deux sens : monter et redescendre butent sur la même vue.
    assert source.count("DROP VIEW IF EXISTS codex_dream_run_v1") == 2
    assert source.count("op.execute(_DREAM_RUN_VIEW_SQL)") == 2

    # Un DROP VIEW emporte ses GRANT : `codex_ro` doit être re-servi des deux
    # côtés, sinon la vue revient sans lecteur et la panne se lit côté client.
    assert module._GRANT_SQL == "GRANT SELECT ON codex_dream_run_v1 TO codex_ro"
    assert source.count("op.execute(_GRANT_SQL)") == 2


def test_migration_045_reuses_the_036_definition_instead_of_retyping_the_view() -> None:
    """Recopier le SELECT ferait de la 045 une seconde source de vérité.

    Le contrat codex est gardé par `test_codex_contract_views_036.py`. Une vue
    recréée à la main dériverait de lui au premier oubli de colonne, et la
    dérive ne serait visible que côté codex, en lecture.
    """
    source = MIGRATION.read_text(encoding="utf-8")

    assert "_CREATE_DREAM_RUN_VIEW" in source
    assert "CREATE OR REPLACE VIEW codex_dream_run_v1" not in source
    assert (
        _load_migration_module()
        ._DREAM_RUN_VIEW_SQL.strip()
        .startswith("CREATE OR REPLACE VIEW codex_dream_run_v1")
    )


def test_declared_width_holds_every_configured_model() -> None:
    """Garde DOCUMENTAIRE : l'inventaire est lu, jamais recopié.

    Une liste retapée ici dériverait de la configuration réelle et rendrait un
    vert sur des modèles que plus personne n'appelle (learning 93dc2ec2).
    """
    declared = _declared_model_width()
    for model in _all_configured_models():
        assert len(model) <= declared, (
            f"{model!r} ({len(model)} car.) ne tient pas dans varchar({declared})"
        )


def test_the_two_sqlite_mirrors_do_not_drift_from_tables_py() -> None:
    """Les miroirs de test se re-déclarent à la main : ils peuvent rester en 30.

    Un miroir resté étroit ferait passer un test sur une contrainte que la
    production n'a plus — le faux vert dans l'autre sens.
    """
    declared = _declared_model_width()
    mirrors = (
        ROOT / "tests" / "integration" / "test_session_start_briefing.py",
        ROOT / "tests" / "unit" / "services" / "test_dream_run_service.py",
    )
    for mirror in mirrors:
        assert f'Column("model", String({declared}))' in mirror.read_text(encoding="utf-8"), (
            f"{mirror.name} déclare une largeur de `model` différente de tables.py"
        )
