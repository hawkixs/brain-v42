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
    content = _content()
    assert (
        'BRAIN_DREAM_CODEX_FAST_MODEL="${BRAIN_DREAM_CODEX_FAST_MODEL:-gpt-5.6-terra}"' in content
    )
    assert 'BRAIN_DREAM_CODEX_DEEP_MODEL="${BRAIN_DREAM_CODEX_DEEP_MODEL:-gpt-5.6-sol}"' in content


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


def test_both_codex_entries_use_the_project_uv_environment_and_project_key() -> None:
    dream_content = _content()
    smoke_content = PROMOTE_SMOKE.read_text(encoding="utf-8")

    for content in (dream_content, smoke_content):
        assert "uv run python -m scripts.dream.codex_runner" in content
        assert "python3 -m scripts.dream.codex_runner" not in content
        assert '--project-key "$PROJECT_KEY"' in content


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
    """
    content = _content()

    assert "Dream capability enforcement requires the Codex provider" not in content
    assert "uv run python -m scripts.dream.claude_runner" in content
    assert '--project-key "$PROJECT_KEY"' in content
    # The wildcard was the other half of the hole: a scoped bearer with an
    # unrestricted tool list is only half a firewall.
    assert '--allowedTools "mcp__brain-v42__*"' not in content


def test_preflight_covers_every_provider_and_every_pool_project() -> None:
    """Deux boucles imbriquées, et chacune ferme un mode de panne distinct.

    Sur les PROJETS : vérifier le seul premier laisserait une nuit démarrer
    puis mourir au troisième, deux projets déjà mutés.

    Sur les PROVIDERS : c'est ce qui rend la chaîne utile. Le préflight détecte
    exactement ce qui tue un rail — binaire absent, jeton manquant, abonnement
    expiré. Le faire échouer la NUIT plutôt que le MAILLON tuerait la nuit
    précisément le jour où le secours devait servir.
    """
    content = _content()

    assert "preflight_provider()" in content
    assert 'for _preflight_project in "${PROJECT_POOL[@]}"' in content
    assert 'for _provider in "${PROVIDER_CHAIN[@]}"' in content
    assert "scripts.dream.claude_runner" in content
    assert "scripts.dream.codex_runner" in content


def test_the_night_still_dies_when_no_provider_passes_its_preflight() -> None:
    """Le fail-closed est conservé : il porte sur l'ENSEMBLE, pas sur le
    premier maillon. Sans cette garde, retirer les providers un par un
    finirait par laisser une chaîne vide tourner dans le vide."""
    content = _content()

    assert "aucun provider de la chaîne ne passe son préflight" in content


def test_both_entry_points_canonicalize_known_project_aliases_before_runner_use() -> None:
    """Les deux rails replient `brain` et `brain_v42`, à des étages différents.

    `dream.sh` canonicalise chaque entrée du POOL, donc dans la variable de
    boucle du parseur ; `_promote_smoke.sh` n'a pas de pool et le fait sur sa
    clé unique. Le contrat commun est le repli des deux alias avant que la clé
    n'atteigne le runner — pas le nom de la variable qui le porte.
    """
    assert 'brain|brain_v42) _entry="brain-v42"' in _content()
    assert 'brain|brain_v42) PROJECT_KEY="brain-v42"' in PROMOTE_SMOKE.read_text(encoding="utf-8")


def test_promote_smoke_renders_the_canonical_project_and_allows_an_isolated_output_dir() -> None:
    content = PROMOTE_SMOKE.read_text(encoding="utf-8")

    assert 'scripts/dream/phase_promote.md "$PROJECT_KEY"' in content
    assert 'OUT_DIR="${OUT_DIR:-/tmp/promote_smoke_${DATE}}"' in content
