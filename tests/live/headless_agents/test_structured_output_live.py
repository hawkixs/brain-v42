"""Schema-constrained output against the real claude and codex CLIs (0.5.3 lot 1).

Each test runs one provider directly, through the library -- no engine, so no
isolation proof is needed -- with the strict schema 0.5.3 Task 0 measured, no context
bundle (the library's ``--context none``), nothing reachable (no workspace, no
server) and the model the live proofs use (``test_proofs_live.MODEL``). It asserts
the run exits 0 with a text that is JSON carrying a boolean ``ok``: the rail enforced
the schema, and ha checked that the answer is JSON.

Opt-in: spends real quota.

    HA_LIVE=1 .venv/bin/python -m pytest -m live \\
        tests/live/headless_agents/test_structured_output_live.py -v -rA

A FAILURE IS A FINDING, not a flake: re-run once to rule out the network, then report
it as measured -- never loosen the assertion. The exit code, the answer and, on a
failure, the tail of the run's log are recorded as properties either way.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from headless_agents.profile import CapabilityProfile
from headless_agents.registry import get_provider, probe
from headless_agents.spec import RunSpec

from .test_proofs_live import LIVE_ROOT, MODEL
from .test_workspace_live import RUN_TIMEOUT_SECONDS, _operator_environment

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("HA_LIVE") != "1", reason="live: spends real quota, set HA_LIVE=1"
    ),
]

#: Task 0's strict schema: codex's API takes nothing looser (measured on 0.156.0).
SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
PROMPT = "Answer with ok = true."


@pytest.fixture
def live_root() -> Iterator[Path]:
    root = LIVE_ROOT / f"structured-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        if os.environ.get("HA_LIVE_KEEP") != "1":
            shutil.rmtree(root, ignore_errors=True)


def _answers_within_the_schema(
    rail: str, live_root: Path, record_property: Callable[[str, object], None]
) -> None:
    found = probe(rail)
    if not found.available:
        pytest.skip(f"{rail} unavailable here: {found.detail}")
    record_property("version", found.version)
    result = get_provider(rail).run(
        RunSpec(
            prompt=PROMPT,
            model=MODEL[rail],
            run_dir=live_root / "run",
            profile=CapabilityProfile(),  # no workspace, no server: it reaches nothing
            context=None,  # no context bundle: the library's --context none
            timeout_seconds=RUN_TIMEOUT_SECONDS,
            reasoning_effort="low",
            environment=_operator_environment(),
            output_schema=SCHEMA,
        )
    )
    record_property("exit_code", result.exit_code)
    record_property("answer", result.text)
    log = result.stderr_log or result.raw_log
    if result.exit_code != 0 and log is not None and log.is_file():
        record_property("log_tail", log.read_text(encoding="utf-8", errors="replace")[-800:])
    assert result.exit_code == 0, f"{rail} {found.version}: exit {result.exit_code}"
    assert result.text is not None
    assert json.loads(result.text)["ok"] in (True, False)


def test_claude_answers_within_the_schema(
    live_root: Path, record_property: Callable[[str, object], None]
) -> None:
    """``--output-format json --json-schema``: the envelope's ``structured_output``."""
    _answers_within_the_schema("claude", live_root, record_property)


def test_codex_answers_within_the_schema(
    live_root: Path, record_property: Callable[[str, object], None]
) -> None:
    """``exec --output-schema FILE``: the final message, under codex's strict mode."""
    _answers_within_the_schema("codex", live_root, record_property)
