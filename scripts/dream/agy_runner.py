"""Adaptateur ``agy`` isolé pour une phase de Dream.

agy ne prend AUCUNE configuration en ligne de commande : ni ``--mcp-config``, ni
allowlist d'outils, ni équivalent du ``--tools ""`` de claude. Sa doc embarquée
(``docs/mcp_servers.md``) ne connaît que deux emplacements, globaux tous les
deux, et un ``.agents/hooks.json`` au niveau projet n'est PAS découvert — mesuré
le 2026-08-11, en workspace de confiance et dépôt git.

D'où le HOME éphémère : c'est la seule voie qui donne un contrôle par
invocation. agy y trouve ``.gemini/config/{mcp_config.json,hooks.json}`` et
s'authentifie par les credentials liés depuis le vrai HOME.

Ce qu'il évite compte autant que ce qu'il permet. Sans lui, la sécurité du rail
reposerait sur un fichier global hors dépôt, qu'une édition manuelle ou une mise
à jour d'agy retirerait en silence — et deux phases concurrentes se
marcheraient dessus en réécrivant le même ``mcp_config.json``.

DEUX PROTECTIONS, DEUX PÉRIMÈTRES, à ne pas confondre :
- ``agy_tool_guard.sh``, câblé en hook ``PreToolUse``, protège la MACHINE ;
- le bearer de ``(projet, phase)`` protège le CORPUS, et c'est le serveur qui
  l'applique.

LE SEUL ÉCART DU RAIL. L'``Authorization`` d'agy est un littéral : sa doc ne
documente aucune interpolation ``${VAR}``, contrairement au ``.mcp.json`` que
lit claude. Le bearer est donc ÉCRIT dans un fichier là où les deux autres rails
le passent par l'environnement. Il est confiné à un HOME en 0700 sous
``XDG_RUNTIME_DIR`` — un tmpfs, jamais le disque persistant — et détruit avec
lui. Nommé ici pour qu'il ne se redécouvre pas par accident.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from brain_v42.mcp.dream_capabilities import (
    DREAM_PHASE_TOOL_ALLOWLISTS,
    DreamCapabilityConfigurationError,
    dream_phase_tool_allowlist,
)
from scripts.dream._agent_capability import (
    CAPABILITY_CONFIGURATION_ERROR,
    DEFAULT_MCP_URL,
    MCP_URL_ENV,
    PROVIDER_FALLBACK_EXIT_CODE,
    TIMEOUT_EXIT_CODE,
    active_capability_token,
    capability_enforcement_enabled,
    preflight_capabilities,
    terminate_process_group,
    validate_loopback_mcp_url,
)

PHASE_TOOL_ALLOWLISTS = DREAM_PHASE_TOOL_ALLOWLISTS

GUARD_PATH = Path(__file__).resolve().parent / "agy_tool_guard.sh"

# Fichiers d'identité lus depuis le vrai HOME. LIÉS, jamais copiés : dupliquer
# les jetons OAuth d'un humain en ferait des copies à révoquer une par une.
_CREDENTIAL_PATHS = (
    ".gemini/oauth_creds.json",
    ".gemini/google_accounts.json",
    ".gemini/gemini-credentials.json",
    ".gemini/antigravity-cli/antigravity-oauth-token",
)


def ephemeral_root(environ: Mapping[str, str]) -> Path | None:
    """Racine des HOME éphémères — tmpfs de préférence.

    Le bearer y est écrit : il ne doit pas atterrir sur du disque persistant.
    ``None`` signifie « pas de tmpfs disponible », et laisse l'appelant
    retomber sur ``tempfile`` plutôt que d'inventer un chemin.
    """
    runtime_dir = environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and Path(runtime_dir).is_dir():
        return Path(runtime_dir)
    return None


def build_ephemeral_home(
    *,
    root: Path,
    phase: str,
    project_key: str,
    environ: Mapping[str, str],
    real_home: Path,
    mcp_url: str | None = None,
) -> Path:
    """Composer le HOME d'une phase : bearer scopé, garde câblée, rien d'autre."""
    dream_phase_tool_allowlist(phase)
    validate_loopback_mcp_url(environ)
    token = active_capability_token(project_key=project_key, phase=phase, environ=environ)

    home = root / f"agy-{project_key.replace(':', '-')}-{phase}"
    config_dir = home / ".gemini" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (home / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)

    # Le mcp_config du vrai HOME déclare aussi red-writer, sur une URL publique.
    # On ne le copie pas : on en écrit un qui ne connaît que brain-v42.
    server_url = mcp_url or environ.get(MCP_URL_ENV, DEFAULT_MCP_URL)
    mcp_config = {
        "mcpServers": {
            "brain-v42": {
                "serverUrl": server_url,
                "headers": {
                    "Authorization": f"Bearer {token}",
                    "X-Brain-Agent": f"dream-agy-{phase}",
                    "X-Brain-Tool-Profile": "native",
                },
                "trust": True,
            }
        }
    }
    config_path = config_dir / "mcp_config.json"
    config_path.write_text(json.dumps(mcp_config), encoding="utf-8")
    config_path.chmod(0o600)

    # La garde vient du DÉPÔT. Une copie posée à côté du secret serait
    # modifiable sans revue et divergerait de ses tests.
    hooks = {
        "dream-phase-guard": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": str(GUARD_PATH), "timeout": 10}],
                }
            ]
        }
    }
    (config_dir / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")

    # Le workspace de confiance doit être le HOME éphémère lui-même : sans lui,
    # agy refuse de charger ses customisations.
    (home / ".gemini" / "antigravity-cli" / "settings.json").write_text(
        json.dumps({"enableTelemetry": False, "trustedWorkspaces": [str(home)]}),
        encoding="utf-8",
    )

    for relative in _CREDENTIAL_PATHS:
        source = real_home / relative
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.exists() and not target.exists():
            target.symlink_to(source)

    return home


# Limite noyau d'un SEUL argument (MAX_ARG_STRLEN = 32 pages). Au-delà, execve
# rend E2BIG. On garde une marge pour le reste de la ligne de commande.
_MAX_PROMPT_BYTES = 120_000


def build_agy_command(
    *,
    model: str,
    prompt: str,
    agy_executable: str = "agy",
    timeout_seconds: float = 300.0,
) -> list[str]:
    """Ligne de commande headless d'une phase.

    LE PROMPT PASSE EN ARGV, et ce n'est pas un choix. Mesuré le 2026-08-11 :
    agy IGNORE stdin — ``--print ""`` avec le prompt sur stdin rend une réponse
    vide, et un prompt en argument plus un contexte sur stdin répond sans le
    contexte. Les deux autres rails passent délibérément par stdin pour éviter
    ARG_MAX ; agy ne laisse pas le choix.

    Le mode de panne si l'on se trompe est traître : agy répond quand même, par
    une salutation, et la phase sort en 0 avec un rapport hors sujet.

    Aucun secret ne transite par argv : le bearer vit dans le ``mcp_config.json``
    du HOME éphémère. Le prompt, lui, y est visible — c'est de la consigne de
    phase et des rapports précédents, pas un secret.

    ``--dangerously-skip-permissions`` est REQUIS : sans lui, agy attend en
    headless une approbation qui ne viendra jamais. Ce n'est pas ce qui borne
    la phase — c'est la garde ``PreToolUse``, qui survit à ce drapeau.
    """
    prompt_bytes = len(prompt.encode("utf-8"))
    if prompt_bytes > _MAX_PROMPT_BYTES:
        # Refuser AVANT execve : un E2BIG au fond d'un Popen est une OSError
        # opaque, là où ceci nomme la cause et sa taille.
        raise ValueError(
            f"prompt trop long pour argv : {prompt_bytes} octets > {_MAX_PROMPT_BYTES}"
        )
    command = [
        agy_executable,
        "--print",
        prompt,
        "--output-format",
        "stream-json",
        "--print-timeout",
        f"{int(timeout_seconds)}s",
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
    ]
    if model.strip():
        command.extend(("--model", model))
    return command


def brain_tool_call_completed(events_log: Path) -> bool:
    """Un appel d'outil Brain a-t-il ABOUTI dans ce flux stream-json ?

    Pendant des prédicats de codex et claude. ``False`` prouve qu'aucune
    mutation n'a été commitée, donc que rejouer la phase ailleurs est sans
    risque.

    Seul ``call_mcp_tool`` compte : c'est la passerelle par laquelle agy atteint
    brain-v42, et la garde refuse tout le reste. Un ``run_command`` REFUSÉ
    produit bien une étape d'outil — la compter bloquerait la bascule sur une
    phase qui n'a manifestement rien écrit.
    """
    if not events_log.is_file():
        return False
    for raw_line in events_log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        step = event.get("step_update")
        if not isinstance(step, dict):
            continue
        if (
            step.get("step_type") == "tool"
            and step.get("state") == "DONE"
            and step.get("tool_name") == "call_mcp_tool"
        ):
            return True
    return False


def guard_denies_machine_tools(guard: Path | None = None) -> bool:
    """PROUVER que la garde refuse, au lieu de constater qu'elle existe.

    La garde est le seul rempart entre une phase nocturne et un shell. Vérifier
    sa présence laisserait passer une garde vide, non exécutable, mal nommée ou
    rendue permissive par une édition — tous des états où le fichier existe.
    On lui soumet donc un vrai payload et on exige le refus.
    """
    guard_path = guard or GUARD_PATH
    if not guard_path.is_file():
        return False
    probes = (
        ("run_command", "deny"),
        ("write_to_file", "deny"),
        ("call_mcp_tool", "allow"),
    )
    for tool_name, expected in probes:
        payload = json.dumps({"toolCall": {"name": tool_name, "args": {}}, "stepIdx": 0})
        try:
            result = subprocess.run(
                ["bash", str(guard_path)],
                input=payload,
                capture_output=True,
                text=True,
                timeout=30,
            )
            decision = json.loads(result.stdout).get("decision")
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        if decision != expected:
            return False
    return True


def run_agy(
    *,
    prompt: str,
    phase: str,
    project_key: str,
    model: str,
    timeout_seconds: float,
    events_log: Path,
    report_log: Path,
    stderr_log: Path,
    agy_executable: str = "agy",
) -> int:
    """Jouer une phase et rendre son code (``124`` sur délai, ``3`` si rejouable)."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    for path in (events_log, report_log, stderr_log):
        path.parent.mkdir(parents=True, exist_ok=True)
    report_log.write_text("", encoding="utf-8")

    # Fail-closed AVANT de lancer quoi que ce soit : sans garde prouvée, une
    # phase agy aurait un shell libre. Refuser de démarrer est le seul choix
    # sûr, et il est journalisé.
    if not guard_denies_machine_tools():
        stderr_log.write_text(
            "garde d'outils agy absente ou permissive — phase refusée\n", encoding="utf-8"
        )
        return 1

    if not capability_enforcement_enabled(os.environ):
        stderr_log.write_text(
            "le rail agy exige BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true\n", encoding="utf-8"
        )
        return 1

    real_home = Path(os.environ.get("HOME", str(Path.home())))
    root = ephemeral_root(os.environ)

    def _run(base: Path) -> int:
        try:
            home = build_ephemeral_home(
                root=base,
                phase=phase,
                project_key=project_key,
                environ=os.environ,
                real_home=real_home,
            )
        except DreamCapabilityConfigurationError:
            stderr_log.write_text(f"{CAPABILITY_CONFIGURATION_ERROR}\n", encoding="utf-8")
            return 1

        child_environment = {
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "TERM": "dumb",
        }
        try:
            command = build_agy_command(
                model=model,
                prompt=prompt,
                agy_executable=agy_executable,
                timeout_seconds=timeout_seconds,
            )
        except ValueError as exc:
            stderr_log.write_text(f"{exc}\n", encoding="utf-8")
            return PROVIDER_FALLBACK_EXIT_CODE

        with (
            events_log.open("w", encoding="utf-8") as events_stream,
            stderr_log.open("w", encoding="utf-8") as stderr_stream,
        ):
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=events_stream,
                    stderr=stderr_stream,
                    cwd=home,
                    env=child_environment,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                stderr_stream.write(f"impossible de démarrer agy : {exc}\n")
                return PROVIDER_FALLBACK_EXIT_CODE
            try:
                process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                terminate_process_group(process)
                return TIMEOUT_EXIT_CODE

        extract_report(events_log, report_log)
        exit_code = int(process.returncode or 0)
        if exit_code == 0:
            return 0
        if exit_code == TIMEOUT_EXIT_CODE:
            return TIMEOUT_EXIT_CODE
        if brain_tool_call_completed(events_log):
            return exit_code
        return PROVIDER_FALLBACK_EXIT_CODE

    if root is not None:
        with tempfile.TemporaryDirectory(prefix="brain-v42-dream-", dir=str(root)) as temporary:
            return _run(Path(temporary))
    with tempfile.TemporaryDirectory(prefix="brain-v42-dream-") as temporary:
        return _run(Path(temporary))


def extract_report(events_log: Path, report_log: Path) -> None:
    """Reconstituer le rapport de phase depuis le flux d'événements.

    La réponse finale vit sous ``{"event":"result","result":{"response":...}}``.
    La chercher ailleurs produit un rapport VIDE, et dream.sh injecte ce fichier
    dans la phase suivante puis le donne à ses validateurs : la chaîne de
    dépendances se casserait sans erreur, la phase sortant en 0.
    """
    if not events_log.is_file():
        return
    response = ""
    for raw_line in events_log.read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("event") != "result":
            continue
        result = event.get("result")
        if isinstance(result, dict) and isinstance(result.get("response"), str):
            response = result["response"]
    if response.strip():
        report_log.write_text(response, encoding="utf-8")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jouer une phase de Dream avec agy")
    parser.add_argument("--preflight-capabilities", action="store_true")
    parser.add_argument("--project-key")
    parser.add_argument("--phase", choices=tuple(PHASE_TOOL_ALLOWLISTS))
    parser.add_argument("--model", default="")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--events-log", type=Path)
    parser.add_argument("--report-log", type=Path)
    parser.add_argument("--stderr-log", type=Path)
    parser.add_argument("--agy-executable", default=os.environ.get("BRAIN_DREAM_AGY_BIN", "agy"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.preflight_capabilities:
        if args.project_key is None:
            parser.error("--project-key est requis avec --preflight-capabilities")
        if not guard_denies_machine_tools():
            print("garde d'outils agy absente ou permissive", file=sys.stderr)
            return 1
        try:
            preflight_capabilities(args.project_key, os.environ)
        except DreamCapabilityConfigurationError:
            print(CAPABILITY_CONFIGURATION_ERROR, file=sys.stderr)
            return 1
        return 0

    required = {
        "--phase": args.phase,
        "--project-key": args.project_key,
        "--timeout-seconds": args.timeout_seconds,
        "--events-log": args.events_log,
        "--report-log": args.report_log,
        "--stderr-log": args.stderr_log,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        parser.error(f"arguments requis manquants : {', '.join(missing)}")

    prompt = sys.stdin.read()
    if not prompt.strip():
        print("prompt de phase agy vide", file=sys.stderr)
        return 1
    return run_agy(
        prompt=prompt,
        phase=args.phase,
        project_key=args.project_key,
        model=args.model,
        timeout_seconds=args.timeout_seconds,
        events_log=args.events_log,
        report_log=args.report_log,
        stderr_log=args.stderr_log,
        agy_executable=args.agy_executable,
    )


if __name__ == "__main__":
    raise SystemExit(main())
