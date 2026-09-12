"""Static orchestration contract for the Dream Claude-to-Codex migration."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DREAM_SH = REPO_ROOT / "scripts" / "dream.sh"
PROMOTE_SMOKE = REPO_ROOT / "scripts" / "dream" / "_promote_smoke.sh"


def _content() -> str:
    return DREAM_SH.read_text(encoding="utf-8")


def test_codex_is_the_explicit_default_agent_provider() -> None:
    assert 'BRAIN_DREAM_AGENT_PROVIDER="${BRAIN_DREAM_AGENT_PROVIDER:-codex}"' in _content()


def test_codex_fast_and_deep_models_are_subscription_backed_defaults() -> None:
    """Phase 1 (scan, clean, connect) runs Luna, phase 2 (synth, promote, reorg)
    runs Astra — operator decision of 2026-09-12, both canaried through the live
    Codex binary that day."""
    content = _content()
    assert 'BRAIN_DREAM_CODEX_FAST_MODEL="${BRAIN_DREAM_CODEX_FAST_MODEL:-gpt-5.6-luna}"' in content
    assert 'BRAIN_DREAM_CODEX_DEEP_MODEL="${BRAIN_DREAM_CODEX_DEEP_MODEL:-gpt-6-astra}"' in content


def test_codex_reasoning_defaults_follow_the_tier() -> None:
    """Luna at ``high`` for the basic phases, Astra at ``max`` for the deep ones.

    ``max`` is declared by Codex for gpt-6-astra (``~/.codex/models_cache.json``,
    measured 2026-09-12) and must be accepted by ``scripts/dream/codex_runner.py``
    — a default the runner refuses would fail every deep phase before launch.
    """
    content = _content()
    assert 'BRAIN_DREAM_CODEX_FAST_REASONING="${BRAIN_DREAM_CODEX_FAST_REASONING:-high}"' in content
    assert 'BRAIN_DREAM_CODEX_DEEP_REASONING="${BRAIN_DREAM_CODEX_DEEP_REASONING:-max}"' in content


def test_all_six_agent_phases_use_provider_neutral_model_tiers() -> None:
    content = _content()
    expected_tiers = {
        "scan": "fast",
        "clean": "fast",
        "connect": "fast",
        "synth": "deep",
        "promote": "deep",
        "reorg": "deep",
    }

    for phase, tier in expected_tiers.items():
        assert f'"{phase}:{tier}:' in content


def test_codex_branch_delegates_to_the_isolated_runner() -> None:
    content = _content()
    assert "scripts.dream.codex_runner" in content
    assert '"$BRAIN_DREAM_AGENT_PROVIDER"' in content


def test_claude_remains_an_explicit_rollback_provider() -> None:
    content = _content()
    assert "scripts.dream.claude_runner" in content
    assert "claude)" in content


def test_unknown_provider_fails_closed_instead_of_consuming_claude_quota() -> None:
    content = _content()
    assert "Unsupported BRAIN_DREAM_AGENT_PROVIDER" in content


def test_promote_smoke_uses_the_same_provider_boundary_as_the_night() -> None:
    """A smoke run must be comparable to the night it stands in for.

    The Claude branch here used to call the CLI directly with the repository
    .mcp.json and the tool wildcard.  Left as-is it would smoke-test the ADMIN
    token and the full tool surface — i.e. no longer the phase that actually
    runs.  Both branches therefore go through their scoped runner.
    """
    content = PROMOTE_SMOKE.read_text(encoding="utf-8")
    assert 'BRAIN_DREAM_AGENT_PROVIDER="${BRAIN_DREAM_AGENT_PROVIDER:-codex}"' in content
    assert "scripts.dream.codex_runner" in content
    assert "scripts.dream.claude_runner" in content
    assert '--allowedTools "mcp__brain-v42__*"' not in content
    assert "$ROOT/.mcp.json" not in content


def test_promote_smoke_codex_entry_uses_the_project_uv_environment_and_project_key() -> None:
    """`_promote_smoke.sh` is a standalone harness, untouched by lot 2 (Brain
    ticket afd56820, which only moved dream.sh's own call site to Python) --
    it still shells out to the codex runner module directly."""
    smoke_content = PROMOTE_SMOKE.read_text(encoding="utf-8")

    assert "uv run python -m scripts.dream.codex_runner" in smoke_content
    assert "python3 -m scripts.dream.codex_runner" not in smoke_content
    assert '--project-key "$PROJECT_KEY"' in smoke_content


def test_dream_sh_project_key_flows_into_the_python_chain_and_the_codex_runner_argv() -> None:
    """TRANSPOSED (lot 2, Brain ticket afd56820): `run_phase`/`run_phase_chain`
    moved to `brain_v42.agents.phase`/`chain`, and dream.sh's call site no
    longer names `scripts.dream.codex_runner` -- it shells out once to
    `brain_v42.agents.run_phase_chain`, which runs `phase.run_phase` per
    provider. The guarantee (the codex rail carries the project key) is
    unchanged; only where it is asserted moved with the code:
    `--project-key "$PROJECT_KEY"` must reach the Python chain from dream.sh,
    and `phase.runner_argv` must carry it into the codex runner's own argv
    (pinned byte for byte by tests/unit/agents/test_chain_golden.py; this test
    only proves it is *reachable* through the codex tier, not the exact
    bytes)."""
    from pathlib import Path as _Path

    from brain_v42.agents import phase

    assert "uv run python -m scripts.dream.codex_runner" not in _content()
    assert "uv run python -m brain_v42.agents.run_phase_chain" in _content()
    assert '--project-key "$PROJECT_KEY"' in _content()

    paths = phase.PhasePaths.build(
        log_dir=_Path("/tmp/logs"),
        timestamp="2026-09-12",
        project_key="brain-v42",
        phase="scan",
        dream_dir=_Path("/tmp/dream"),
    )
    argv = phase.runner_argv(
        "codex",
        phase="scan",
        project_key="brain-v42",
        model="gpt-5.6-luna",
        reasoning="high",
        timeout_minutes=5,
        max_turns=30,
        paths=paths,
        executable="codex",
    )
    assert "--project-key" in argv
    assert argv[argv.index("--project-key") + 1] == "brain-v42"


def test_both_entry_points_still_read_the_capability_enforcement_killswitch() -> None:
    for content in (_content(), PROMOTE_SMOKE.read_text(encoding="utf-8")):
        assert (
            'BRAIN_DREAM_CAPABILITY_ENFORCEMENT="${BRAIN_DREAM_CAPABILITY_ENFORCEMENT-false}"'
            in content
        )
        assert '"$BRAIN_DREAM_CAPABILITY_ENFORCEMENT" == "true"' in content


def test_claude_rail_is_scoped_rather_than_refused_under_enforcement() -> None:
    """The refusal was lifted only because its cause was removed.

    Deleting the guard on its own would hand the Claude rail the ADMIN token
    again — green logs, six unscoped phases, nothing saying so. So the contract
    is the *replacement*: the rail must delegate to the runner that carries a
    per-(project, phase) bearer, and the old refusal must be gone.

    TRANSPOSED (lot 2, Brain ticket afd56820): dream.sh no longer names
    `scripts.dream.claude_runner` directly -- it shells out to
    `brain_v42.agents.run_phase_chain`, whose `phase.runner_module("claude")`
    is what still carries the scoped, per-(project, phase) runner. The
    guarantee (project key reaches the claude rail, no wildcard tool list) is
    unchanged; only where it is asserted moved with the code.
    """
    from brain_v42.agents import phase

    content = _content()

    assert "Dream capability enforcement requires the Codex provider" not in content
    assert "uv run python -m scripts.dream.claude_runner" not in content
    assert phase.runner_module("claude") == "brain_v42.agents.providers.claude"
    assert '--project-key "$PROJECT_KEY"' in content
    # The wildcard was the other half of the hole: a scoped bearer with an
    # unrestricted tool list is only half a firewall.
    assert '--allowedTools "mcp__brain-v42__*"' not in content


def test_preflight_covers_every_provider_and_every_pool_project() -> None:
    """Two nested loops, and each closes a distinct failure mode.

    On PROJECTS: checking only the first would let a night start then die on the
    third, two projects already mutated.

    On PROVIDERS: that is what makes the chain useful. The preflight detects
    exactly what kills a rail — missing binary, missing token, expired
    subscription. Making it fail the NIGHT rather than the LINK would kill the
    night precisely on the day the fallback was meant to serve.
    """
    content = _content()

    assert "preflight_provider()" in content
    assert 'for _preflight_project in "${PROJECT_POOL[@]}"' in content
    assert 'for _provider in "${PROVIDER_CHAIN[@]}"' in content
    assert "scripts.dream.claude_runner" in content
    assert "scripts.dream.codex_runner" in content


def test_the_night_still_dies_when_no_provider_passes_its_preflight() -> None:
    """The fail-closed posture is kept: it applies to the WHOLE SET, not to the
    first link. Without this guard, removing providers one by one would end up
    letting an empty chain run over nothing."""
    content = _content()

    assert "aucun provider de la chaîne ne passe son préflight" in content


def test_both_entry_points_canonicalize_known_project_aliases_before_runner_use() -> None:
    """Both rails fold `brain` and `brain_v42`, at different levels.

    `dream.sh` canonicalizes every POOL entry, hence in the parser's loop
    variable; `_promote_smoke.sh` has no pool and does it on its single key. The
    shared contract is folding both aliases before the key reaches the runner —
    not the name of the variable carrying it.
    """
    assert 'brain|brain_v42) _entry="brain-v42"' in _content()
    assert 'brain|brain_v42) PROJECT_KEY="brain-v42"' in PROMOTE_SMOKE.read_text(encoding="utf-8")


def test_promote_smoke_renders_the_canonical_project_and_allows_an_isolated_output_dir() -> None:
    content = PROMOTE_SMOKE.read_text(encoding="utf-8")

    assert 'scripts/dream/phase_promote.md "$PROJECT_KEY"' in content
    assert 'OUT_DIR="${OUT_DIR:-/tmp/promote_smoke_${DATE}}"' in content
