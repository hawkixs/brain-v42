"""Le rail agy — HOME éphémère, bearer scopé, garde câblée par invocation.

agy ne prend AUCUNE configuration en ligne de commande : ni `--mcp-config`, ni
allowlist d'outils. Sa doc embarquée (`docs/mcp_servers.md`) ne connaît que deux
emplacements, globaux tous les deux. Mesuré : un `.agents/hooks.json` au niveau
projet n'est pas découvert, même en workspace de confiance et dépôt git.

La seule voie qui donne un contrôle PAR INVOCATION est donc un HOME éphémère —
vérifié le 2026-08-11 : agy y trouve `.gemini/config/{mcp_config.json,hooks.json}`
et s'authentifie par les credentials liés depuis le vrai HOME.

Ce que ça évite compte autant que ce que ça permet : sans lui, la sécurité du
rail reposerait sur un fichier global hors dépôt, qu'une édition manuelle ou une
mise à jour d'agy pourrait retirer en silence — et deux phases concurrentes se
marcheraient dessus en réécrivant le même mcp_config.

LE SECRET SUR DISQUE, assumé et borné. L'`Authorization` d'agy est un LITTÉRAL :
sa doc ne documente aucune interpolation `${VAR}`, contrairement au .mcp.json du
dépôt. Le bearer de la phase est donc écrit dans un fichier, là où codex et
claude le passent par l'environnement. Il est confiné à un HOME en 0700 sous
XDG_RUNTIME_DIR — un tmpfs, donc jamais le disque persistant — et détruit avec
lui. C'est le seul écart du rail agy, il est nommé ici pour qu'il ne se
redécouvre pas par accident.
"""

from __future__ import annotations

import importlib
import json
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO_ROOT / "scripts" / "dream" / "agy_runner.py"
GUARD_PATH = REPO_ROOT / "scripts" / "dream" / "agy_tool_guard.sh"

PHASES = ("scan", "clean", "connect", "synth", "promote", "reorg")
ADMIN_TOKEN = "admin-token-never-scoped"


def _runner() -> ModuleType:
    assert RUNNER_PATH.is_file(), "attendu : scripts/dream/agy_runner.py"
    return importlib.import_module("scripts.dream.agy_runner")


def _registry(*, project_key: str = "brain-v42") -> str:
    return json.dumps(
        {
            f"{project_key}:{phase}": {"active": f"{phase}-active-token", "accepted": []}
            for phase in PHASES
        }
    )


def _enforced_environment(**overrides: str) -> dict[str, str]:
    environment = {
        "BRAIN_DREAM_CAPABILITY_ENFORCEMENT": "true",
        "MCP_HTTP_TOKEN": ADMIN_TOKEN,
        "MCP_HTTP_DREAM_TOKENS": _registry(),
        "HOME": "/home/hawixs",
        "PATH": "/usr/bin:/bin",
    }
    environment.update(overrides)
    return environment


# --- Le HOME éphémère -------------------------------------------------------


def test_the_ephemeral_home_carries_the_phase_scoped_bearer(tmp_path: Path) -> None:
    runner = _runner()

    home = runner.build_ephemeral_home(
        root=tmp_path,
        phase="scan",
        project_key="brain-v42",
        environ=_enforced_environment(),
        real_home=Path("/home/hawixs"),
    )

    config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())
    authorization = config["mcpServers"]["brain-v42"]["headers"]["Authorization"]

    assert authorization == "Bearer scan-active-token"
    assert ADMIN_TOKEN not in json.dumps(config)


def test_each_phase_gets_a_different_bearer(tmp_path: Path) -> None:
    runner = _runner()

    seen = set()
    for index, phase in enumerate(PHASES):
        home = runner.build_ephemeral_home(
            root=tmp_path / str(index),
            phase=phase,
            project_key="brain-v42",
            environ=_enforced_environment(),
            real_home=Path("/home/hawixs"),
        )
        config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())
        seen.add(config["mcpServers"]["brain-v42"]["headers"]["Authorization"])

    assert len(seen) == len(PHASES), "six phases, six bearers"


def test_the_mcp_config_declares_only_brain_v42_on_loopback(tmp_path: Path) -> None:
    """Le mcp_config du vrai HOME déclare aussi red-writer, sur une URL
    publique. Le copier tel quel donnerait à une phase de dream un accès qu'elle
    n'a aucune raison d'avoir."""
    runner = _runner()

    home = runner.build_ephemeral_home(
        root=tmp_path,
        phase="scan",
        project_key="brain-v42",
        environ=_enforced_environment(),
        real_home=Path("/home/hawixs"),
    )
    config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())

    assert set(config["mcpServers"]) == {"brain-v42"}
    assert config["mcpServers"]["brain-v42"]["serverUrl"].startswith("http://127.0.0.1:")


def test_the_agent_header_names_the_phase(tmp_path: Path) -> None:
    runner = _runner()

    for phase in PHASES:
        home = runner.build_ephemeral_home(
            root=tmp_path / phase,
            phase=phase,
            project_key="brain-v42",
            environ=_enforced_environment(),
            real_home=Path("/home/hawixs"),
        )
        config = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())

        assert config["mcpServers"]["brain-v42"]["headers"]["X-Brain-Agent"] == f"dream-agy-{phase}"


def test_the_secret_bearing_file_is_not_world_readable(tmp_path: Path) -> None:
    """Le seul écart du rail agy : le bearer est écrit, pas passé par l'env."""
    runner = _runner()

    home = runner.build_ephemeral_home(
        root=tmp_path,
        phase="scan",
        project_key="brain-v42",
        environ=_enforced_environment(),
        real_home=Path("/home/hawixs"),
    )
    config_path = home / ".gemini" / "config" / "mcp_config.json"

    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_the_hook_points_at_the_versioned_guard(tmp_path: Path) -> None:
    """La garde doit venir du DÉPÔT, pas d'une copie écrite à côté du secret.

    Une copie serait modifiable sans revue et divergerait de ses tests ; le
    chemin absolu vers le fichier versionné garde les deux ensemble.
    """
    runner = _runner()

    home = runner.build_ephemeral_home(
        root=tmp_path,
        phase="scan",
        project_key="brain-v42",
        environ=_enforced_environment(),
        real_home=Path("/home/hawixs"),
    )
    hooks = json.loads((home / ".gemini" / "config" / "hooks.json").read_text())
    entry = next(iter(hooks.values()))
    handler = entry["PreToolUse"][0]

    assert handler["matcher"] == "*", "la garde doit voir TOUS les outils"
    assert str(GUARD_PATH) in handler["hooks"][0]["command"]


def test_credentials_are_linked_not_copied(tmp_path: Path) -> None:
    """Dupliquer les jetons OAuth de l'utilisateur en ferait des copies à
    révoquer une par une. Un lien lit l'original et meurt avec le HOME."""
    runner = _runner()
    real_home = tmp_path / "real"
    (real_home / ".gemini" / "antigravity-cli").mkdir(parents=True)
    (real_home / ".gemini" / "oauth_creds.json").write_text("{}")
    (real_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token").write_text("t")

    home = runner.build_ephemeral_home(
        root=tmp_path / "eph",
        phase="scan",
        project_key="brain-v42",
        environ=_enforced_environment(),
        real_home=real_home,
    )

    linked = home / ".gemini" / "oauth_creds.json"
    assert linked.is_symlink(), "les credentials doivent être liés, jamais copiés"


def test_a_missing_project_profile_fails_closed(tmp_path: Path) -> None:
    runner = _runner()
    from brain_v42.mcp.dream_capabilities import DreamCapabilityConfigurationError

    with pytest.raises(DreamCapabilityConfigurationError):
        runner.build_ephemeral_home(
            root=tmp_path,
            phase="scan",
            project_key="un-projet-sans-profil",
            environ=_enforced_environment(),
            real_home=Path("/home/hawixs"),
        )


# --- Le prédicat de bascule -------------------------------------------------


def _stream_event(**fields: object) -> str:
    return json.dumps(fields) + "\n"


def test_a_completed_mcp_step_is_read_as_a_brain_tool_call(tmp_path: Path) -> None:
    runner = _runner()
    events = tmp_path / "events.jsonl"
    events.write_text(
        _stream_event(event="init", init={"tools": []})
        + _stream_event(
            event="step_update",
            step_update={
                "step_index": 3,
                "step_type": "tool",
                "state": "DONE",
                "tool_name": "call_mcp_tool",
            },
        )
    )

    assert runner.brain_tool_call_completed(events) is True


def test_a_run_without_any_completed_mcp_step_is_safe_to_fall_back(tmp_path: Path) -> None:
    runner = _runner()
    events = tmp_path / "events.jsonl"
    events.write_text(
        _stream_event(event="init", init={"tools": []})
        + _stream_event(
            event="step_update",
            step_update={"step_index": 1, "step_type": "agent_response", "state": "DONE"},
        )
    )

    assert runner.brain_tool_call_completed(events) is False


def test_an_mcp_step_that_errored_did_not_write(tmp_path: Path) -> None:
    runner = _runner()
    events = tmp_path / "events.jsonl"
    events.write_text(
        _stream_event(
            event="step_update",
            step_update={
                "step_index": 3,
                "step_type": "tool",
                "state": "ERROR",
                "tool_name": "call_mcp_tool",
            },
        )
    )

    assert runner.brain_tool_call_completed(events) is False


def test_a_denied_non_mcp_tool_never_counts_as_a_brain_call(tmp_path: Path) -> None:
    """Un run_command refusé par la garde a bien produit une étape d'outil.
    La compter bloquerait la bascule sur une phase qui n'a rien écrit."""
    runner = _runner()
    events = tmp_path / "events.jsonl"
    events.write_text(
        _stream_event(
            event="step_update",
            step_update={
                "step_index": 2,
                "step_type": "tool",
                "state": "DONE",
                "tool_name": "run_command",
            },
        )
    )

    assert runner.brain_tool_call_completed(events) is False


def test_a_missing_event_stream_does_not_block_the_fallback(tmp_path: Path) -> None:
    runner = _runner()

    assert runner.brain_tool_call_completed(tmp_path / "absent.jsonl") is False


# --- Le préflight, qui PROUVE que la garde refuse ---------------------------


def test_the_preflight_proves_the_guard_denies_a_shell_tool() -> None:
    """Le préflight n'a pas le droit de se contenter de constater le fichier.

    La garde est le seul rempart entre une phase nocturne et un shell. Vérifier
    sa PRÉSENCE laisserait passer une garde vide, mal nommée, non exécutable ou
    rendue permissive par une édition — tous des états où le fichier existe.
    """
    runner = _runner()

    assert runner.guard_denies_machine_tools() is True


def test_the_preflight_fails_closed_when_the_guard_is_unusable(tmp_path: Path) -> None:
    runner = _runner()
    broken = tmp_path / "broken_guard.sh"
    broken.write_text('#!/usr/bin/env bash\nprintf \'{"decision":"allow"}\'\n')
    broken.chmod(0o755)

    assert runner.guard_denies_machine_tools(guard=broken) is False
    assert runner.guard_denies_machine_tools(guard=tmp_path / "absent.sh") is False


def test_the_runner_exposes_a_project_scoped_api() -> None:
    import inspect

    parameters = inspect.signature(_runner().run_agy).parameters
    assert "project_key" in parameters
    assert "phase" in parameters


def test_the_ephemeral_home_defaults_to_a_tmpfs_runtime_dir() -> None:
    """Le bearer est écrit sur disque : il ne doit pas atterrir sur du
    persistant. XDG_RUNTIME_DIR est un tmpfs."""
    runner = _runner()

    root = runner.ephemeral_root({"XDG_RUNTIME_DIR": "/run/user/1001"})
    assert str(root).startswith("/run/user/1001")

    fallback = runner.ephemeral_root({})
    assert fallback is None or str(fallback).startswith(os.environ.get("TMPDIR", "/tmp"))


# --- Le prompt : argv, pas stdin --------------------------------------------


def test_the_prompt_travels_as_the_print_argument_not_on_stdin() -> None:
    """MESURÉ le 2026-08-11 : agy ignore stdin, dans les deux formes.

    `--print ""` avec le prompt sur stdin rend une réponse vide, et un prompt
    en argument PLUS un contexte sur stdin répond sans le contexte. Les deux
    autres rails passent délibérément par stdin pour éviter ARG_MAX ; agy ne
    laisse pas le choix.

    Le mode de panne si on se trompe est traître : agy répond quand même, par
    une salutation, et la phase sort en 0 avec un rapport hors sujet.
    """
    runner = _runner()

    command = runner.build_agy_command(model="gemini-3.6-flash-medium", prompt="AUDIT SCAN")

    assert "AUDIT SCAN" in command
    assert command[command.index("--print") + 1] == "AUDIT SCAN"
    assert "-" not in command[command.index("--print") + 1]


def test_an_oversized_prompt_is_refused_with_a_readable_reason() -> None:
    """Un argument dépasse 128 Kio -> E2BIG, une OSError opaque au fond d'un
    Popen. Le refuser AVANT, avec sa taille, rend la cause lisible au matin."""
    runner = _runner()

    with pytest.raises(ValueError, match="trop long"):
        runner.build_agy_command(model="gemini-3.6-flash-medium", prompt="x" * 200_000)


def test_no_secret_travels_through_argv() -> None:
    """Le prompt est en argv, donc visible dans `ps`. Le BEARER, lui, ne doit
    jamais l'être : il vit dans le mcp_config du HOME éphémère."""
    runner = _runner()

    command = runner.build_agy_command(model="gemini-3.6-flash-medium", prompt="AUDIT")

    assert not any("Bearer" in argument for argument in command)
    assert not any("token" in argument.lower() for argument in command)


# --- Le rapport de phase ----------------------------------------------------


def test_the_report_is_extracted_from_the_result_event(tmp_path: Path) -> None:
    """dream.sh injecte ce rapport dans la phase suivante et le donne à ses
    validateurs. Le chercher au mauvais endroit produit un rapport VIDE et une
    chaîne de dépendances cassée — sans erreur, la phase sortant en 0."""
    runner = _runner()
    events = tmp_path / "events.jsonl"
    report = tmp_path / "report.log"
    events.write_text(
        json.dumps({"event": "init", "init": {"tools": []}})
        + "\n"
        + json.dumps(
            {"event": "result", "result": {"status": "SUCCESS", "response": "=== SCAN REPORT ==="}}
        )
        + "\n"
    )

    runner.extract_report(events, report)

    assert report.read_text().strip() == "=== SCAN REPORT ==="
