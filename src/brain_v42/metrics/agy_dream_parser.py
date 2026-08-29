"""Normaliser le flux ``agy --output-format stream-json`` et persister une phase.

Troisième parser du dream, après ``dream_parser`` (claude, OTEL) et
``codex_dream_parser`` (codex, JSONL). Sans lui, une phase jouée par agy était
jouée mais NON MESURÉE — et ce trou a dicté quelques heures durant un ordre de
chaîne absurde, claude placé avant agy pour préserver les lignes ``dream_runs``,
donc l'abonnement qu'on voulait épargner mis en première ligne.

LA CONVENTION DE CACHE EST L'INVERSE DE CELLE DE CODEX. Mesuré sur un vrai run :

    input_tokens      = 20137
    cache_read_tokens = 56950      <-- PLUS GRAND que input_tokens
    total_tokens      = 21691      = input + output, le cache n'y est PAS

Chez codex, ``cached_input_tokens`` est un SOUS-ENSEMBLE de ``input_tokens``, et
son parser calcule ``frais = input - cached``. Chez agy les deux compteurs sont
INDÉPENDANTS : ``input_tokens`` est déjà le frais. Copier la formule de codex
produirait ici un nombre NÉGATIF, et une colonne de tokens n'est pas regardée
d'assez près pour que quiconque s'en aperçoive avant des semaines.

L'ÉVÉNEMENT ``result`` EST L'AGRÉGAT AUTORITATIF. Mesuré aussi : la somme des
usages ``step_update`` égale exactement celui du ``result``. Additionner les deux
doublerait tous les compteurs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Iterable
from pathlib import Path

from brain_v42.metrics.dream_parser import PhaseTelemetry, _insert_dream_run, _str_to_bool


def _events(content: str) -> Iterable[dict[str, object]]:
    """Rendre les événements JSON, en ignorant lignes vides, bruit et inconnues."""
    for raw_line in content.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _counter(usage: dict[str, object], key: str) -> int:
    """Lire un compteur, en refusant ce qui ne peut pas être un compte."""
    value = usage.get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"usage.{key} n'est pas un nombre")
    if value < 0:
        raise ValueError(f"usage.{key} est négatif")
    return int(value)


def parse_agy_stream(content: str) -> PhaseTelemetry:
    """Mapper le flux stream-json d'agy sur le schéma historique du dream."""
    telemetry = PhaseTelemetry(
        # Adossé à un abonnement : ni coût, ni nombre d'appels API, ni tokens
        # de création de cache ne sont exposés. Écrire 0 affirmerait « gratuit »
        # et « aucun appel » — deux mensonges. NULL dit « non mesuré ».
        cache_creation_tokens=None,
        cost_usd=None,
        api_calls=None,
    )
    result_usage: dict[str, object] | None = None

    for event in _events(content):
        kind = event.get("event")
        if kind == "result":
            result = event.get("result")
            if not isinstance(result, dict):
                raise ValueError("événement result sans objet result")
            usage = result.get("usage")
            if not isinstance(usage, dict):
                raise ValueError("événement result sans objet usage")
            result_usage = usage
        elif kind == "step_update":
            step = event.get("step_update")
            if not isinstance(step, dict):
                continue
            # Seuls les appels MCP ABOUTIS comptent. Un run_command refusé par
            # la garde produit bien une étape d'outil : la compter ferait croire
            # qu'une phase a touché le brain alors qu'elle en a été empêchée.
            if (
                step.get("step_type") == "tool"
                and step.get("state") == "DONE"
                and step.get("tool_name") == "call_mcp_tool"
            ):
                telemetry.tool_calls += 1

    if result_usage is None:
        raise ValueError("le flux ne porte aucun événement result exploitable")

    # Pas de soustraction, contrairement à codex — voir l'en-tête du module.
    telemetry.input_tokens = _counter(result_usage, "input_tokens")
    telemetry.output_tokens = _counter(result_usage, "output_tokens")
    telemetry.cache_read_tokens = _counter(result_usage, "cache_read_tokens")
    # 049 : mesuré SÉPARÉMENT, jamais additionné à output_tokens — les rails
    # qui ne distinguent pas le thinking laisseraient une somme incomparable.
    # Absent du flux = NULL (« pas mesuré »), pas 0 (« mesuré nul »).
    if "thinking_tokens" in result_usage:
        telemetry.thinking_tokens = _counter(result_usage, "thinking_tokens")
    return telemetry


#: Préfixe exact posé par agy quand c'est un APPEL D'OUTIL qui a échoué, et non
#: agy lui-même. C'est la seule famille d'échec qu'un retry peut rattraper.
_TOOL_ERROR_PREFIX = "Error in MCP tool execution"


def _failure_messages(content: str) -> list[str]:
    """Tous les messages d'échec portés par le flux, dans l'ordre du flux."""
    messages: list[str] = []
    for event in _events(content):
        result = event.get("result")
        if isinstance(result, dict) and result.get("status") not in (None, "SUCCESS"):
            message = result.get("error") or result.get("status")
            if isinstance(message, str) and message.strip():
                messages.append(message.strip())
        if event.get("event") == "error":
            message = event.get("message")
            if isinstance(message, str) and message.strip():
                messages.append(message.strip())
    return messages


def _terminal_error(content: str) -> str | None:
    """Le dernier message d'échec porté par le flux, s'il y en a un."""
    messages = _failure_messages(content)
    return messages[-1] if messages else None


def _last_failed_call_was_retried(content: str) -> bool:
    """Le dernier appel MCP en échec a-t-il été REJOUÉ avec succès ?

    Le nom de l'outil vit dans ``tool_info.parameters.ToolName`` : ``tool_name``
    vaut toujours ``call_mcp_tool`` et ne distingue rien. Exiger le MÊME outil
    n'est pas du zèle — mesuré sur la nuit du 2026-08-12, se contenter de
    « un appel quelconque a réussi ensuite » blanchissait aussi refondrre/connect,
    dont le ``brain_assign_domain`` perdu n'a jamais été retenté.

    Sans indice d'étape, « après » n'a pas de sens : on répond non, fail-closed.
    """
    last_failed_index = -1
    last_failed_tool: str | None = None
    successes: list[tuple[int, str | None]] = []

    for event in _events(content):
        if event.get("event") != "step_update":
            continue
        step = event.get("step_update")
        if not isinstance(step, dict):
            continue
        if step.get("step_type") != "tool" or step.get("tool_name") != "call_mcp_tool":
            continue
        index = step.get("step_index")
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        info = step.get("tool_info")
        parameters = info.get("parameters") if isinstance(info, dict) else None
        tool = parameters.get("ToolName") if isinstance(parameters, dict) else None
        tool_name = tool if isinstance(tool, str) else None

        if step.get("state") == "ERROR" and index > last_failed_index:
            last_failed_index, last_failed_tool = index, tool_name
        elif step.get("state") == "DONE":
            successes.append((index, tool_name))

    if last_failed_index < 0 or last_failed_tool is None:
        return False
    return any(index > last_failed_index and tool == last_failed_tool for index, tool in successes)


def _unrecovered_error(content: str) -> str | None:
    """Le message d'échec qui a réellement emporté la phase, s'il y en a un.

    agy LATCHE ``result.status=ERROR`` dès qu'un appel d'outil a échoué, même
    quand l'agent a retenté et réussi ensuite. Mesuré sur la nuit du 2026-08-12 :
    19 phases sur 21 étaient comptées ``fail`` en ayant produit 27 artefacts
    durables, tous vérifiés en base. Un rouge qui ne porte sur rien coûte aussi
    cher qu'un vert qui ne porte sur rien — il fait chercher une panne absente.

    Seul un échec d'OUTIL est rattrapable. Une panne d'agy lui-même reste
    terminale, et c'est pourquoi on retombe sur le message suivant plutôt que de
    rendre ``None`` : supprimer l'échec d'outil ne doit jamais masquer ce qui se
    trouvait derrière lui.
    """
    messages = _failure_messages(content)
    if not messages:
        return None
    if _last_failed_call_was_retried(content):
        messages = [message for message in messages if not message.startswith(_TOOL_ERROR_PREFIX)]
    return messages[-1] if messages else None


def _error_tail(*contents: str, max_chars: int = 2000) -> str | None:
    text = "\n".join(content.strip() for content in contents if content.strip()).strip()
    if not text:
        return None
    return text[-max_chars:]


def _read(path: str | None) -> str:
    if not path or not os.path.isfile(path):
        return ""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persister la télémétrie d'une phase agy")
    parser.add_argument("events_file", help="flux stream-json d'agy")
    parser.add_argument("--phase", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--raw-log", default=None, help="stderr de la phase")
    parser.add_argument("--report-log", default=None, help="rapport final de la phase")
    parser.add_argument("--phase-dry-run", type=_str_to_bool, default=False)
    return parser


def main() -> None:
    args = _build_arg_parser().parse_args()
    events_content = _read(args.events_file)
    raw_content = _read(args.raw_log)
    report_content = _read(args.report_log)

    telemetry: PhaseTelemetry | None
    telemetry_error: str | None = None
    try:
        telemetry = parse_agy_stream(events_content)
    except ValueError as exc:
        telemetry = None
        telemetry_error = str(exc)

    status = args.status
    terminal_error = _unrecovered_error(events_content)
    if terminal_error is None and _terminal_error(events_content) is not None:
        # Jamais en silence : la phase a bien échoué quelque part, et l'opérateur
        # doit pouvoir compter ces reprises sans relire les flux à la main.
        print(f"[agy_dream_parser] {args.phase}: échec d'outil rejoué avec succès — statut tenu")
    if status == "done" and (terminal_error or telemetry_error):
        status = "fail"
        print(f"[agy_dream_parser] {args.phase}: événement terminal détecté — status done→fail")

    error_message = None
    if status != "done":
        error_message = _error_tail(
            raw_content,
            report_content,
            terminal_error or telemetry_error or "",
        )

    asyncio.run(
        _insert_dream_run(
            run_date=args.date,
            phase=args.phase,
            model=args.model,
            status=status,
            duration_s=args.duration,
            telemetry=telemetry,
            project_key=args.project_key,
            error_message=error_message,
            phase_dry_run=args.phase_dry_run,
        )
    )

    if telemetry is None:
        print(f"[agy_dream_parser] {args.phase}: télémétrie indisponible, cost=n/a")
    else:
        total_tokens = telemetry.input_tokens + telemetry.output_tokens
        print(
            f"[agy_dream_parser] {args.phase}: {total_tokens} fresh tokens, "
            f"{telemetry.cache_read_tokens} cached, cost=n/a, "
            f"{telemetry.tool_calls} tool calls"
        )


if __name__ == "__main__":
    main()
