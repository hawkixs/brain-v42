"""The six `dream_runs` writers lay down a project key, and which one.

This file exists because the four "best-effort" writers had NO witness reading
the SQL actually emitted: some twenty tests replace them with an `AsyncMock()`
without ever asserting their arguments, so a forgotten column would have stayed
green. Each test below runs the REAL function against a recording session and
reads the query produced — never a reconstruction independent of the
implementation.

Two keys, and the distinction is not cosmetic (spec §15.3):

- the GLOBAL phases (`extract`, `roadmap`, `sweep`) and the dead `RESONANCE`
  phase write the sentinel, held by ONE shared constant. It never transits
  through `canonicalize_project_key`, which rejects it (`_KEBAB`), nor through a
  command-line flag, which two bash pins forbid (`test_dream_sh_sweep.py`,
  `test_dream_sh_extract.py`), nor through the writers' signature, which a real
  positional call pins;
- the PER-PROJECT phases (`promote` through the empty pool, and the parser's six
  phases) write the real key, received as an argument.
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
    """An async session that executes nothing and remembers what it is given."""

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
    """The value the `dream_runs` INSERT really lays down in `project_key`.

    Reads the column in the query AND the value in the binds, so that a misaligned
    INSERT — the right value in the wrong column — cannot pass for a success.
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


# --- The four global sentinels ----------------------------------------------


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
    """Dead, unwired — but made consistent so that it is not the only wrong one
    the day someone re-wires it (operator decision of 2026-08-09)."""
    from scripts.dream.cross_project_resonance import _insert_run

    factory, calls = _recording_factory()

    await _insert_run(factory, run_date=dt.date(2026, 8, 9), dry_run=True)

    assert _written_project_key(calls) == GLOBAL_PHASE_PROJECT_KEY


def test_the_four_global_writers_share_one_constant() -> None:
    """Four independent SQL strings, one single truth.

    A sentinel retyped four times drifts apart at the first typo, and the typo
    would be silent: these four writers swallow their failure.
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
    """The fact that forbids "validating the key before writing".

    This test is not decorative: it documents why the sentinel goes through no
    canonicalisation path. The day `_KEBAB` accepts `*`, it goes red and the
    constraint is deliberately re-discussed.
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
    """The sentinel enters through the SQL, never through the signature.

    One more parameter — even with a default — would break a real positional call
    (`test_session_sweep.py`) without waking the ~20 tests that patch these
    functions blindly: the worst possible ratio.
    """
    import importlib
    import inspect

    signature = inspect.signature(getattr(importlib.import_module(module), attribute))

    assert "project_key" not in signature.parameters


# --- The two per-project writers --------------------------------------------


@pytest.mark.asyncio
async def test_empty_pool_row_carries_the_real_project_key() -> None:
    """`promote` is a PER-PROJECT phase: it has no use for the sentinel.

    This is the site the §14.2 inventory forgot while filing its file among the
    readers. It writes the `promote` rows of empty-pool nights, that is, every
    night since 2026-08-08.
    """
    from scripts.dream._promote_helpers import _record_empty_pool

    factory, calls = _recording_factory()

    await _record_empty_pool(factory, dt.date(2026, 8, 9), 3.5, project_key="un-autre-projet")

    assert _written_project_key(calls) == "un-autre-projet"


def test_empty_pool_cli_requires_a_project_key_without_a_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No default: a `default="brain-v42"` is exactly the class of bug targeted.

    The precedent NOT to follow WAS `post_run_alert.py`
    (`default=DEFAULT_PROJECT_KEY`); the one to follow is `promote_prepare.py`
    (`required=True`). The counter-example no longer exists: the pool batch
    removed that parameter, decorative from the start, rather than giving it a
    `required=True` no caller would have read.
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


# --- `model`: the column, not merely the argument ---------------------------


@pytest.mark.asyncio
async def test_ticket_extract_binds_the_model_into_the_insert() -> None:
    """The argument must reach the COLUMN, not die in the signature.

    Measured on the 19→20: 53 `extract` rows out of 53 at `model IS NULL`. Extract
    is the only phase that switches model mid-run, hence the only one where the
    configured model does not allow reconstructing what actually ran.
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
