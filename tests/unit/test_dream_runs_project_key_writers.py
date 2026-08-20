"""Les six écrivains de `dream_runs` posent une clé de projet, et laquelle.

Ce fichier existe parce que les quatre écrivains « best-effort » n'avaient AUCUN
témoin lisant le SQL réellement émis : une vingtaine de tests les remplacent par
un `AsyncMock()` sans jamais asserter leurs arguments, donc une colonne oubliée
serait restée verte. Chaque test ci-dessous exécute la VRAIE fonction contre une
session enregistreuse et lit la requête produite — jamais une reconstruction
indépendante de l'implémentation.

Deux clés, et la distinction n'est pas cosmétique (spec §15.3) :

- les phases GLOBALES (`extract`, `roadmap`, `sweep`) et la phase morte
  `RESONANCE` écrivent la sentinelle, tenue par UNE constante partagée. Elle ne
  transite jamais par `canonicalize_project_key`, qui la rejette (`_KEBAB`), ni
  par un flag de ligne de commande, que deux épingles bash interdisent
  (`test_dream_sh_sweep.py`, `test_dream_sh_extract.py`), ni par la signature
  des writers, qu'un appel positionnel réel épingle ;
- les phases PAR PROJET (`promote` via le pool vide, et les six phases du
  parser) écrivent la vraie clé, reçue en argument.
"""

from __future__ import annotations

import datetime as dt
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from brain_v42.dream_run_project_key import GLOBAL_PHASE_PROJECT_KEY

_INSERT = re.compile(
    r"INSERT\s+INTO\s+dream_runs\s*\((?P<cols>[^)]*)\)\s*VALUES\s*\((?P<vals>[^)]*)\)",
    re.IGNORECASE | re.DOTALL,
)


class _RecordingSession:
    """Session asynchrone qui n'exécute rien et retient ce qu'on lui donne."""

    def __init__(self, calls: list[tuple[Any, Any]]) -> None:
        self._calls = calls
        self.committed = 0

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self._calls.append((statement, params))
        return SimpleNamespace(scalar_one=lambda: 1, scalar_one_or_none=lambda: 1)

    async def commit(self) -> None:
        self.committed += 1

    def begin(self) -> _RecordingSession:
        return self

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False


def _recording_factory() -> tuple[Any, list[tuple[Any, Any]]]:
    calls: list[tuple[Any, Any]] = []
    session = _RecordingSession(calls)
    return (lambda: session), calls


def _written_project_key(calls: list[tuple[Any, Any]]) -> Any:
    """La valeur que l'INSERT `dream_runs` pose réellement en `project_key`.

    Lit la colonne dans la requête ET la valeur dans les binds, pour qu'un
    INSERT désaligné — la bonne valeur dans la mauvaise colonne — ne puisse pas
    passer pour un succès.
    """
    inserts = [call for call in calls if _INSERT.search(str(call[0]))]
    assert len(inserts) == 1, f"attendu 1 INSERT dream_runs, vu {len(inserts)}"
    statement, params = inserts[0]

    match = _INSERT.search(str(statement))
    assert match is not None
    columns = [part.strip() for part in match.group("cols").split(",")]
    values = [part.strip() for part in match.group("vals").split(",")]
    assert len(columns) == len(values), (
        f"{len(columns)} colonnes pour {len(values)} valeurs — "
        "un INSERT désaligné écrit la bonne valeur dans la mauvaise colonne"
    )
    written = dict(zip(columns, values, strict=True))
    assert "project_key" in written, f"colonne absente de l'INSERT : {written}"
    assert written["project_key"] == ":project_key", (
        "la clé doit être un paramètre lié nommé, pas une valeur interpolée : "
        f"{written['project_key']}"
    )

    bound = params if params is not None else statement.compile().params
    return bound["project_key"]


# --- Les quatre sentinelles globales ---------------------------------------


@pytest.mark.asyncio
async def test_ticket_extract_writes_the_global_sentinel() -> None:
    from scripts.ticket_extract import record_dream_run

    factory, calls = _recording_factory()

    await record_dream_run(factory, "done", dry=True, duration_s=1.0, error=None)

    assert _written_project_key(calls) == GLOBAL_PHASE_PROJECT_KEY


@pytest.mark.asyncio
async def test_roadmap_curate_writes_the_global_sentinel() -> None:
    from scripts.roadmap_curate import record_dream_run

    factory, calls = _recording_factory()

    await record_dream_run(factory, "done", dry=True, duration_s=1.0, error=None)

    assert _written_project_key(calls) == GLOBAL_PHASE_PROJECT_KEY


@pytest.mark.asyncio
async def test_session_sweep_writes_the_global_sentinel() -> None:
    from brain_v42.maintenance.session_sweep import record_dream_run

    factory, calls = _recording_factory()

    await record_dream_run(factory, "done", dry=True, duration_s=1.0, error=None)

    assert _written_project_key(calls) == GLOBAL_PHASE_PROJECT_KEY


@pytest.mark.asyncio
async def test_cross_project_resonance_writes_the_global_sentinel() -> None:
    """Mort, non câblé — mais mis en cohérence pour ne pas être le seul faux
    le jour où quelqu'un le rebranche (décision opérateur du 2026-08-09)."""
    from scripts.dream.cross_project_resonance import _insert_run

    factory, calls = _recording_factory()

    await _insert_run(factory, run_date=dt.date(2026, 8, 9), dry_run=True)

    assert _written_project_key(calls) == GLOBAL_PHASE_PROJECT_KEY


def test_the_four_global_writers_share_one_constant() -> None:
    """Quatre chaînes SQL indépendantes, une seule vérité.

    Une sentinelle recopiée quatre fois se désaligne à la première coquille, et
    la coquille serait silencieuse : ces quatre écrivains avalent leur échec.
    """
    import importlib

    modules = [
        "scripts.ticket_extract",
        "scripts.roadmap_curate",
        "brain_v42.maintenance.session_sweep",
        "scripts.dream.cross_project_resonance",
    ]

    for name in modules:
        module = importlib.import_module(name)
        assert getattr(module, "GLOBAL_PHASE_PROJECT_KEY", None) is GLOBAL_PHASE_PROJECT_KEY, (
            f"{name} n'importe pas la constante partagée"
        )


def test_the_sentinel_is_rejected_by_the_central_validator() -> None:
    """Le fait qui interdit de « valider la clé avant écriture ».

    Ce test n'est pas décoratif : il documente pourquoi la sentinelle ne passe
    par aucun chemin de canonicalisation. Le jour où `_KEBAB` accepterait `*`,
    il devient rouge et la contrainte se rediscute exprès.
    """
    from brain_v42.models.project_key import canonicalize_project_key

    with pytest.raises(ValueError, match="kebab-case"):
        canonicalize_project_key(GLOBAL_PHASE_PROJECT_KEY)


@pytest.mark.parametrize(
    ("module", "attribute"),
    [
        ("scripts.ticket_extract", "record_dream_run"),
        ("scripts.roadmap_curate", "record_dream_run"),
        ("brain_v42.maintenance.session_sweep", "record_dream_run"),
    ],
)
def test_the_global_writers_keep_their_signature(module: str, attribute: str) -> None:
    """La sentinelle entre par le SQL, jamais par la signature.

    Un paramètre de plus — même avec défaut — casserait un appel positionnel
    réel (`test_session_sweep.py`) sans réveiller les ~20 tests qui patchent
    ces fonctions en aveugle : le pire ratio possible.
    """
    import importlib
    import inspect

    signature = inspect.signature(getattr(importlib.import_module(module), attribute))

    assert "project_key" not in signature.parameters


# --- Les deux écrivains par projet -----------------------------------------


@pytest.mark.asyncio
async def test_empty_pool_row_carries_the_real_project_key() -> None:
    """`promote` est une phase PAR PROJET : elle n'a rien à faire de la sentinelle.

    C'est ce site que l'inventaire §14.2 avait oublié tout en rangeant son
    fichier parmi les lecteurs. Il écrit les lignes `promote` des nuits à pool
    vide, c'est-à-dire toutes les nuits depuis le 2026-08-08.
    """
    from scripts.dream._promote_helpers import _record_empty_pool

    factory, calls = _recording_factory()

    await _record_empty_pool(factory, dt.date(2026, 8, 9), 3.5, project_key="un-autre-projet")

    assert _written_project_key(calls) == "un-autre-projet"


def test_empty_pool_cli_requires_a_project_key_without_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sans défaut : un `default="brain-v42"` est exactement la classe de bug visée.

    Le précédent à ne PAS suivre ÉTAIT `post_run_alert.py`
    (`default=DEFAULT_PROJECT_KEY`) ; celui à suivre est
    `promote_prepare.py` (`required=True`). Le contre-exemple n'existe plus :
    le lot du pool a retiré ce paramètre, décoratif depuis toujours, plutôt que
    de lui donner un `required=True` qu'aucun appelant n'aurait lu.
    """
    from scripts.dream import _promote_helpers

    monkeypatch.setattr(_promote_helpers, "_build_factory", MagicMock(return_value="factory"))
    monkeypatch.setattr(
        _promote_helpers,
        "Settings",
        MagicMock(return_value=MagicMock(postgres_url="postgresql+asyncpg://unused")),
    )

    with pytest.raises(SystemExit) as excinfo:
        _promote_helpers.main(["record-empty-pool", "--date", "2026-08-09"])

    assert excinfo.value.code == 2


def test_empty_pool_cli_forwards_the_project_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from scripts.dream import _promote_helpers

    recorder = AsyncMock()
    monkeypatch.setattr(_promote_helpers, "_build_factory", MagicMock(return_value="factory"))
    monkeypatch.setattr(_promote_helpers, "_record_empty_pool", recorder)
    monkeypatch.setattr(
        _promote_helpers,
        "Settings",
        MagicMock(return_value=MagicMock(postgres_url="postgresql+asyncpg://unused")),
    )

    return_code = _promote_helpers.main(
        [
            "record-empty-pool",
            "--date",
            "2026-08-09",
            "--duration-seconds",
            "3.5",
            "--project-key",
            "red-shrik",
        ]
    )

    assert return_code == 0
    recorder.assert_awaited_once_with("factory", dt.date(2026, 8, 9), 3.5, project_key="red-shrik")


# --- `model` : la colonne, pas seulement l'argument -------------------------


@pytest.mark.asyncio
async def test_ticket_extract_binds_the_model_into_the_insert() -> None:
    """L'argument doit atteindre la COLONNE, pas mourir dans la signature.

    Mesuré le 19→20 : 53 lignes `extract` sur 53 à `model IS NULL`. Extract est
    la seule phase qui bascule de modèle en cours de run, donc la seule où le
    modèle configuré ne permet pas de reconstituer ce qui a tourné.
    """
    from scripts.ticket_extract import record_dream_run

    factory, calls = _recording_factory()

    await record_dream_run(
        factory,
        "done",
        dry=True,
        duration_s=1.0,
        error=None,
        model="nvidia/nemotron-3-super-120b-a12b",
    )

    inserts = [call for call in calls if _INSERT.search(str(call[0]))]
    assert len(inserts) == 1
    statement, params = inserts[0]
    match = _INSERT.search(str(statement))
    assert match is not None
    columns = [part.strip() for part in match.group("cols").split(",")]
    values = [part.strip() for part in match.group("vals").split(",")]
    written = dict(zip(columns, values, strict=True))

    assert "model" in written, f"colonne absente de l'INSERT : {sorted(written)}"
    assert written["model"] == ":model", (
        f"le modèle doit être un paramètre lié nommé, jamais interpolé : {written['model']}"
    )
    bound = params if params is not None else statement.compile().params
    assert bound["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert len(bound["model"]) > 30, (
        "le secours WET dépasse les 30 caractères d'avant la migration 045 — "
        "c'est ce que cette migration a élargi la colonne pour accueillir"
    )
