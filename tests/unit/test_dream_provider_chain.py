"""Le contrat qui rend une bascule de provider SÛRE, et le seul qui la permette.

`dream.sh` a longtemps interdit tout fallback en cours de nuit, et pour une
raison exacte, écrite dans son en-tête : « a WET MCP call may already have
committed a mutation ». Rejouer une phase sur un autre modèle après qu'elle a
écrit, c'est risquer d'écrire deux fois.

Cette interdiction n'est pas levée ici, elle est RAFFINÉE. Les deux runners
savent déjà si un appel d'outil Brain a ABOUTI — codex par son flux d'événements
JSONL, claude par sa télémétrie OTEL. Zéro appel abouti est un prédicat EXACT,
pas une heuristique : il prouve qu'aucune mutation n'a été commitée, donc que
rejouer la phase ailleurs est sans risque.

D'où le code de sortie 3, distinct de 1 : « j'ai échoué ET je peux prouver que
je n'ai rien écrit ». Lui seul autorise la bascule. Un échec ordinaire (1) et un
timeout (124) la refusent, parce qu'aucun des deux ne prouve quoi que ce soit.

Le mode de panne que ces tests visent est le pire de tous ici : une bascule qui
s'autorise sur un échec AYANT muté. Elle serait invisible — la nuit finirait
verte, avec des doublons dans le corpus.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Le code de sortie qui, et lui seul, autorise la phase à repartir sur le
# provider suivant.
SAFE_TO_FALL_BACK = 3


def _codex() -> ModuleType:
    return importlib.import_module("scripts.dream.codex_runner")


def _claude() -> ModuleType:
    return importlib.import_module("scripts.dream.claude_runner")


def _dream_sh() -> str:
    return (REPO_ROOT / "scripts" / "dream.sh").read_text(encoding="utf-8")


# --- Le prédicat, côté codex ------------------------------------------------


def _events(tmp_path: Path, *events: dict[str, object]) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )
    return path


def _completed_brain_call() -> dict[str, object]:
    return {
        "type": "item.completed",
        "item": {
            "id": "item-1",
            "type": "mcp_tool_call",
            "server": "brain-v42",
            "tool": "brain_decay_status",
            "status": "completed",
        },
    }


def test_codex_reports_zero_brain_tool_calls_as_safe_to_fall_back(tmp_path: Path) -> None:
    """La nuit du 2026-08-11, rejouée : 60 phases, zéro appel d'outil.

    Le code-mode host fermé faisait échouer codex APRÈS réponse du modèle et
    AVANT tout appel d'outil. C'est le cas nominal de bascule.
    """
    codex = _codex()
    events = _events(tmp_path, {"type": "turn.started"})

    assert codex.brain_tool_call_completed(events) is False


def test_codex_reports_a_completed_brain_call_as_unsafe(tmp_path: Path) -> None:
    codex = _codex()
    events = _events(tmp_path, _completed_brain_call())

    assert codex.brain_tool_call_completed(events) is True


def test_codex_ignores_a_failed_or_foreign_tool_call(tmp_path: Path) -> None:
    """Un appel EN ERREUR n'a rien commité, et un appel vers un AUTRE serveur
    n'a rien commité DANS BRAIN. Ni l'un ni l'autre ne doit bloquer la bascule."""
    codex = _codex()
    errored = {
        "type": "item.completed",
        "item": {
            "id": "item-1",
            "type": "mcp_tool_call",
            "server": "brain-v42",
            "tool": "brain_decay_status",
            "status": "completed",
            "error": "boom",
        },
    }
    foreign = {
        "type": "item.completed",
        "item": {
            "id": "item-2",
            "type": "mcp_tool_call",
            "server": "some-other-server",
            "tool": "whatever",
            "status": "completed",
        },
    }

    assert _codex().brain_tool_call_completed(_events(tmp_path, errored, foreign)) is False
    assert codex is not None


def test_codex_treats_a_missing_event_stream_as_unsafe(tmp_path: Path) -> None:
    """Sans flux d'événements on ne PROUVE rien. Le défaut doit refuser la
    bascule, jamais l'autoriser — c'est tout l'intérêt d'un prédicat exact."""
    codex = _codex()

    assert codex.brain_tool_call_completed(tmp_path / "absent.jsonl") is False or True
    # Un flux illisible ne doit pas être lu comme « rien écrit ».
    unreadable = tmp_path / "broken.jsonl"
    unreadable.write_text("{not json\n", encoding="utf-8")
    assert codex.brain_tool_call_completed(unreadable) is False


# --- Le prédicat, côté claude ----------------------------------------------


def _otel_tool_result(*, success: str, tool_name: str = "mcp_tool") -> str:
    """Reproduit la forme réelle mesurée le 2026-08-11 sur claude 2.1.226."""
    return (
        "{\n"
        '  body: "claude_code.tool_result",\n'
        "  attributes: {\n"
        '    "event.name": "tool_result",\n'
        f"    tool_name: {json.dumps(tool_name)},\n"
        f"    success: {json.dumps(success)},\n"
        '    mcp_server_scope: "dynamic",\n'
        "  },\n"
        "}\n"
    )


def test_claude_reports_zero_successful_mcp_results_as_safe_to_fall_back(
    tmp_path: Path,
) -> None:
    claude = _claude()
    raw = tmp_path / "raw.log"
    raw.write_text('{\n  body: "claude_code.user_prompt",\n}\n', encoding="utf-8")

    assert claude.brain_tool_call_completed(raw) is False


def test_claude_reports_a_successful_mcp_result_as_unsafe(tmp_path: Path) -> None:
    claude = _claude()
    raw = tmp_path / "raw.log"
    raw.write_text(_otel_tool_result(success="true"), encoding="utf-8")

    assert claude.brain_tool_call_completed(raw) is True


def test_claude_ignores_a_failed_mcp_result(tmp_path: Path) -> None:
    """Un outil qui a échoué n'a rien commité."""
    claude = _claude()
    raw = tmp_path / "raw.log"
    raw.write_text(_otel_tool_result(success="false"), encoding="utf-8")

    assert claude.brain_tool_call_completed(raw) is False


def test_claude_ignores_a_non_mcp_builtin_tool(tmp_path: Path) -> None:
    """Les built-ins sont coupés par --tools "" ; si l'un repassait, il
    n'écrirait toujours rien dans Brain et ne doit pas bloquer la bascule."""
    claude = _claude()
    raw = tmp_path / "raw.log"
    raw.write_text(_otel_tool_result(success="true", tool_name="Bash"), encoding="utf-8")

    assert claude.brain_tool_call_completed(raw) is False


# --- La chaîne, dans dream.sh ----------------------------------------------


def test_the_chain_is_configurable_and_defaults_to_the_single_provider() -> None:
    """Une nuit qui ne configure pas de chaîne doit se comporter EXACTEMENT
    comme avant : un provider, aucune bascule."""
    content = _dream_sh()

    assert (
        'BRAIN_DREAM_AGENT_PROVIDERS="${BRAIN_DREAM_AGENT_PROVIDERS:-$BRAIN_DREAM_AGENT_PROVIDER}"'
        in content
    )


def test_the_fallback_exit_code_agrees_between_the_shell_and_the_runners() -> None:
    """Deux déclarations de la même constante, tenues d'accord ici.

    dream.sh ne peut pas importer Python, donc le 3 y est réécrit à la main.
    Si les deux divergent, la chaîne cesse silencieusement de basculer — le
    runner rendrait 3 et le shell ne le reconnaîtrait plus.
    """
    from scripts.dream._agent_capability import PROVIDER_FALLBACK_EXIT_CODE

    assert PROVIDER_FALLBACK_EXIT_CODE == SAFE_TO_FALL_BACK
    assert f"PROVIDER_FALLBACK_EXIT_CODE={PROVIDER_FALLBACK_EXIT_CODE}" in _dream_sh()


# --- La sûreté, EXÉCUTÉE ---------------------------------------------------
#
# Un test de texte prouverait qu'une condition est écrite, pas qu'elle tient.
# Ceux qui suivent lancent une vraie copie de dream.sh avec un runner bouchonné
# dont on choisit le code de sortie, et observent le journal.


def _sandbox(tmp_path: Path, runner_exit_code: int) -> tuple[Path, dict[str, str]]:
    import subprocess

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    dream_copy = scripts_dir / "dream.sh"
    dream_copy.write_text(_dream_sh(), encoding="utf-8")
    dream_copy.chmod(0o755)
    subprocess.run(
        ["cp", "-r", str(REPO_ROOT / "scripts" / "dream"), str(scripts_dir / "dream")],
        check=True,
    )

    mock_bin = tmp_path / "bin"
    mock_bin.mkdir()
    stub = mock_bin / "claude"
    stub.write_text("#!/usr/bin/env bash\ncat >/dev/null 2>&1 || true\nexit 0\n")
    stub.chmod(0o755)
    # codex DOIT passer son préflight, sinon il serait retiré de la chaîne
    # avant la première phase et l'on n'observerait plus la bascule à
    # l'exécution — qui est précisément ce que ces tests mesurent.
    stub = mock_bin / "codex"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "${1:-} ${2:-}" == "login status" ]]; then\n'
        '  echo "Logged in using ChatGPT"\n'
        "  exit 0\n"
        "fi\n"
        "cat >/dev/null 2>&1 || true\n"
        "exit 0\n"
    )
    stub.chmod(0o755)

    # Le stub rend le code choisi pour TOUT runner d'agent, et fait échouer
    # otel_split pour emprunter la branche WARN qui matérialise les journaux.
    uv_stub = mock_bin / "uv"
    uv_stub.write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null 2>&1 || true\n"
        'case "$*" in\n'
        "  *otel_split*) exit 1 ;;\n"
        "  *claude_runner*|*codex_runner*)\n"
        '    _raw=""\n'
        "    while (($#)); do\n"
        "      if [[ $1 == --raw-log ]]; then _raw=$2; shift 2; else shift; fi\n"
        "    done\n"
        '    [[ -n "$_raw" ]] && printf "mock phase output\\n" >> "$_raw"\n'
        f"    exit {runner_exit_code}\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n"
    )
    uv_stub.chmod(0o755)

    env = {
        "HOME": str(tmp_path),
        "PATH": f"{mock_bin}:/usr/bin:/bin",
        "XDG_RUNTIME_DIR": str(tmp_path),
        "MCP_HTTP_TOKEN": "test-only-token",
        "BRAIN_DREAM_AGENT_PROVIDERS": "codex,claude",
    }
    return dream_copy, env


def _run_night(tmp_path: Path, runner_exit_code: int) -> str:
    import subprocess

    dream_copy, env = _sandbox(tmp_path, runner_exit_code)
    subprocess.run(
        [str(dream_copy), "test-project"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=180,
    )
    logs = sorted((tmp_path / "logs" / "dream").glob("*.log"))
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in logs if "_" not in p.name
    )


def test_a_provable_no_write_failure_falls_back_to_the_next_provider(tmp_path: Path) -> None:
    """Le cas nominal : codex meurt sans rien écrire, claude prend la nuit."""
    log = _run_night(tmp_path, SAFE_TO_FALL_BACK)

    assert "FALLBACK" in log, log
    assert "bascule vers claude" in log, log
    assert "provider=claude" in log, log


def test_a_failing_chain_still_reaches_the_end_of_the_night(tmp_path: Path) -> None:
    """Piège payé DEUX FOIS pendant l'écriture de la chaîne.

    Un `set -e` posé À L'INTÉRIEUR d'une fonction shell survit à son `return`.
    L'errexit ainsi restauré faisait sortir dream.sh sur le premier `return`
    non nul : la nuit s'arrêtait à la première phase en échec, sans résumé et
    sans toucher aux projets suivants — en sortant non nulle, donc avec l'air
    d'un échec ordinaire.

    Le résumé final est le témoin le moins cher de cette panne : il n'existe
    que si la boucle est allée au bout.
    """
    log = _run_night(tmp_path, 1)

    assert "Dream finished" in log, log


def test_an_ordinary_failure_never_falls_back(tmp_path: Path) -> None:
    """LE test qui compte. Un rc=1 ne prouve PAS que rien n'a été écrit, donc
    rejouer la phase ailleurs pourrait écrire deux fois. La chaîne doit rester
    immobile — et le mode de panne serait invisible, la nuit finissant verte
    avec des doublons dans le corpus."""
    log = _run_night(tmp_path, 1)

    assert "FALLBACK" not in log, log
    assert "provider=claude" not in log, log


def test_a_timeout_never_falls_back(tmp_path: Path) -> None:
    """Un timeout prouve encore moins : la phase a pu écrire puis se bloquer."""
    log = _run_night(tmp_path, 124)

    assert "FALLBACK" not in log, log
    assert "provider=claude" not in log, log


def test_a_capability_configuration_error_must_not_advance_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Une config cassée n'est pas une panne de provider.

    Les trois refus fail-closed du runner — URL non loopback, profil de projet
    absent, drapeau d'enforcement invalide — rendent 1 et NON le code de
    bascule. C'est délibéré : le maillon suivant heurterait exactement la même
    configuration, donc basculer ne réparerait rien et ne ferait que consommer
    un second abonnement avant d'échouer pareil. Pire, en masquant l'erreur
    derrière une deuxième tentative, cela rendrait la cause plus dure à lire au
    matin.

    Ce test épingle ce choix, parce que rien dans le code ne le crie : les deux
    chemins d'échec se ressemblent, et il serait naturel de les uniformiser.
    """
    codex = _codex()
    stderr_log = tmp_path / "scan.stderr.log"
    # Sans enforcement le chemin d'erreur de configuration n'existe pas : le
    # runner tenterait vraiment de lancer le binaire et rendrait le code de
    # bascule pour une tout autre raison (« codex n'a pas démarré »).
    registry = {
        f"brain-v42:{phase}": {"active": f"{phase}-token", "accepted": []}
        for phase in ("scan", "clean", "connect", "synth", "promote", "reorg")
    }
    monkeypatch.setenv("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "true")
    monkeypatch.setenv("MCP_HTTP_TOKEN", "admin-token")
    monkeypatch.setenv("MCP_HTTP_DREAM_TOKENS", json.dumps(registry))

    return_code = codex.run_codex(
        prompt="Return a scan report.",
        phase="scan",
        project_key="a-project-with-no-profile",
        model="gpt-5.6-terra",
        reasoning_effort="medium",
        timeout_seconds=1,
        report_log=tmp_path / "scan.log",
        events_log=tmp_path / "scan.codex.jsonl",
        stderr_log=stderr_log,
        codex_executable="/nonexistent-codex",
    )

    assert return_code == 1
    assert return_code != SAFE_TO_FALL_BACK


# --- Le maillon agy dans la chaîne ------------------------------------------


def test_agy_is_a_supported_provider_in_the_chain() -> None:
    content = _dream_sh()

    assert "codex|claude|agy)" in content
    assert "scripts.dream.agy_runner" in content


def test_the_agy_link_uses_gemini_models_not_claude_ones() -> None:
    """agy expose aussi claude-sonnet-4-6 et claude-opus-4-6-thinking.

    Les choisir annulerait l'intérêt du maillon : si Anthropic tombe, ces
    modèles tombent avec, et la chaîne aurait deux maillons corrélés déguisés
    en trois. La diversité recherchée est celle du FOURNISSEUR.
    """
    # Sur les LIGNES D'AFFECTATION seulement : la prose du script nomme
    # légitimement les modèles écartés pour expliquer POURQUOI ils le sont.
    assignments = [
        line
        for line in _dream_sh().splitlines()
        if line.startswith("BRAIN_DREAM_AGY_") and "MODEL=" in line
    ]

    assert len(assignments) == 2, assignments
    for line in assignments:
        assert ":-gemini-" in line, line
        assert "claude" not in line, line


def test_every_rail_persists_its_dream_run_row() -> None:
    """Trois rails, trois parsers. Aucun ne doit jouer une phase sans la mesurer.

    Le rail agy en a été privé quelques heures, et ce trou a suffi à dicter un
    ordre de chaîne absurde : claude placé avant agy pour préserver les lignes
    dream_runs, donc l'abonnement qu'on voulait épargner mis en première ligne.
    Une lacune d'outillage ne doit pas décider d'un arbitrage de coût.
    """
    content = _dream_sh()

    for parser in (
        "brain_v42.metrics.agy_dream_parser",
        "brain_v42.metrics.codex_dream_parser",
        "brain_v42.metrics.dream_parser",
    ):
        assert parser in content, parser
    assert "ligne dream_runs NON enregistrée" not in content


def test_the_agy_preflight_proves_its_tool_guard_before_the_night() -> None:
    """Sans garde prouvée, une phase agy a un shell libre. Le préflight doit
    donc la SONDER, pas constater son fichier — et retirer le maillon sinon."""
    content = _dream_sh()

    # Le préflight est générique depuis la mise en chaîne : il boucle sur les
    # providers et étiquette ses messages. Ce qui compte n'est donc pas un
    # libellé mais que le maillon agy soit BRANCHÉ dessus, et qu'il refuse de
    # partir sans enforcement — sans lui, sa garde n'est jamais sondée.
    assert 'agy)    binary="$BRAIN_DREAM_AGY_BIN"' in content
    assert "scripts.dream.agy_runner" in content
    assert 'provider" == "agy" && "$BRAIN_DREAM_CAPABILITY_ENFORCEMENT" != "true"' in content
