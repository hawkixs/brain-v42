"""Reading the tool calls REALLY emitted by the REORG phase.

The agent's report is a DECLARATION. The event stream is an OBSERVATION.
Confronting them is the only way to see the `bccc9115` ghost: an id declared in
`updated` for which no `brain_update` was ever emitted. The symmetric case — a
`brain_update` emitted for an id the report does not mention — is today entirely
invisible, and it is the more worrying of the two: a mutation nobody has a trace
of.

TWO FORMATS, and the requirement is hard. The live rail is codex
(`BRAIN_DREAM_AGENT_PROVIDER=codex` by default); agy is the fallback. A
single-format parser would return "0 writes observed" on any fallback night — a
false negative with exactly the shape of good news, and that nobody would
question. That is why "no recognised event" must be a fact DISTINCT from "zero
calls", and not the same silence.

The two shapes below are MEASURED on real streams from the repository, not
assumed: `logs/dream/2026-08-20_watchk-claude_reorg.events.jsonl` for codex,
`logs/dream/2026-08-13_red-shrik:agent_reorg.events.jsonl` for agy. The agy shape
in particular cannot be guessed — the tool's arguments live under
`tool_info.parameters.Arguments`, while the name lives under `ToolName` and
`tool_name` is invariably `call_mcp_tool`.
"""

from __future__ import annotations

import json

from scripts.dream.reorg_events import scan_events

_LID = "f9bd5e03-cbc4-43d4-92d8-c3c98c1a19b6"
_DID = "cf988d7c-ffb1-4bd4-8b64-cbaf39c9256e"


def _codex_update(entity_id: str, tool: str = "brain_update") -> str:
    """Shape measured on a real codex stream (item.completed / mcp_tool_call)."""
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "item_44",
                "type": "mcp_tool_call",
                "server": "brain-v42",
                "tool": tool,
                "arguments": {
                    "entity_type": "learning",
                    "entity_id": entity_id,
                    "fields": {"tags": ["a"]},
                },
                "status": "completed",
                "error": None,
            },
        }
    )


def _agy_update(entity_id: str, tool: str = "brain_update") -> str:
    """Shape measured on a real agy stream (step_update / call_mcp_tool)."""
    return json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "step_index": 12,
                "step_type": "tool",
                "state": "DONE",
                "tool_name": "call_mcp_tool",
                "tool_info": {
                    "name": "call_mcp_tool",
                    "parameters": {
                        "Arguments": {"entity_type": "learning", "entity_id": entity_id},
                        "ServerName": "brain-v42",
                        "ToolName": tool,
                    },
                },
            },
        }
    )


def test_codex_updates_are_observed() -> None:
    scan = scan_events("\n".join([_codex_update(_LID), _codex_update(_DID)]))

    assert scan.updated_ids == {_LID, _DID}
    assert scan.codex_events == 2
    assert scan.agy_events == 0
    assert scan.recognised is True


def test_agy_updates_are_observed() -> None:
    """The fallback must count as much as the live rail.

    This is THE test that prevents the false negative: without it, a fallback
    night would yield a mute stream, and "no undeclared write" would read as a
    clean night when nothing had been looked at.
    """
    scan = scan_events("\n".join([_agy_update(_LID), _agy_update(_DID)]))

    assert scan.updated_ids == {_LID, _DID}
    assert scan.agy_events == 2
    assert scan.codex_events == 0
    assert scan.recognised is True


def test_a_mixed_stream_is_read_in_both_dialects() -> None:
    """Unlikely in production, decisive as a witness: neither format excludes the other."""
    scan = scan_events("\n".join([_codex_update(_LID), _agy_update(_DID)]))

    assert scan.updated_ids == {_LID, _DID}
    assert scan.codex_events == 1
    assert scan.agy_events == 1


def test_only_brain_update_counts_as_a_mutation() -> None:
    """`brain_list` and `brain_get` are most of the stream — counting them would drown the signal.

    REORG paginates its whole corpus before mutating anything: on the 2026-08-20
    stream, 22 `brain_list` and 6 `brain_get` for 4 `brain_update`.
    """
    scan = scan_events(
        "\n".join(
            [
                _codex_update(_LID, tool="brain_get"),
                _agy_update(_DID, tool="brain_list"),
                _codex_update(_DID),
            ]
        )
    )

    assert scan.updated_ids == {_DID}


def test_another_mcp_server_is_not_ours() -> None:
    """A `brain_update` from another MCP server proves nothing about our corpus."""
    foreign = json.loads(_codex_update(_LID))
    foreign["item"]["server"] = "some-other-server"
    scan = scan_events(json.dumps(foreign))

    assert scan.updated_ids == set()


def test_blank_and_malformed_lines_are_skipped_without_crashing() -> None:
    """A stream truncated by a timeout is the NORMAL case, not the exception."""
    scan = scan_events("\n".join(["", "   ", "{ceci n'est pas du JSON", _codex_update(_LID)]))

    assert scan.updated_ids == {_LID}
    assert scan.recognised is True


def test_an_unreadable_stream_is_not_the_same_fact_as_zero_calls() -> None:
    """THE batch's trap: "nothing recognised" and "nothing called" must be distinguished.

    Both give an empty set. Confusing them turns a reading failure — a new agent
    format, an empty file, a stream from another command — into silent good news.
    """
    scan = scan_events('{"type":"thread.started","thread_id":"x"}\n{"unknown":"shape"}')

    assert scan.updated_ids == set()
    assert scan.recognised is False


def test_an_update_without_an_entity_id_is_counted_but_not_attributed() -> None:
    """A call with no id is observed without being attributable — it must invent nothing.

    Counting it as a recognised event keeps `recognised` honest; manufacturing an
    id for it would make the symmetry check say something it did not see.
    """
    payload = json.loads(_codex_update(_LID))
    del payload["item"]["arguments"]["entity_id"]
    scan = scan_events(json.dumps(payload))

    assert scan.updated_ids == set()
    assert scan.recognised is True


# ────────── Symmetry between report and observed calls ───────────────────────


def _report(updated: list[str], archived: list[str] | None = None) -> dict:
    return {
        "dry_run": False,
        "found_marker": True,
        "updated_ids": updated,
        "archived_ids": archived or [],
    }


def test_a_declared_id_that_was_never_called_is_named() -> None:
    """The `bccc9115` ghost: declared in `updated`, never written.

    This is the only direction we already knew how to look for — and even then, by
    hand.
    """
    from scripts.dream.reorg_validate import symmetry_warnings

    scan = scan_events(_codex_update(_LID))
    warnings = symmetry_warnings(_report([_LID, _DID]), scan)

    assert len(warnings) == 1
    assert _DID in warnings[0]
    assert _LID not in warnings[0], "l'id réellement écrit ne doit pas être dénoncé"


def test_a_call_that_no_report_declared_is_named() -> None:
    """The direction that is INVISIBLE today, and the more worrying of the two.

    A mutation the report does not speak of leaves no readable trace: neither the
    validator, nor the alert, nor the briefing would mention it. It would exist
    only in the event stream, which nobody reads back.
    """
    from scripts.dream.reorg_validate import symmetry_warnings

    scan = scan_events("\n".join([_codex_update(_LID), _codex_update(_DID)]))
    warnings = symmetry_warnings(_report([_LID]), scan)

    assert len(warnings) == 1
    assert _DID in warnings[0]


def test_an_archived_id_is_declared_too() -> None:
    """Part 2 also goes through `brain_update` — ignoring it would invent ghosts.

    `phase_reorg.md` §Part 2 d: archiving means writing
    `fields={"freshness_status": "archived"}` with the same tool. Comparing against
    `updated` alone would denounce every archival as an undeclared mutation, and
    the check would scream every night about its own nominal operation.
    """
    from scripts.dream.reorg_validate import symmetry_warnings

    scan = scan_events(_codex_update(_DID))

    assert symmetry_warnings(_report([], archived=[_DID]), scan) == []


def test_a_matching_pair_is_silent() -> None:
    from scripts.dream.reorg_validate import symmetry_warnings

    scan = scan_events(_codex_update(_LID))

    assert symmetry_warnings(_report([_LID]), scan) == []


def test_an_unreadable_stream_says_so_instead_of_denouncing_everything() -> None:
    """THE inverted false negative, and its twin: neither "all wrong" nor "all well".

    On an unreadable stream, `updated_ids` is empty. Comparing it naively would
    denounce EVERY declared id as a ghost — a massive, false alert one would
    quickly learn to ignore. And staying silent would make "nothing abnormal"
    indistinguishable from "nothing was read". The only honest warning names the
    inability to verify.
    """
    from scripts.dream.reorg_validate import symmetry_warnings

    scan = scan_events('{"type":"thread.started","thread_id":"x"}')
    warnings = symmetry_warnings(_report([_LID, _DID]), scan)

    assert len(warnings) == 1
    assert "UNVERIFIED" in warnings[0]
    assert _LID not in warnings[0] and _DID not in warnings[0]


# ────────── The CLI: a WARNING, never a failure ──────────────────────────────


def _validator_argv(tmp_path, *, trailer: str, events: str | None) -> list[str]:
    report_log = tmp_path / "reorg.log"
    report_log.write_text(trailer, encoding="utf-8")
    tags_before = tmp_path / "tags_before.json"
    tags_before.write_text("{}", encoding="utf-8")
    events_path = tmp_path / "reorg.events.jsonl"
    if events is not None:
        events_path.write_text(events, encoding="utf-8")
    return [
        "--report-log",
        str(report_log),
        "--project-key",
        "rv-cli-unused",
        "--tags-before-json",
        str(tags_before),
        "--events-jsonl",
        str(events_path),
    ]


def _empty_trailer() -> str:
    return '=== REORG REPORT ===\n{"dry_run": false, "updated": [], "archived": []}\n=== END ===\n'


def test_the_cli_warns_without_failing_the_phase(tmp_path, monkeypatch, capsys) -> None:
    """An undeclared write must be VISIBLE, and must not yet redden anything.

    Escalation to failure awaits a week of clean observation. A guard that starts
    by failing nights it has never measured teaches operators to disable it — and
    that is the only failure there is no coming back from.
    """
    from unittest.mock import MagicMock

    from scripts.dream import reorg_validate

    monkeypatch.setattr(
        reorg_validate,
        "Settings",
        lambda: MagicMock(postgres_url="postgresql+asyncpg://unused"),
    )
    monkeypatch.setattr(reorg_validate, "_build_factory", lambda _url: MagicMock())

    argv = _validator_argv(tmp_path, trailer=_empty_trailer(), events=_codex_update(_LID))
    rc = reorg_validate.main(argv)

    err = capsys.readouterr().err
    assert rc == 0, "le contrôle de symétrie ne doit pas changer le code de retour"
    assert "REORG SYMMETRY WARN" in err
    assert _LID in err
    assert "REORG VALIDATE: OK" in err, "le verdict du validateur doit rester lisible"


def test_an_absent_event_file_is_announced_once(tmp_path, monkeypatch, capsys) -> None:
    """A missing stream (a phase that died before writing) announces itself, without crashing or doubling.

    Two warnings for a single fact teach people to skim warnings; that is how an
    alert stops being read.
    """
    from unittest.mock import MagicMock

    from scripts.dream import reorg_validate

    monkeypatch.setattr(
        reorg_validate,
        "Settings",
        lambda: MagicMock(postgres_url="postgresql+asyncpg://unused"),
    )
    monkeypatch.setattr(reorg_validate, "_build_factory", lambda _url: MagicMock())

    argv = _validator_argv(tmp_path, trailer=_empty_trailer(), events=None)
    rc = reorg_validate.main(argv)

    err = capsys.readouterr().err
    assert rc == 0
    assert err.count("REORG SYMMETRY WARN") == 1, f"avertissements doublés :\n{err}"
    assert "unreadable" in err
