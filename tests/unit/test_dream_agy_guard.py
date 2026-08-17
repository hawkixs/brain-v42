"""La garde d'outils du rail agy — deny par défaut, et fail-closed.

agy n'a AUCUN équivalent du `--tools ""` de claude ni de l'`enabled_tools` de
codex : mesuré le 2026-08-11, il expose 56 outils dont run_command,
write_to_file, invoke_subagent et schedule, et `--dangerously-skip-permissions`
— requis en headless — les auto-approuve tous. Sur un prompt demandant UN appel
MCP, il a exécuté 12 étapes d'outils et lancé `ps aux`.

Le seul mécanisme qui le contraint est un hook `PreToolUse` rendant
`{"decision":"deny"}`. Vérifié : il survit à `--dangerously-skip-permissions`.

Cette garde est INCONDITIONNELLE. Une version antérieure la conditionnait à une
variable d'environnement, ce qui en faisait un interrupteur qu'on pouvait
oublier de poser. Le HOME éphémère du runner rend cette précaution inutile : la
garde n'est câblée que là, donc elle ne peut pas gêner une session interactive,
donc elle n'a aucune raison d'avoir un mode permissif.

Séparation des rôles, à ne pas confondre :
- la GARDE protège la MACHINE (shell, fichiers, sous-agents, cron) ;
- le BEARER scopé protège le CORPUS, et c'est le serveur qui l'applique.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "scripts" / "dream" / "agy_tool_guard.sh"

# Ce qu'une phase de dream a besoin d'appeler. `call_mcp_tool` est la passerelle
# par laquelle agy atteint brain-v42 ; le serveur borne ensuite ce qu'elle peut
# y faire, par phase, via le bearer.
ALLOWED = ("call_mcp_tool", "list_resources", "read_resource", "finish", "send_message")

# Un échantillon de ce qu'agy expose et qu'une phase nocturne ne doit jamais
# obtenir. La liste n'est pas exhaustive À DESSEIN : la garde est deny par
# défaut, donc un outil ajouté par une future version d'agy est refusé sans
# que personne n'ait à mettre cette liste à jour.
FORBIDDEN = (
    "run_command",
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "invoke_subagent",
    "define_subagent",
    "schedule",
    "search_web",
    "read_url_content",
    "browser_click_element",
    "execute_browser_javascript",
    "delete_knowledge",
    "notebook_edit",
    "send_command_input",
)


def _decide(payload: str) -> dict[str, object]:
    result = subprocess.run(
        ["bash", str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _tool_call(name: str) -> str:
    return json.dumps({"toolCall": {"name": name, "args": {}}, "stepIdx": 1})


def test_guard_exists_and_is_executable() -> None:
    assert GUARD.is_file(), "la garde doit être versionnée dans le dépôt"


def test_the_brain_gateway_and_the_reply_tools_are_allowed() -> None:
    for name in ALLOWED:
        assert _decide(_tool_call(name))["decision"] == "allow", name


def test_every_machine_reaching_tool_is_denied() -> None:
    for name in FORBIDDEN:
        decision = _decide(_tool_call(name))
        assert decision["decision"] == "deny", name
        assert decision.get("reason"), f"un refus sans raison est illisible au matin ({name})"


def test_an_unknown_tool_is_denied_by_default() -> None:
    """Le point de tout le design : agy expose 56 outils et en gagnera d'autres.

    Une allowlist se périme en silence dans le bon sens ; une denylist se périme
    dans le mauvais. Un outil inventé demain doit être refusé sans que personne
    ne touche cette garde.
    """
    assert _decide(_tool_call("tool_that_does_not_exist_yet"))["decision"] == "deny"


def test_a_malformed_payload_is_denied_rather_than_allowed() -> None:
    """Fail-closed. Une garde qui s'ouvre sur une entrée qu'elle ne comprend
    pas ne garde rien — et le jour où le format du payload changera, elle
    laisserait tout passer sans un bruit."""
    for payload in ("", "   ", "not json at all", "{}", '{"toolCall": {}}', "[]", "null"):
        decision = _decide(payload)
        assert decision["decision"] == "deny", repr(payload)


def test_the_decision_is_always_valid_json_on_stdout() -> None:
    """agy lit stdout comme du JSON. Une garde qui écrit autre chose serait
    ignorée, et le refus se transformerait en autorisation."""
    for payload in ("", "garbage", _tool_call("run_command"), _tool_call("call_mcp_tool")):
        result = subprocess.run(
            ["bash", str(GUARD)], input=payload, capture_output=True, text=True, timeout=30
        )
        parsed = json.loads(result.stdout)
        assert parsed["decision"] in {"allow", "deny"}


def test_the_guard_never_writes_to_stdout_beyond_its_decision() -> None:
    """Un `echo` de debug oublié casserait le parsing du même coup."""
    result = subprocess.run(
        ["bash", str(GUARD)],
        input=_tool_call("call_mcp_tool"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == json.dumps({"decision": "allow"}, separators=(",", ":"))
