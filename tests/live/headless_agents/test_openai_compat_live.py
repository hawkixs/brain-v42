"""Replay the ``openai-compat`` presets against the real APIs (spec 3.2, section 4).

WHY IT IS OPT-IN. Each test spends real quota: it carries the ``live`` marker,
excluded by ``addopts``, skips unless ``HA_LIVE=1``, and skips a preset whose
key variable is not set (the zero-quota ``probe``). Run it deliberately:

    HA_LIVE=1 MISTRAL_API_KEY=... .venv/bin/pytest -m live tests/live/headless_agents -v -rA

The model of each preset can be overridden with ``HA_LIVE_<PRESET>_MODEL``
(for example ``HA_LIVE_NVIDIA_MODEL``): catalogues move, the contract does not.

WHAT IT ASSERTS ON. The answer, the measured usage, and that the key appears
in nothing the run wrote -- including after a refused key, whose error body a
provider may echo.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from headless_agents.registry import get_provider, probe
from headless_agents.spec import RunSpec

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("HA_LIVE") != "1", reason="live: spends real quota, set HA_LIVE=1"
    ),
]

#: nvidia: ``openai/gpt-oss-20b`` is a reasoning model -- measured 2026-09-24 it took
#: 38 s to 60 s+ for a one-word reply and tripped the 60 s deadline below, while
#: ``z-ai/glm-5.3-flash`` answered the same prompt in 29-38 s (several listed
#: catalogue models returned 404 or 410 for this key).
DEFAULT_MODELS = {
    "openrouter": "openai/gpt-4o-mini",
    "mistral": "mistral-small-latest",
    "nvidia": "z-ai/glm-5.3-flash",
}
KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}


def _model(preset: str) -> str:
    return os.environ.get(f"HA_LIVE_{preset.upper()}_MODEL", DEFAULT_MODELS[preset])


def _written(run_dir: Path) -> str:
    return "\n".join(p.read_text(errors="replace") for p in run_dir.rglob("*") if p.is_file())


@pytest.mark.parametrize("preset", sorted(DEFAULT_MODELS))
def test_a_preset_answers_with_measured_usage(preset: str, tmp_path: Path) -> None:
    if not probe(preset).available:
        pytest.skip(f"{KEY_ENV[preset]} is not set")
    key = os.environ[KEY_ENV[preset]]
    result = get_provider(preset).run(
        RunSpec(
            prompt="Reply with exactly the word OK and nothing else.",
            model=_model(preset),
            timeout_seconds=60.0,
            run_dir=tmp_path / "run",
            extra={"max_tokens": 256, "temperature": 0},
        )
    )
    assert result.exit_code == 0, (tmp_path / "run" / "stderr.log").read_text(errors="replace")
    assert result.text is not None and "OK" in result.text.upper()
    assert result.model_reported
    assert result.tokens is not None and result.tokens.input and result.tokens.output
    if preset == "openrouter":
        assert result.cost_usd is not None
    assert key not in _written(tmp_path)


@pytest.mark.parametrize("preset", sorted(DEFAULT_MODELS))
def test_a_refused_key_stops_the_chain_and_is_never_written(
    preset: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bogus = "sk-live-bogus-0000000000000000000000"
    monkeypatch.setenv(KEY_ENV[preset], bogus)
    result = get_provider(preset).run(
        RunSpec(
            prompt="Reply OK.",
            model=_model(preset),
            timeout_seconds=60.0,
            run_dir=tmp_path / "run",
        )
    )
    assert result.exit_code == 1
    assert result.text is None
    assert bogus not in _written(tmp_path)
