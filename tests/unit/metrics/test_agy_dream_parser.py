"""Le parser de métriques du rail agy — et la convention de cache qui l'oppose à codex.

Sans lui, une phase jouée par agy est jouée mais NON MESURÉE, et c'est ce trou
qui a dicté pendant quelques heures un ordre de chaîne absurde : claude placé
avant agy pour préserver les lignes `dream_runs`, donc l'abonnement qu'on voulait
épargner mis en première ligne. Optimiser la mesure contre l'objectif.

LE PIÈGE CENTRAL, mesuré le 2026-08-11 sur un vrai run :

    input_tokens      = 20137
    cache_read_tokens = 56950      <-- PLUS GRAND que input_tokens
    total_tokens      = 21691      = input + output, le cache n'y est PAS

Chez codex, `cached_input_tokens` est un SOUS-ENSEMBLE de `input_tokens` et le
parser calcule `frais = input - cached`. Chez agy les deux compteurs sont
INDÉPENDANTS : `input_tokens` est déjà le frais. Appliquer la formule de codex
ici produirait un nombre NÉGATIF — et personne ne regarde une colonne de
tokens assez près pour s'en apercevoir avant des semaines.
"""

from __future__ import annotations

import json

import pytest

from brain_v42.metrics.agy_dream_parser import _unrecovered_error, parse_agy_stream

# Le message exact qu'agy pose dans `result.error` quand un appel MCP a échoué.
_TOOL_ERROR = (
    "Error in MCP tool execution: 4 validation errors for call[brain_save_snippet]\n"
    "title\n  Missing required argument"
)

# Relevé intégral d'un run réel, pas une maquette.
_REAL_RESULT_USAGE = {
    "input_tokens": 20137,
    "output_tokens": 1554,
    "thinking_tokens": 962,
    "cache_read_tokens": 56950,
    "total_tokens": 21691,
}


def _stream(*events: dict[str, object]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _result_event(usage: dict[str, object] | None = None, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {"status": "SUCCESS", "response": "rapport"}
    payload["usage"] = usage if usage is not None else dict(_REAL_RESULT_USAGE)
    payload.update(extra)
    return {"event": "result", "result": payload}


def _tool_step(tool_name: str, state: str = "DONE") -> dict[str, object]:
    return {
        "event": "step_update",
        "step_update": {
            "step_index": 3,
            "step_type": "tool",
            "state": state,
            "tool_name": tool_name,
        },
    }


def _mcp_step(tool: str, state: str, step_index: int) -> dict[str, object]:
    """Une étape `call_mcp_tool`, avec l'outil réellement appelé.

    Le nom de l'outil vit dans `tool_info.parameters.ToolName` — `tool_name`
    vaut toujours `call_mcp_tool` et ne distingue donc rien.
    """
    return {
        "event": "step_update",
        "step_update": {
            "step_index": step_index,
            "step_type": "tool",
            "state": state,
            "tool_name": "call_mcp_tool",
            "tool_info": {"parameters": {"ServerName": "brain-v42", "ToolName": tool}},
        },
    }


def test_a_tool_error_retried_successfully_is_not_terminal() -> None:
    """LE test de cette règle, relevé sur brain-v42/synth du 2026-08-12.

    Étape 47 : `brain_save_snippet(description=…, topic=…)` -> ERROR, arguments
    invalides. Étape 50 : `brain_save_snippet(title=…, intention=…)` -> DONE.
    agy latche pourtant `result.status=ERROR`, et la phase était comptée `fail`
    alors que ses trois artefacts sont en base (93ae6c8d, adbc5f88, 2989018b).
    """
    content = _stream(
        _mcp_step("brain_save_snippet", "ERROR", 47),
        _mcp_step("brain_save_snippet", "DONE", 50),
        _result_event(status="ERROR", error=_TOOL_ERROR),
    )

    assert _unrecovered_error(content) is None


def test_a_phase_that_ends_on_a_failed_tool_call_stays_terminal() -> None:
    """Relevé sur watchk-claude/synth : deux learnings écrits, puis un dernier
    `brain_learn` en échec que rien ne retente. L'écriture est perdue pour de
    bon — ce rouge-là doit rester rouge."""
    content = _stream(
        _mcp_step("brain_learn", "DONE", 42),
        _mcp_step("brain_learn", "ERROR", 46),
        _result_event(status="ERROR", error="Error in MCP tool execution: 'brain_learn'"),
    )

    assert _unrecovered_error(content) == "Error in MCP tool execution: 'brain_learn'"


def test_another_tool_succeeding_later_does_not_prove_recovery() -> None:
    """Relevé sur refondrre/connect : `brain_assign_domain` échoue, puis un
    `brain_list` réussit. Un autre outil qui passe ne dit RIEN de l'écriture
    perdue. Seul le même appel retenté prouve la reprise."""
    content = _stream(
        _mcp_step("brain_assign_domain", "ERROR", 28),
        _mcp_step("brain_list", "DONE", 36),
        _result_event(status="ERROR", error="Error in MCP tool execution: brain_assign_domain"),
    )

    assert _unrecovered_error(content) is not None


def test_an_agy_level_error_stays_terminal_despite_a_successful_retry() -> None:
    """La suppression ne vaut QUE pour un échec d'outil. Une panne d'agy
    lui-même emporte la phase, quoi qu'aient fait les outils avant elle."""
    content = _stream(
        _mcp_step("brain_learn", "ERROR", 4),
        _mcp_step("brain_learn", "DONE", 7),
        {"event": "error", "message": "agy: connection reset"},
        _result_event(status="ERROR", error=_TOOL_ERROR),
    )

    assert _unrecovered_error(content) == "agy: connection reset"


def test_a_failure_status_without_a_tool_error_stays_terminal() -> None:
    """Un `result.status` d'échec sans message d'outil — un timeout, par
    exemple — n'est pas rattrapable par un retry réussi."""
    content = _stream(
        _mcp_step("brain_learn", "ERROR", 4),
        _mcp_step("brain_learn", "DONE", 7),
        _result_event(status="TIMEOUT"),
    )

    assert _unrecovered_error(content) == "TIMEOUT"


def test_steps_without_an_index_cannot_prove_recovery() -> None:
    """Fail-closed : sans indice, l'ordre des étapes est inconnu et « après »
    n'a plus de sens. Supposer la reprise rendrait un vert non mesuré."""
    step = _mcp_step("brain_save_snippet", "DONE", 50)
    del step["step_update"]["step_index"]  # type: ignore[index]
    content = _stream(
        _mcp_step("brain_save_snippet", "ERROR", 47),
        step,
        _result_event(status="ERROR", error=_TOOL_ERROR),
    )

    assert _unrecovered_error(content) is not None


def test_cache_reads_are_not_subtracted_from_input() -> None:
    """LE test de ce fichier. La convention d'agy est l'inverse de celle de codex."""
    telemetry = parse_agy_stream(_stream(_result_event()))

    assert telemetry.input_tokens == 20137, "input_tokens d'agy est DÉJÀ le frais"
    assert telemetry.cache_read_tokens == 56950
    assert telemetry.output_tokens == 1554
    assert telemetry.input_tokens > 0, "la soustraction de codex donnerait un négatif ici"


def test_the_result_event_is_the_authoritative_aggregate() -> None:
    """Mesuré : la somme des usages `step_update` égale exactement celui du
    `result`. Additionner les deux doublerait tous les compteurs."""
    telemetry = parse_agy_stream(
        _stream(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": 0,
                    "step_type": "agent_response",
                    "state": "DONE",
                    "usage": {
                        "input_tokens": 10480,
                        "output_tokens": 383,
                        "cache_read_tokens": 8141,
                    },
                },
            },
            _result_event(),
        )
    )

    assert telemetry.input_tokens == 20137
    assert telemetry.output_tokens == 1554


def test_only_completed_mcp_steps_are_counted_as_tool_calls() -> None:
    """Un run_command REFUSÉ par la garde produit une étape d'outil. La compter
    ferait croire qu'une phase a touché le brain alors qu'elle en a été
    empêchée."""
    telemetry = parse_agy_stream(
        _stream(
            _tool_step("call_mcp_tool"),
            _tool_step("call_mcp_tool"),
            _tool_step("run_command"),
            _tool_step("call_mcp_tool", state="ERROR"),
            _result_event(),
        )
    )

    assert telemetry.tool_calls == 2


def test_subscription_backed_fields_stay_null_instead_of_zero() -> None:
    """agy est adossé à un abonnement : il n'expose ni coût ni nombre d'appels
    API. Écrire 0 affirmerait « gratuit » et « aucun appel », deux mensonges ;
    NULL dit « non mesuré »."""
    telemetry = parse_agy_stream(_stream(_result_event()))

    assert telemetry.cost_usd is None
    assert telemetry.api_calls is None
    assert telemetry.cache_creation_tokens is None


def test_a_stream_without_a_result_event_is_rejected() -> None:
    """Fail-closed : sans event `result`, aucun total n'est connu. Persister des
    zéros ferait passer une phase non mesurée pour une phase gratuite."""
    with pytest.raises(ValueError, match="result"):
        parse_agy_stream(_stream(_tool_step("call_mcp_tool")))


def test_a_result_without_usage_is_rejected() -> None:
    with pytest.raises(ValueError, match="usage"):
        parse_agy_stream(_stream({"event": "result", "result": {"status": "SUCCESS"}}))


def test_malformed_lines_are_skipped_not_fatal() -> None:
    """Le flux peut porter des lignes de diagnostic ou d'une version future."""
    telemetry = parse_agy_stream(
        "pas du json\n" + json.dumps(["liste"]) + "\n" + _stream(_result_event())
    )

    assert telemetry.input_tokens == 20137


def test_negative_or_absurd_counters_are_rejected() -> None:
    with pytest.raises(ValueError):
        parse_agy_stream(_stream(_result_event(usage={"input_tokens": -1, "output_tokens": 5})))


def test_a_failed_run_still_yields_its_usage() -> None:
    """Une phase qui échoue a quand même consommé des tokens. Les perdre
    fausserait le coût réel d'une nuit dégradée — précisément la nuit qu'on
    veut pouvoir chiffrer."""
    telemetry = parse_agy_stream(
        _stream(
            _result_event(
                status="ERROR",
                usage={"input_tokens": 10, "output_tokens": 2, "cache_read_tokens": 3},
            )
        )
    )

    assert telemetry.input_tokens == 10
    assert telemetry.output_tokens == 2
