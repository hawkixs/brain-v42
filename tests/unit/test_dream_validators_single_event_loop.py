"""Un validateur qui échoue doit MARQUER la ligne, pas mourir en la marquant.

La nuit du 19→20 : `reorg` a échoué, `dream.sh` a imprimé « dream_runs marked
partial », et la ligne `dream_runs` est restée `done`. Les deux faits sont vrais
en même temps parce que le marquage lui-même a crashé.

LA FORME, exactement. `main()` construit l'engine HORS de toute boucle, puis
l'utilise dans DEUX `asyncio.run()` successifs :

    session_factory = _build_factory(...)      # engine créé hors boucle
    try:
        asyncio.run(validate(..., session_factory, ...))     # boucle 1, puis FERMÉE
    except ValidationFailure as exc:
        asyncio.run(_mark_dream_run_partial(session_factory, ...))   # boucle 2

`asyncio.run` ferme sa boucle en sortant. Les connexions que le pool a ouvertes
pendant la boucle 1 restent attachées à cette boucle morte ; `pool_pre_ping=True`
les touche au premier checkout de la boucle 2, et asyncpg lève
`RuntimeError: Event loop is closed`. Le marquage n'a donc jamais lieu — et il
lève APRÈS le `except`, donc l'exception remonte hors de `main()` : le `print`
d'échec ne s'exécute pas non plus.

Ce chemin ne se déclenche QUE sur le chemin d'échec, c'est-à-dire exactement
quand on a besoin de lui. Une nuit verte ne le rencontre jamais — voilà pourquoi
il a survécu.

`connect_validate.py` est le modèle sain : un seul `asyncio.run`.

POURQUOI LES TESTS EXISTANTS NE LE VOYAIENT PAS : `test_main_logs_positive_wet_
validation_evidence` remplace la fabrique par un `MagicMock()`, qui se laisse
utiliser depuis n'importe quelle boucle et n'a pas de pool. Il teste le chemin
VERT avec un objet qui ne peut pas reproduire le défaut. Le harnais ci-dessous
reproduit l'affinité de boucle, qui est le fait de production.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
from typing import Any
from unittest.mock import MagicMock

import pytest

_WET_REORG_TRAILER = (
    "Prose.\n\n"
    "=== REORG REPORT ===\n"
    '{"dry_run": false, "updated": [], "archived": []}\n'
    "=== END ===\n"
)
_WET_PROMOTE_TRAILER = (
    'Prose.\n\n=== PROMOTE REPORT ===\n{"promoted": [], "skipped": []}\n=== END ===\n'
)


class _DeadLoopError(RuntimeError):
    """Ce qu'asyncpg lève quand on retouche une connexion d'une boucle fermée."""


class _Begin:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Session:
    def __init__(self, pool: _LoopAffinePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _Session:
        # Le checkout du pool : c'est ICI que `pool_pre_ping` touche la connexion.
        self._pool.checkout()
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return _Begin()

    async def execute(self, statement: Any) -> MagicMock:
        self._pool.statements.append(statement)
        return MagicMock()

    async def commit(self) -> None:
        return None


class _LoopAffinePool:
    """Une fabrique de sessions qui se lie à la boucle du PREMIER checkout.

    C'est le comportement d'un pool asyncpg sous `pool_pre_ping=True`, réduit à
    ce qui compte ici. Un `MagicMock` ne peut pas le reproduire : il n'a pas de
    pool, donc pas d'affinité.
    """

    def __init__(self) -> None:
        self.bound_loop: asyncio.AbstractEventLoop | None = None
        self.statements: list[Any] = []
        self.checkouts = 0

    def checkout(self) -> None:
        loop = asyncio.get_running_loop()
        self.checkouts += 1
        if self.bound_loop is None:
            self.bound_loop = loop
        elif loop is not self.bound_loop:
            raise _DeadLoopError("Event loop is closed")

    def __call__(self) -> _Session:
        return _Session(self)


def _module(name: str) -> Any:
    from scripts.dream import connect_validate, promote_validate, reorg_validate

    return {
        "reorg": reorg_validate,
        "promote": promote_validate,
        "connect": connect_validate,
    }[name]


def _argv(name: str, tmp_path: pathlib.Path) -> list[str]:
    if name == "reorg":
        log = tmp_path / "reorg.log"
        log.write_text(_WET_REORG_TRAILER)
        tags_before = tmp_path / "tags_before.json"
        tags_before.write_text("{}")
        return [
            "--report-log",
            str(log),
            "--dream-run-id",
            "4242",
            "--project-key",
            "brain-v42",
            "--tags-before-json",
            str(tags_before),
        ]
    log = tmp_path / "promote.log"
    log.write_text(_WET_PROMOTE_TRAILER)
    candidates = tmp_path / "candidates.json"
    candidates.write_text(json.dumps([]))
    return [
        "--report-log",
        str(log),
        "--candidates-json",
        str(candidates),
        "--dream-run-id",
        "4242",
        "--project-key",
        "brain-v42",
    ]


def _install(name: str, monkeypatch: pytest.MonkeyPatch, pool: _LoopAffinePool) -> Any:
    """Câbler le module sur la fabrique affine, avec un `validate` de production.

    « De production » veut dire : il TOUCHE la base — donc il lie la boucle 1 —
    puis il échoue. C'est la séquence de la nuit du 19→20. Un `validate` qui
    échouerait sans toucher la base ne reproduirait rien : le pool resterait
    vierge et la boucle 2 fonctionnerait.
    """
    module = _module(name)
    monkeypatch.setattr(
        module, "Settings", lambda: MagicMock(postgres_url="postgresql+asyncpg://unused")
    )
    monkeypatch.setattr(module, "_build_factory", lambda _url: pool)

    async def _validate_that_touched_the_db(*_args: object, **_kwargs: object) -> None:
        async with pool() as session:
            await session.execute("SELECT 1")
        raise module.ValidationFailure("integrity violation")

    monkeypatch.setattr(module, "validate", _validate_that_touched_the_db)
    return module


@pytest.mark.parametrize("name", ["reorg", "promote"])
def test_a_failing_validator_actually_marks_the_row_partial(
    name: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Le fait de production : le marquage doit ABOUTIR, pas lever."""
    module = _install(name, monkeypatch, pool := _LoopAffinePool())

    rc = module.main(_argv(name, tmp_path))

    assert rc == 1, "un validateur qui échoue rend 1, il ne remonte pas son exception"
    assert pool.checkouts == 2, "validate PUIS le marquage doivent avoir servi le pool"
    assert pool.statements, (
        "aucun UPDATE n'a atteint la base : la ligne dream_runs reste `done` alors "
        "que la nuit a déclaré l'échec — c'est le faux-vert du 19→20"
    )
    compiled = str(pool.statements[-1]).lower()
    assert "update" in compiled and "dream_runs" in compiled
    assert "VALIDATION FAILED" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["reorg", "promote"])
def test_the_pool_is_never_used_from_a_second_event_loop(
    name: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La propriété structurelle, énoncée directement plutôt que déduite.

    Le harnais LÈVE si un second checkout vient d'une autre boucle ; ce test
    échoue donc sur `_DeadLoopError` tant que `main()` ouvre deux boucles.
    """
    module = _install(name, monkeypatch, pool := _LoopAffinePool())

    module.main(_argv(name, tmp_path))

    assert pool.bound_loop is not None
    assert pool.checkouts == 2


@pytest.mark.parametrize("name", ["reorg", "promote", "connect"])
def test_a_validator_opens_exactly_one_event_loop(name: str) -> None:
    """Épinglage textuel : un `asyncio.run` par module, `connect` compris.

    `connect_validate` est le modèle sain et sert de témoin — si ce test devenait
    vert pour une mauvaise raison (le motif ne matche plus), il rougirait ici.
    """
    module = _module(name)
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    runs = re.findall(r"asyncio\.run\(", source)

    assert len(runs) == 1, (
        f"{name}_validate ouvre {len(runs)} boucles. Deux `asyncio.run` sur une "
        f"fabrique partagée laissent les connexions de la première boucle dans le "
        f"pool ; `pool_pre_ping` les touche depuis la seconde et asyncpg lève."
    )
