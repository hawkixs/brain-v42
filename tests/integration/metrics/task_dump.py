"""Name WHO is waiting, when the metrics bench hangs.

This module only exists because the one net armed in this repository cannot answer
the question. Two measurements, not an argument:

1. **The net is unreachable — but the arithmetic that said so was WRONG.**
   ``pyproject.toml`` arms ``faulthandler_timeout = 120`` / ``exit_on_timeout``,
   and the plugin arms it on an item's PROTOCOL (setup + call + teardown), not on
   its body. The first draft announced "60 s of body + 40 s of teardown = 100 s,
   under 120": it forgot the server's startup, and above all that the body carries
   TWO CONSECUTIVE bounded waits. 5 + 60 + 60 + 40 crosses 120 with nothing
   pathological, and the runner was measured 22 % slower on 2026-08-25 (148 s
   against 120.9 s on the same suite). ``test_agent_attribution.py``'s budgets
   were therefore brought back down: 5 + 25 + 25 + 21 = 76 s at worst, i.e. 93 s
   at +22 %, against a net at 120. The application-level bound wins the race back,
   instead of claiming to. The only run that ever reached 120 s (32779161805) is
   the one that had no bound inside the test body.
2. **And even if it fired, it would see nothing useful.**
   ``faulthandler.dump_traceback_later`` dumps THREADS. The guilty coroutine is on
   no thread stack: it is SUSPENDED in ``client.py:571 ready_event.wait()``, its
   frames live in the ``Task`` object, and the MainThread only shows
   ``run_forever → _run_once → epoll.poll``. The only reader of those frames is
   ``Task.get_stack()``, which faulthandler never calls.

Hence the measured result: we know THAT it hangs, never WHO.

Three design rules, each paid for by an incident in the neighbouring file:

- **The watchdog fires DURING the wait.** Placed in an ``except TimeoutError``, it
  would be dead code on its own path: ``asyncio.wait_for`` cancels the gather,
  WAITS for the cancellation, THEN raises — the tasks are finished by then and
  ``Task.get_stack()`` returns an empty list.
- **It never cancels anything** and it never fails the test it observes. A green
  run with a dump stays green: the dump becomes the measurement of the overrun.
- **It writes to a FILE, not only to stderr.** That is the defect that made this
  module useless on exactly the path it is written for, and it is MEASURED, not
  deduced: ``pytest -o faulthandler_timeout=2 -o faulthandler_exit_on_timeout=true``
  on an armed case, then ``grep -c "DUMP TÂCHES"`` over the run's whole output →
  **0**. faulthandler exits through ``os._exit``; pytest never writes the item's
  report and the capture buffer goes with the process. Also measured, because it
  was the obvious escape route: ``os.write(2, ...)`` does not escape either,
  pytest's capture is at the DESCRIPTOR level. A file written then FLUSHED does
  survive — the bytes are already with the kernel when ``os._exit`` lands. stderr
  is still written, for the normal red path where pytest returns
  ``Captured stderr call``; it is no longer the only channel. A dump never becomes
  a failure.
- **It dies on the happy path.** A watchdog is one more task on the same loop; not
  cancelled, it recreates the immortal task ``asyncio.Runner.close()`` then waits
  for without a bound. The hang would be moved, not closed.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

__all__ = [
    "DUMP_FILE_ENV",
    "DUMP_FILE_NAME",
    "MAX_LINES",
    "MAX_TASKS",
    "DumpState",
    "dump_tasks_after",
    "durable_sink_paths",
    "format_probe_report",
    "format_task_dump",
    "write_durable_copy",
]

#: The dump's bounds. On a loaded runner, ``asyncio.all_tasks()`` of an HTTP +
#: uvicorn + SQLAlchemy bench returns several dozen tasks: the budget is an
#: OVER-estimate and known, not hoped for.
MAX_TASKS = 40
STACK_FRAME_LIMIT = 8
#: MEASURED: for a SUSPENDED coroutine, ``Task.get_stack()`` returns only ONE
#: frame — the outermost. On this bench it says ``visible_tools:292``, which we
#: already knew. The useful link (``client.py:571 ready_event.wait()``) lives
#: further down, in the ``cr_await`` chain, which only an explicit walk reaches.
AWAIT_CHAIN_LIMIT = 12
MAX_LINES = 300

#: The watchdog's join budget on the happy path.
_JOIN_BUDGET_SECONDS = 5.0

_UNAVAILABLE = "indisponible"

#: The DURABLE channel. `BRAIN_TEST_TASK_DUMP_FILE` overrides the path; by default
#: the dump lands under `$RUNNER_TEMP` (the GitHub runner's scratch), falling back
#: to the local temporary directory so that a workstation run gets the SAME channel
#: — a channel you cannot exercise at home is not a channel.
DUMP_FILE_ENV = "BRAIN_TEST_TASK_DUMP_FILE"
DUMP_FILE_NAME = "brain-v42-task-dump.log"
_RUNNER_TEMP_ENV = "RUNNER_TEMP"
#: The second sink, and it carries VISIBILITY: nothing uploads `$RUNNER_TEMP` in
#: this repository, so a dump that exists only there is durable and invisible.
#: GitHub renders `$GITHUB_STEP_SUMMARY` on the run's page without any workflow
#: changing, and it is a plain file — so it survives `os._exit` like the other.
_STEP_SUMMARY_ENV = "GITHUB_STEP_SUMMARY"
#: The step summary is Markdown: outside a fence, a stack dump would be reflowed
#: there into a single paragraph. The same fence serves as a separator in the raw
#: log, which is APPEND-only and interleaves items.
_FENCE = "```"


@dataclass
class DumpState:
    """What the caller can measure of an arming: did it fire, exactly once."""

    fired: bool = False


# ---------------------------------------------------------------------------
# Formatting — pure, hence testable with no loop and no server
# ---------------------------------------------------------------------------


def _short_path(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[-3:]) if len(parts) > 3 else path


def _describe_coro(task: Any) -> str:
    """Return a task's coro, or SAY that we could not reach it."""
    try:
        coro = task.get_coro()
    except Exception as exc:  # noqa: BLE001 — a probe never raises
        return f"coro {_UNAVAILABLE}: {exc!r}"
    if coro is None:
        return f"coro {_UNAVAILABLE}: get_coro() a rendu None"
    name = getattr(coro, "__qualname__", None) or getattr(coro, "__name__", None) or repr(coro)
    frame = getattr(coro, "cr_frame", None)
    if frame is None:
        return f"coro={name} (frame courante {_UNAVAILABLE})"
    code = getattr(frame, "f_code", None)
    filename = _short_path(getattr(code, "co_filename", "?"))
    return f"coro={name} ({filename}:{getattr(frame, 'f_lineno', '?')})"


def _describe_stack(task: Any) -> list[str]:
    """Return the suspended frames, NAMING each gap rather than omitting it."""
    try:
        frames = task.get_stack(limit=STACK_FRAME_LIMIT)
    except Exception as exc:  # noqa: BLE001
        return [f"<pile illisible: {exc!r}>"]
    if not frames:
        return ["<aucune frame récupérable: tâche terminée, annulée ou non démarrée>"]
    lines: list[str] = []
    for frame in frames:
        try:
            code = frame.f_code
            lines.append(f"{_short_path(code.co_filename)}:{frame.f_lineno} in {code.co_name}")
        except Exception as exc:  # noqa: BLE001
            lines.append(f"<frame illisible: {exc!r}>")
    return lines


def _describe_await_chain(task: Any) -> list[str]:
    """Walk down the ``cr_await`` chain — the only path to the link that waits.

    Every link is returned, including the ones we cannot read: a non-coroutine
    awaitable (Future, anyio primitive) is NAMED by its type rather than omitted,
    otherwise the chain would stop silently exactly where it becomes interesting.
    """
    try:
        node = task.get_coro()
    except Exception as exc:  # noqa: BLE001
        return [f"<chaîne d'attente illisible: {exc!r}>"]

    lines: list[str] = []
    seen: set[int] = set()
    truncated = True
    for _hop in range(AWAIT_CHAIN_LIMIT):
        if node is None:
            truncated = False
            break
        if id(node) in seen:
            lines.append("<cycle détecté dans la chaîne d'attente>")
            break
        seen.add(id(node))
        try:
            frame = getattr(node, "cr_frame", None) or getattr(node, "gi_frame", None)
            if frame is not None:
                code = frame.f_code
                lines.append(
                    f"↳ {_short_path(code.co_filename)}:{frame.f_lineno} in {code.co_name}"
                )
            else:
                lines.append(f"↳ <attente non-coroutine: {type(node).__name__} {repr(node)[:100]}>")
            node = (
                getattr(node, "cr_await", None)
                or getattr(node, "gi_yieldfrom", None)
                or getattr(node, "ag_await", None)
            )
        except Exception as exc:  # noqa: BLE001
            lines.append(f"<maillon illisible: {exc!r}>")
            truncated = False
            break
    if truncated:
        # Saying that we stop BEFORE the end is the opposite of omitting: without
        # this line, a truncated chain reads as a complete one.
        lines.append(f"<chaîne tronquée à {AWAIT_CHAIN_LIMIT} maillons>")

    if not lines:
        return ["<aucune chaîne d'attente récupérable: coro absent>"]
    return lines


def _sort_key(task: Any) -> tuple[int, str]:
    try:
        finished = 1 if task.done() else 0
    except Exception:  # noqa: BLE001
        finished = 0
    try:
        name = str(task.get_name())
    except Exception as exc:  # noqa: BLE001
        name = f"<nom {_UNAVAILABLE}: {exc!r}>"
    return (finished, name)


def format_task_dump(
    *,
    label: str,
    deadline: float,
    elapsed: float,
    tasks: Iterable[Any],
    excluded: int,
    probes: Mapping[str, str] | None = None,
    context: Mapping[str, str] | None = None,
) -> str:
    """Return the bounded dump. No I/O, no loop: unit-testable."""
    ordered = sorted(tasks, key=_sort_key)
    shown = ordered[:MAX_TASKS]
    omitted = len(ordered) - len(shown)

    lines: list[str] = [
        f"=== DUMP TÂCHES ASYNCIO — {label} ===",
    ]
    # The durable log is APPEND-only and shared by every item of the run: without a
    # timestamp or a nodeid, two dumps read back as one. These lines are INSIDE the
    # bounded list, not above it: otherwise the MAX_LINES bound would stay an
    # over-estimate of everything except the header.
    if context:
        lines.extend(f"{name}: {value}" for name, value in context.items())
    lines += [
        f"deadline armée: {deadline:.2f} s ; écoulé depuis l'armement: {elapsed:.2f} s",
        (
            f"tâches vues: {len(ordered)} ; examinées: {len(shown)} ; "
            f"{omitted} tâche(s) omise(s) par la borne MAX_TASKS={MAX_TASKS} ; "
            f"{excluded} exclue(s) (tâche courante + chien de garde)"
        ),
        "-- sondes --",
    ]
    if probes:
        lines.extend(f"{name} = {value}" for name, value in probes.items())
    elif probes is None:
        lines.append("(différées: second enregistrement — voir SONDES ci-dessous)")
    else:
        lines.append("(aucune sonde fournie à cet armement)")

    lines.append("-- tâches (non terminées d'abord, puis par nom) --")
    for index, task in enumerate(shown, start=1):
        finished, name = _sort_key(task)
        try:
            cancelling = task.cancelling()
        except Exception as exc:  # noqa: BLE001
            cancelling = f"{_UNAVAILABLE}: {exc!r}"  # type: ignore[assignment]
        lines.append(f"[{index}] name={name!r} done={bool(finished)} cancelling={cancelling}")
        lines.append(f"    {_describe_coro(task)}")
        stack = _describe_stack(task)
        chain = _describe_await_chain(task)
        # The chain's first link REPEATS the outer frame returned by get_stack().
        # One line per task, forty tasks: the budget is spent repeating what we have
        # just read.
        if stack and chain and chain[0] == f"↳ {stack[-1]}":
            chain = chain[1:]
        lines.extend(f"    {frame}" for frame in stack)
        lines.extend(f"    {link}" for link in chain)

    if len(lines) > MAX_LINES:
        kept = lines[: MAX_LINES - 1]
        kept.append(f"... {len(lines) - len(kept)} ligne(s) tronquée(s) par MAX_LINES={MAX_LINES}")
        lines = kept
    return "\n".join(lines)


def format_probe_report(*, label: str, probes: Mapping[str, str]) -> str:
    """Return the probes SEPARATELY from the dump's body.

    Separate because they do not cost the same. The dump's body makes no import;
    `collect_probes` makes four, lazily (`fastmcp`, `brain_v42.db.engine`).
    MEASURED on 2026-08-25: under `-o faulthandler_timeout=0.5`, the watchdog was
    killed DURING those imports — exit 139 and a faulthandler dump truncated in the
    middle of `<frozen posixpath>` — hence BEFORE any write. A single record made
    the diagnosis's core depend on the cost of its annex.
    """
    lines = [f"=== SONDES — {label} ==="]
    lines.extend(f"{name} = {value}" for name, value in probes.items())
    if len(lines) > MAX_LINES:
        kept = lines[: MAX_LINES - 1]
        kept.append(f"... {len(lines) - len(kept)} ligne(s) tronquée(s) par MAX_LINES={MAX_LINES}")
        lines = kept
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Probes — each isolated: a broken probe NEVER masks the real failure
# ---------------------------------------------------------------------------


#: The TWO `should_exit`, spelled out so that they are distinguishable by eye in
#: the output. They are two different variables: the first is a class attribute of
#: `sse_starlette`, shared by the whole process and never reset to False; the
#: second is a field of the bench's `uvicorn.Server` instance.
_SSE_LATCH_PROBE = "sse_starlette.sse.AppStatus.should_exit [GLOBAL processus]"
_UVICORN_EXIT_PROBE = "uvicorn.Server.should_exit [INSTANCE locale du banc]"


def _sse_exit_latch() -> str:
    """Read `sse_starlette`'s exit latch, without requiring it to be imported."""
    from sse_starlette.sse import AppStatus

    return str(AppStatus.should_exit)


def _probe(probes: dict[str, str], name: str, read: Any) -> None:
    try:
        probes[name] = str(read())
    except Exception as exc:  # noqa: BLE001
        probes[name] = f"{_UNAVAILABLE}: {exc!r}"


def collect_probes(*, mcp: Any = None, server: Any = None) -> dict[str, str]:
    """Read the state of the shared singleton, the local server and the engine.

    No SQL query: a network wait inside a watchdog would be a second hanging point.
    """
    probes: dict[str, str] = {}

    # FIRST, because it is the probe that settles the matter.
    #
    # `AppStatus.should_exit` is a CLASS attribute: PROCESS-GLOBAL, and NEVER reset
    # to False. It arms `sse_starlette/sse.py:311-313 _listen_for_exit_signal`, the
    # only immediate and SILENT exit of the four tasks of
    # `EventSourceResponse.__call__` — the very shape of the observed failure, where
    # the server sends the SSE headers then RETURNS WITH NO BODY, client still
    # connected (`ASGI callable returned without completing response`, h11 requiring
    # `disconnected=False`). The latch is set by a watcher that polls
    # `uvicorn_server.should_exit` every 0.5 s from a `threading.local()` state
    # shared by ALL pytest-asyncio loops: an earlier module can therefore have armed
    # it for every following one.
    #
    # A LATE and defensive import: `sse_starlette` may not be imported, and `_probe`
    # turns ImportError as well as AttributeError into an "unavailable" line — a
    # diagnosis never dies of its annex.
    _probe(probes, _SSE_LATCH_PROBE, _sse_exit_latch)

    if mcp is None:
        probes["mcp"] = f"{_UNAVAILABLE}: aucun serveur FastMCP passé à l'armement"
    else:
        # PRIVATE fastmcp attributes (server/mixins/lifespan.py): liable to move
        # between versions, so each under its own probe.
        #
        # ALL THREE lifespan probes were REMOVED from here — `_lifespan_ref_count`,
        # `_lifespan_result_set` and `_lifespan_result` — for the SAME reason, which
        # refutes all three at once: `mcp = FastMCP("brain",
        # mask_error_details=True)` (server.py) is built WITHOUT a lifespan, so
        # `_lifespan_proxy` (fastmcp/server/server.py:264-266) returns `{}` and EXITS
        # before reading anything. None of the three can influence a request.
        # `_lifespan_result` does alternate `None`/`{}`, but that is exactly what
        # `_started.is_set()` already says, and better: it only added a causally
        # inert duplicate. A probe that can explain nothing reads back as information
        # in a dump one only opens during an outage.
        _probe(probes, "mcp._started.is_set()", lambda: mcp._started.is_set())

    def _session_manager() -> str:
        from fastmcp.server.http import StreamableHTTPSessionManager

        return repr(StreamableHTTPSessionManager)

    _probe(probes, "fastmcp.server.http.StreamableHTTPSessionManager", _session_manager)

    if server is None:
        probes["uvicorn"] = f"{_UNAVAILABLE}: aucun serveur uvicorn passé à l'armement"
    else:
        _probe(probes, "uvicorn.server.started", lambda: server.started)
        # Named INSTANCE, because there are now TWO `should_exit` in this dump and
        # confusing them would cancel the measurement: this one is THIS bench's
        # `uvicorn.Server` object, the other is a process global.
        _probe(probes, _UVICORN_EXIT_PROBE, lambda: server.should_exit)
        # A DIRECT measurement of the "in-flight requests" the ASGI error only
        # infers at shutdown.
        _probe(probes, "len(server.server_state.tasks)", lambda: len(server.server_state.tasks))

    def _engine_present() -> str:
        import brain_v42.db.engine as engine_module

        return repr(engine_module._engine is not None)

    def _pool_status() -> str:
        import brain_v42.db.engine as engine_module

        engine = engine_module._engine
        if engine is None:
            return f"{_UNAVAILABLE}: aucun moteur installé"
        return str(engine.pool.status())

    _probe(probes, "db.engine._engine is not None", _engine_present)
    _probe(probes, "db.engine pool.status()", _pool_status)

    return probes


# ---------------------------------------------------------------------------
# The durable channel — the only output that still exists after `os._exit`
# ---------------------------------------------------------------------------


def durable_sink_paths() -> list[Path]:
    """Where the dump must exist AFTER the process's brutal exit.

    Two sinks, for two readers, and neither is a buffer:

    - a file — `BRAIN_TEST_TASK_DUMP_FILE` if it is set, otherwise
      `$RUNNER_TEMP/brain-v42-task-dump.log`, falling back to the local temporary
      directory so that a workstation run writes into the same channel as CI;
    - `$GITHUB_STEP_SUMMARY` when it exists, because the first file is durable but
      INVISIBLE: nothing uploads `$RUNNER_TEMP` in this repository, and adding that
      step touches the workflow, outside this batch's surface. The step summary, by
      contrast, is rendered by GitHub on the run's page as is.
    """
    explicit = os.environ.get(DUMP_FILE_ENV, "").strip()
    base = os.environ.get(_RUNNER_TEMP_ENV, "").strip() or tempfile.gettempdir()
    paths = [Path(explicit) if explicit else Path(base) / DUMP_FILE_NAME]
    summary = os.environ.get(_STEP_SUMMARY_ENV, "").strip()
    if summary:
        paths.append(Path(summary))
    return paths


def write_durable_copy(text: str, *, paths: Iterable[Path] | None = None) -> list[str]:
    """Write the dump into each sink, FLUSHED, and return what happened.

    The flush IS the mechanism: it hands the bytes to the kernel, so the file
    carries the dump even when the process then exits through `os._exit`, which
    unwinds neither `atexit` nor Python's buffers. No `fsync`: what kills this
    diagnosis is a brutal process exit, not a power cut — claiming to cover the
    second would cost a synchronous I/O inside a watchdog.

    Never raises and never fails the observed test. An unreachable sink is NAMED in
    the dump itself, never left unsaid.
    """
    notes: list[str] = []
    for path in durable_sink_paths() if paths is None else paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n{_FENCE}\n{text}\n{_FENCE}\n")
                handle.flush()
            notes.append(f"copie durable écrite: {path}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"copie durable {_UNAVAILABLE} ({path}): {exc!r}")
    return notes


# ---------------------------------------------------------------------------
# The watchdog
# ---------------------------------------------------------------------------


async def _watch(
    deadline: float,
    *,
    label: str,
    state: DumpState,
    mcp: Any,
    server: Any,
    stream: TextIO | None,
) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        await asyncio.sleep(deadline)
    except asyncio.CancelledError:
        # Happy path: the observed wait returned before the deadline.
        return

    # EXACTLY ONCE per arming: after this block the task terminates.
    #
    # The ORDER of this block is the fix, not a matter of style. The race is against
    # an `os._exit` triggered by faulthandler: what is written and flushed before
    # exists, the rest never existed. So the CORE first — the tasks and their await
    # chains, zero imports — written to disk, and only then the probes, which cost
    # lazy imports and under which the watchdog was killed in flight during the
    # 2026-08-25 measurement.
    try:
        me = asyncio.current_task()
        watched = [t for t in asyncio.all_tasks() if t is not me]
        excluded = 1 if me is not None else 0
        core = format_task_dump(
            label=label,
            deadline=deadline,
            elapsed=loop.time() - started,
            tasks=watched,
            excluded=excluded,
            context={
                "horodatage": datetime.now(tz=UTC).isoformat(timespec="seconds"),
                "item": os.environ.get("PYTEST_CURRENT_TEST", _UNAVAILABLE),
                "pid": str(os.getpid()),
            },
        )
    except Exception as exc:  # noqa: BLE001
        core = f"=== DUMP TÂCHES ASYNCIO — {label} ===\n# dump {_UNAVAILABLE}: {exc!r}"
    notes = write_durable_copy(core)

    try:
        annex = format_probe_report(label=label, probes=collect_probes(mcp=mcp, server=server))
    except Exception as exc:  # noqa: BLE001
        annex = f"=== SONDES — {label} ===\n# sondes {_UNAVAILABLE}: {exc!r}"
    notes += write_durable_copy(annex)

    print(
        "\n".join([core, annex, *(f"# {note}" for note in notes)]),
        file=stream if stream is not None else sys.stderr,
        flush=True,
    )
    state.fired = True


@asynccontextmanager
async def dump_tasks_after(
    deadline: float,
    *,
    label: str,
    mcp: Any = None,
    server: Any = None,
    stream: TextIO | None = None,
) -> AsyncIterator[DumpState]:
    """Arm a watchdog that DUMPS the tasks if the body exceeds ``deadline``.

    It cancels nothing, raises nothing and does not change the test's verdict. On
    the happy path it is cancelled in the ``finally`` — otherwise we recreate the
    immortal task that hangs at the loop's close.
    """
    state = DumpState()
    watchdog = asyncio.create_task(
        _watch(deadline, label=label, state=state, mcp=mcp, server=server, stream=stream),
        name=f"dump-tasks-after[{label}]",
    )
    try:
        yield state
    finally:
        watchdog.cancel()
        with suppress(asyncio.CancelledError, TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(watchdog), timeout=_JOIN_BUDGET_SECONDS)
