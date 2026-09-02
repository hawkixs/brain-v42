"""The diagnostic watchdog must FIRE, SURVIVE, be BOUNDED, and NAME its gaps.

A dump that is never triggered is dead code that reads as a protection. A dump
triggered into a channel the process throws away on its way out is just as much
so, and that is the MEASURED defect of the first draft: under
``-o faulthandler_exit_on_timeout=true``, faulthandler exits through ``os._exit``,
pytest never writes the item's report, and ``grep -c "DUMP TÂCHES"`` over the run's
whole output returns **0**. CI arms exactly that configuration.

These tests therefore do not measure a formatting: they measure that the dump runs
while the task is still suspended, that it STILL EXISTS after a brutal process
exit, that its volume is over-estimated, and that an unreadable stack is SAID.
"""

from __future__ import annotations

import asyncio
import io
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration.metrics.task_dump import (
    DUMP_FILE_ENV,
    DUMP_FILE_NAME,
    MAX_LINES,
    MAX_TASKS,
    collect_probes,
    dump_tasks_after,
    durable_sink_paths,
    format_probe_report,
    format_task_dump,
)

pytestmark = pytest.mark.integration

#: What the survival proof's subprocess must spend at worst. faulthandler is armed
#: at 3 s there: beyond this bound it is the launcher that has a problem, not the
#: measured case.
_SURVIVAL_PROOF_BUDGET_SECONDS = 90.0


@pytest.fixture(autouse=True)
def _durable_sink_under_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """THIS module's dumps write into ``tmp_path``, not into the real sinks.

    Several tests here trigger a dump ON PURPOSE. Without this redirection, they
    would append to the runner's shared log and to the GitHub step summary on every
    green run: the channel we have just made durable would become permanent noise,
    and permanent noise ends up filtered out by its reader.
    """
    sink = tmp_path / "dump.log"
    monkeypatch.setenv(DUMP_FILE_ENV, str(sink))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return sink


async def test_the_watchdog_fires_while_the_task_is_still_suspended() -> None:
    """The dump fires DURING the wait, and names the still-suspended frame.

    This is the difference with a dump placed in the ``except TimeoutError``:
    ``asyncio.wait_for`` cancels then WAITS for the cancellation, so over there
    ``Task.get_stack()`` on a finished task returns an empty list.
    """
    stream = io.StringIO()
    never = asyncio.Event()

    async def waits_forever() -> None:
        await never.wait()

    victim = asyncio.create_task(waits_forever(), name="la-victime")
    try:
        with pytest.raises(TimeoutError):
            async with dump_tasks_after(0.05, label="témoin", stream=stream) as state:
                await asyncio.wait_for(asyncio.shield(victim), timeout=0.4)
        assert state.fired is True, "le chien de garde n'a pas tiré : diagnostic mort"
    finally:
        never.set()
        await asyncio.wait_for(victim, timeout=5)

    text = stream.getvalue()
    assert "témoin" in text
    assert "la-victime" in text
    # The suspended frame is named, not only the task.
    assert "waits_forever" in text
    assert "test_task_dump.py:" in text
    # And the CHAIN, not only the outer frame: `Task.get_stack()` on a suspended
    # coroutine returns only ONE frame. Without the `cr_await` walk, the dump would
    # say "the task is in waits_forever" — which we already knew — and would leave
    # unsaid the link that is actually waiting.
    assert "asyncio/locks.py:" in text, f"chaîne d'attente absente du dump:\n{text}"


async def test_the_watchdog_does_not_fire_or_survive_on_the_happy_path() -> None:
    """Happy path: no dump, and NO task left alive.

    A watchdog is one more task on the same loop. Not cancelled, it recreates the
    immortal task ``asyncio.Runner.close()`` then waits for without a bound — the
    hang would be moved, not closed.
    """
    stream = io.StringIO()
    before = {t.get_name() for t in asyncio.all_tasks()}

    async with dump_tasks_after(30.0, label="jamais", stream=stream) as state:
        await asyncio.sleep(0)

    assert state.fired is False
    assert stream.getvalue() == ""
    leftover = {t.get_name() for t in asyncio.all_tasks() if not t.done()} - before
    assert leftover == set(), f"le chien de garde a survécu : {leftover}"


async def test_the_watchdog_never_fails_the_test_it_observes() -> None:
    """A broken probe must return ``# indisponible``, never mask the failure."""
    stream = io.StringIO()

    class Explodes:
        def __getattr__(self, name: str) -> object:
            raise AttributeError(f"attribut privé disparu: {name}")

    async with dump_tasks_after(0.05, label="sonde-cassée", stream=stream, mcp=Explodes()):
        await asyncio.sleep(0.2)

    text = stream.getvalue()
    assert "indisponible" in text
    assert "attribut privé disparu" in text


def test_the_dump_is_capped_and_says_how_much_it_omitted() -> None:
    """A drowned log is as unusable as an absent one: the budget is over-estimated."""

    class FakeTask:
        def __init__(self, index: int) -> None:
            self._index = index

        def get_name(self) -> str:
            return f"Task-{self._index:04d}"

        def done(self) -> bool:
            return False

        def cancelling(self) -> int:
            return 0

        def get_coro(self) -> object:
            return None

        def get_stack(self, limit: int | None = None) -> list[object]:
            raise RuntimeError("pile illisible pour ce faux")

    tasks = [FakeTask(i) for i in range(MAX_TASKS * 4)]
    text = format_task_dump(
        label="borné",
        deadline=25.0,
        elapsed=25.1,
        tasks=tasks,
        excluded=0,
        probes={},
    )
    lines = text.splitlines()
    assert len(lines) <= MAX_LINES, f"{len(lines)} lignes — le dump noie le log"
    assert f"{len(tasks) - MAX_TASKS} tâche(s) omise(s)" in text
    # A NAMED gap, not one silently omitted.
    assert "pile illisible" in text


def test_a_task_without_a_recoverable_stack_is_named_not_dropped() -> None:
    """A task with no recoverable stack MUST appear, with its gap declared."""

    class Finished:
        def get_name(self) -> str:
            return "déjà-terminée"

        def done(self) -> bool:
            return True

        def cancelling(self) -> int:
            return 0

        def get_coro(self) -> object:
            return None

        def get_stack(self, limit: int | None = None) -> list[object]:
            return []

    text = format_task_dump(
        label="trou",
        deadline=1.0,
        elapsed=1.0,
        tasks=[Finished()],
        excluded=2,
        probes={"uvicorn.server.started": "indisponible: pas de serveur"},
    )
    assert "déjà-terminée" in text
    assert "aucune frame récupérable" in text
    assert "uvicorn.server.started = indisponible: pas de serveur" in text


async def test_a_chain_that_ends_is_not_reported_as_truncated() -> None:
    """The word "tronquée" must mean truncated.

    The first draft put the note in the ``for``'s ``else``: a chain that ended
    exactly at the last link was announced as cut. A bound that lies about itself
    reads back as a missing link that does not exist.
    """
    stream = io.StringIO()
    async with dump_tasks_after(0.02, label="chaîne-courte", stream=stream):
        await asyncio.sleep(0.15)

    text = stream.getvalue()
    assert "tronquée" not in text.split("-- tâches")[0]
    # The current task is sleeping: its chain fits in three links and terminates.
    assert "asyncio/tasks.py" in text


def test_the_dump_survives_a_faulthandler_hard_exit(tmp_path: Path) -> None:
    """THE proof: the dump still exists AFTER ``os._exit``.

    Replayed from CI's path, not from an equivalent: a real pytest, with
    ``faulthandler_timeout`` + ``exit_on_timeout``, on an armed case that never
    returns. On the stderr-only draft, that same run returned
    ``grep -c "DUMP TÂCHES"`` = **0** over its whole output — the diagnosis was mute
    precisely where it serves. We do NOT assert the absence on the console side:
    that would pin pytest's capture behaviour, which is not this module's contract.
    What is pinned is the presence on the file side.
    """
    repo_root = Path(__file__).resolve().parents[3]
    sink = tmp_path / "durable" / "dump.log"
    case = tmp_path / "test_never_returns.py"
    case.write_text(
        "import asyncio\n"
        "from tests.integration.metrics.task_dump import dump_tasks_after\n"
        "\n"
        "\n"
        "async def _body() -> None:\n"
        "    never = asyncio.Event()\n"
        "    async with dump_tasks_after(0.2, label='preuve-os-exit'):\n"
        "        await never.wait()\n"
        "\n"
        "\n"
        "def test_never_returns() -> None:\n"
        "    asyncio.run(_body())\n",
        encoding="utf-8",
    )

    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(repo_root),
        "PYTHONIOENCODING": "utf-8",
        DUMP_FILE_ENV: str(sink),
    }
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            str(case),
            "-o",
            "faulthandler_timeout=3",
            "-o",
            "faulthandler_exit_on_timeout=true",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=_SURVIVAL_PROOF_BUDGET_SECONDS,
        check=False,
    )

    console = completed.stdout + completed.stderr
    # The process did exit THROUGH the net, otherwise the proof bears on a path
    # other than CI's.
    assert "Timeout (0:00:03)!" in console, f"faulthandler n'a pas tiré:\n{console[-2000:]}"
    assert completed.returncode != 0

    assert sink.exists(), (
        "aucune copie durable : le dump n'existe que dans un tampon que "
        f"`os._exit` a jeté\n{console[-2000:]}"
    )
    durable = sink.read_text(encoding="utf-8")
    assert "DUMP TÂCHES ASYNCIO — preuve-os-exit" in durable
    # And it carries the useful link, not only the title: the `cr_await` walk is the
    # heart of the deliverable, survival is worth nothing without it.
    assert "asyncio/locks.py:" in durable, f"chaîne d'attente absente:\n{durable}"
    assert "item:" in durable and "horodatage:" in durable


def test_the_default_sink_lands_under_runner_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default sink is the runner's scratch, and the explicit one replaces it."""
    monkeypatch.delenv(DUMP_FILE_ENV, raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runner"))
    assert durable_sink_paths() == [tmp_path / "runner" / DUMP_FILE_NAME]

    monkeypatch.setenv(DUMP_FILE_ENV, str(tmp_path / "explicite.log"))
    assert durable_sink_paths() == [tmp_path / "explicite.log"]

    # The step summary is ADDED: it carries visibility, not durability, and it only
    # exists under GitHub Actions.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    assert durable_sink_paths() == [tmp_path / "explicite.log", tmp_path / "summary.md"]


async def test_an_unreachable_sink_is_named_and_never_fails_the_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreachable sink is SAID in the dump, never left unsaid, never raised.

    The durable channel is code that runs during an outage. If it raised, it would
    replace the diagnosis with its own.
    """
    blocker = tmp_path / "pas-un-dossier"
    blocker.write_text("je suis un fichier", encoding="utf-8")
    monkeypatch.setenv(DUMP_FILE_ENV, str(blocker / "dump.log"))

    stream = io.StringIO()
    async with dump_tasks_after(0.05, label="puits-mort", stream=stream) as state:
        await asyncio.sleep(0.2)

    assert state.fired is True
    text = stream.getvalue()
    assert "DUMP TÂCHES" in text
    assert f"copie durable indisponible ({blocker / 'dump.log'})" in text


class _FakeMcp:
    """A credible FastMCP singleton: every MCP probe succeeds on it.

    The opposite of the ``Explodes`` above. Here we do not measure a broken probe's
    robustness, we measure WHICH probes are read — a fake that raised would return
    "indisponible" and would mask a probe we believe removed.
    """

    _lifespan_ref_count = 0
    _lifespan_result_set = False
    _lifespan_result = None
    _lifespan_ref_count_should_not_be_read = True

    class _Started:
        @staticmethod
        def is_set() -> bool:
            return False

    _started = _Started()


def test_the_refuted_lifespan_probes_are_no_longer_collected() -> None:
    """The THREE lifespan probes are REFUTED BY THE CODE, hence removed.

    ``mcp = FastMCP("brain", mask_error_details=True)`` (server.py) is built WITHOUT
    a lifespan: ``_lifespan_proxy`` (fastmcp/server/server.py:264-266) returns ``{}``
    and EXITS before reading ``_lifespan_ref_count``, ``_lifespan_result_set`` or
    ``_lifespan_result``. None of the three can influence a request, so none can
    explain a failure. It is noise that reads as information in a dump one only
    re-reads during an outage.
    """
    probes = collect_probes(mcp=_FakeMcp())

    assert "mcp._lifespan_ref_count" not in probes
    assert "mcp._lifespan_result_set" not in probes
    # `_lifespan_result` falls with the other two, and for the same reason: the
    # proxy exits before reading it. It does vary (`None`/`{}`), but that is exactly
    # what `_started.is_set()` says — a causally inert duplicate is not a
    # measurement. `_FakeMcp` DOES carry it, so this test would fail if it came back.
    assert "mcp._lifespan_result is None" not in probes
    assert "mcp._started.is_set()" in probes


def test_the_sse_exit_latch_is_probed_and_named_apart_from_uvicorns() -> None:
    """TWO distinct ``should_exit``, and the output must not confuse them.

    ``sse_starlette.sse.AppStatus.should_exit`` is a CLASS attribute, hence
    PROCESS-GLOBAL and never reset to ``False``; it arms
    ``_listen_for_exit_signal``'s only immediate and silent exit.
    ``server.should_exit`` is THIS bench's uvicorn instance. Reading them as the
    same variable would cancel the measurement, so their keys must be readable
    separately.
    """

    class _FakeUvicorn:
        started = True
        should_exit = False

        class _State:
            tasks: set[object] = set()

        server_state = _State()

    probes = collect_probes(mcp=_FakeMcp(), server=_FakeUvicorn())

    latch = [name for name in probes if "AppStatus.should_exit" in name]
    instance = [
        name for name in probes if "should_exit" in name and "AppStatus.should_exit" not in name
    ]
    assert len(latch) == 1, f"sonde du latch SSE absente ou dupliquée: {sorted(probes)}"
    assert len(instance) == 1, f"sonde uvicorn absente ou dupliquée: {sorted(probes)}"
    assert latch != instance

    # Once rendered, the two lines must stay distinguishable by eye.
    text = format_probe_report(label="deux-latches", probes=probes)
    assert "GLOBAL processus" in text
    assert "INSTANCE locale du banc" in text
    # And the value actually read, not a placeholder.
    assert probes[latch[0]].endswith(("True", "False")), probes[latch[0]]


def test_the_sse_latch_probe_is_named_unavailable_when_the_module_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``sse_starlette`` may not be importable: the probe SAYS so, it does not raise.

    The import is late and defensive. An ``ImportError`` must return an
    "indisponible" line, never fail the dump — a diagnosis that dies of its annex
    replaces the observed failure with its own.
    """
    monkeypatch.setitem(sys.modules, "sse_starlette.sse", None)

    probes = collect_probes()

    (latch,) = [name for name in probes if "AppStatus.should_exit" in name]
    assert "indisponible" in probes[latch]
    # The FAMILY, not the exact class: a missing module raises
    # `ModuleNotFoundError`, a subclass of `ImportError`. Pinning the literal name
    # "ImportError" would fail this test on the most mundane case of its own
    # scenario.
    assert "Error" in probes[latch] and "sse_starlette" in probes[latch]


def test_the_sse_latch_probe_is_named_unavailable_when_the_attribute_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AppStatus.should_exit`` is a PRIVATE upstream attribute: it can disappear.

    The same contract as for the missing import, a different exception: an
    ``AttributeError`` must return an "indisponible" line, never kill the dump.
    """
    from sse_starlette.sse import AppStatus

    monkeypatch.delattr(AppStatus, "should_exit", raising=True)

    probes = collect_probes()

    (latch,) = [name for name in probes if "AppStatus.should_exit" in name]
    assert "indisponible" in probes[latch]
    assert "AttributeError" in probes[latch]


def test_the_entry_witness_reads_the_latch_before_the_bench_touches_anything() -> None:
    """The entry reading is the measurement that settles it: it must come FIRST.

    ``True`` on entering the metrics bench = an earlier module left the latch armed
    (``tests/integration/mcp/**`` is collected before: "mcp" < "metrics"), and the
    cause is established. ``False`` kills the hypothesis cleanly. The reading is only
    worth anything if it is taken BEFORE this bench builds anything at all: moved
    below ``build_services()``, it would measure the state the bench has just
    produced itself and could no longer attribute the latch to anyone.

    Pinned on the TEXT because the order is the only thing that matters here and no
    execution can prove it without standing up the whole bench.
    """
    source = (Path(__file__).parent / "test_agent_attribution.py").read_text(encoding="utf-8")

    witness = source.index("collect_probes(mcp=mcp)")
    build = source.index("services = build_services()")
    assert witness < build, "le témoin d'entrée est passé APRÈS le montage du banc"
    # And it stays a READING: no assertion on the latch, as long as we do not know
    # what the value is worth in practice. Asserting here would convert a coin-flip
    # into a plain red without anyone knowing why.
    between = source[witness:build]
    asserting = [line for line in between.splitlines() if line.strip().startswith("assert ")]
    assert asserting == [], f"le témoin d'entrée assert au lieu de relever: {asserting}"
