"""Per-rail isolation and confinement proofs (spec 0.5.0 §3.8.0), recorded for the engine.

Since 0.5.2 lot 4a the harness lives in :mod:`headless_agents.prove`, so the installed
``ha`` records these proofs with ``ha prove`` -- no repository checkout needed. These
live tests are thin wrappers over the same code: what a proof plants, what counts as a
leak or a refusal, and when nothing may be recorded, are the module's (see its
docstring), never redefined here.

RECORDED in the operator's state directory (``~/.local/state/ha/proofs/``): a pass or a
failure with the rail's probed version; an inconclusive proof records nothing.
Opt-in: spends real quota.

    HA_LIVE=1 .venv/bin/pytest -m live tests/live/headless_agents/test_proofs_live.py -v -rA

An isolation proof binds to a fingerprint of the INSTALLED package's isolation source.
Run from a checkout (an editable install), it would name the checkout's source instead,
and the installed ``ha`` would refuse the rail: ``test_isolation`` therefore fails before
any run unless ``HA_PROVE_FROM_CHECKOUT=1`` says both are the same source -- the rule
``ha prove`` applies too.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from headless_agents import prove
from headless_agents.config_paths import state_dir

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("HA_LIVE") != "1", reason="live: spends real quota, set HA_LIVE=1"
    ),
]

LIVE_ROOT = Path.home() / ".cache" / "ha-live"
REAL_HOME = Path.home()

#: The model each rail is proven with: the package hard-codes none; a test may.
MODEL = {
    "claude": "haiku",
    "codex": "gpt-6-luna",
    "opencode": "opencode-go/glm-5.3-flash",
    "agy": "",
}


@pytest.fixture
def live_root() -> Iterator[Path]:
    root = LIVE_ROOT / f"proof-{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        if os.environ.get("HA_LIVE_KEEP") != "1":
            shutil.rmtree(root, ignore_errors=True)


def _prove(rail: str, kind: prove.Kind, live_root: Path) -> prove.Verdict:
    return prove.prove(
        rail,
        kind,
        model=MODEL[rail],
        state=state_dir(os.environ, home=REAL_HOME),
        home=REAL_HOME,
        environ=os.environ,
        root=live_root,
    )


def _judge(verdict: prove.Verdict) -> None:
    if verdict.outcome == "skipped":
        pytest.skip(f"{verdict.rail}: {verdict.reason}")
    if verdict.outcome == "inconclusive":
        pytest.fail(
            f"{verdict.rail} {verdict.version}: inconclusive, no proof recorded: {verdict.reason}"
        )
    assert verdict.outcome == "passed", f"{verdict.rail} {verdict.version}: {verdict.reason}"


@pytest.mark.parametrize("rail", ["claude", "codex", "agy", "opencode"])
def test_isolation(rail: str, live_root: Path) -> None:
    if prove.running_from_checkout() and os.environ.get("HA_PROVE_FROM_CHECKOUT") != "1":
        pytest.fail(
            "isolation cannot be recorded from a development install: its fingerprint is "
            "this checkout's, not the installed ha's; run the installed `ha prove`, or set "
            "HA_PROVE_FROM_CHECKOUT=1 if both are the same source"
        )
    _judge(_prove(rail, "isolation", live_root))


@pytest.mark.parametrize("rail", ["claude", "codex", "agy", "opencode"])
def test_confinement(rail: str, live_root: Path) -> None:
    """A write role cannot write outside its worktree (headless_agents.prove)."""
    _judge(_prove(rail, "confinement", live_root))
