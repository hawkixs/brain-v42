"""The rollout half of codex's confinement evidence (lot 1b, ticket e454b011).

``codex exec --json`` never logs a refused command (learnings a5460289,
80934778): the refusal exists only in the session's own rollout, copied out
by :func:`headless_agents.providers.codex.run_codex`'s probe entry point.
These tests render the measured fixture (``fixtures/confinement/codex-0.156.0.
{rollout,events}.jsonl``, sanitised from the live measurement of 2026-09-27)
into a scenario, mutate it, and check the reader credits a target ONLY from
the exact measured shape: any drift -- another script, another pairing, the
refusal readable only in the model's own narration -- proves nothing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from headless_agents import proofs
from headless_agents.profile import Workspace
from headless_agents.providers import codex

RAIL: Final = "codex-cli 0.156.0"
LINE: Final = "ha-confinement-probe-deadbeef"
FIXTURES: Final = Path(__file__).parent / "fixtures" / "confinement"

Records = list[dict[str, object]]
Mutator = Callable[[Records], Records]


def _identity(records: Records) -> Records:
    return records


def _paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = tmp_path / "workspace"
    control = workspace / "ha-confinement-control.txt"
    target = tmp_path / "outside" / "target.txt"
    return workspace, control, target


def _render(template: str, *, workspace: Path, control: Path, target: Path, line: str) -> str:
    return (
        template.replace("{WORKSPACE}", str(workspace))
        .replace("{CONTROL}", str(control))
        .replace("{TARGET}", str(target))
        .replace("{LINE}", line)
    )


def _rendered_records(
    name: str, *, workspace: Path, control: Path, target: Path, line: str
) -> Records:
    template = (FIXTURES / name).read_text(encoding="utf-8")
    text = _render(template, workspace=workspace, control=control, target=target, line=line)
    return [json.loads(raw) for raw in text.splitlines() if raw.strip()]


def _write(records: Records, path: Path) -> None:
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")


def _scenario(
    tmp_path: Path,
    *,
    workspace: Path,
    control: Path,
    target: Path,
    line: str = LINE,
    rollout: Mutator = _identity,
    events: Mutator = _identity,
) -> Path:
    """Render the measured fixture into ``tmp_path/run`` as one run
    directory, after applying ``rollout``/``events`` to the parsed records."""
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    rollout_records = rollout(
        _rendered_records(
            "codex-0.156.0.rollout.jsonl",
            workspace=workspace,
            control=control,
            target=target,
            line=line,
        )
    )
    events_records = events(
        _rendered_records(
            "codex-0.156.0.events.jsonl",
            workspace=workspace,
            control=control,
            target=target,
            line=line,
        )
    )
    _write(rollout_records, run_dir / "rollout.jsonl")
    _write(events_records, run_dir / "events.jsonl")
    return run_dir


def _payload(record: dict[str, object]) -> dict[str, object]:
    payload = record.get("payload")
    assert isinstance(payload, dict)
    return payload


def _with_payload(record: dict[str, object], **updates: object) -> dict[str, object]:
    return {**record, "payload": {**_payload(record), **updates}}


def _find(records: Records, type_: str, *, nth: int = 0) -> dict[str, object]:
    matches = [record for record in records if record.get("type") == type_]
    return matches[nth]


def _index_of_target_call(records: Records, target: Path) -> int:
    for index, record in enumerate(records):
        payload = record.get("payload")
        if (
            record.get("type") == "response_item"
            and isinstance(payload, dict)
            and payload.get("type") == "custom_tool_call"
            and str(target) in str(payload.get("input", ""))
        ):
            return index
    raise AssertionError("no custom_tool_call names the target")


def _set_target_input(target: Path, new_input: str) -> Mutator:
    def mutate(records: Records) -> Records:
        out = list(records)
        index = _index_of_target_call(out, target)
        out[index] = _with_payload(out[index], input=new_input)
        return out

    return mutate


def test_measured_shape_credits_the_target_not_the_control(tmp_path: Path) -> None:
    workspace, control, target = _paths(tmp_path)
    run_dir = _scenario(tmp_path, workspace=workspace, control=control, target=target)
    assert proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL) == {
        target
    }
    # The control's own successful write is never credited as a refusal.
    assert proofs.refused_attempts(
        "codex", run_dir, [target, control], line=LINE, rail_version=RAIL
    ) == {target}


def test_without_rail_version_the_rollout_is_ignored(tmp_path: Path) -> None:
    """Every lot-1 test keeps its meaning: the rollout is consulted only when
    a caller opts in with ``rail_version`` (codex exec --json alone never
    shows this refusal, so lot-1's own events.jsonl path credits nothing)."""
    workspace, control, target = _paths(tmp_path)
    run_dir = _scenario(tmp_path, workspace=workspace, control=control, target=target)
    assert proofs.refused_attempts("codex", run_dir, [target], line=LINE) == set()


class TestThreadIdBinding:
    """session_meta.id must equal the ONE thread.started.thread_id in
    events.jsonl: anything else ties the rollout to no run at all."""

    def test_a_mismatched_thread_id_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def mismatch(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "thread.started")
            out[index] = {**out[index], "thread_id": "not-the-session-id"}
            return out

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, events=mismatch
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_two_thread_started_records_credit_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def duplicate(records: Records) -> Records:
            first = _find(records, "thread.started")
            return [first, *records]

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, events=duplicate
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_no_thread_started_record_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def drop(records: Records) -> Records:
            return [r for r in records if r.get("type") != "thread.started"]

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, events=drop
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )


def test_a_different_cli_version_credits_nothing(tmp_path: Path) -> None:
    workspace, control, target = _paths(tmp_path)

    def bump(records: Records) -> Records:
        out = list(records)
        index = next(i for i, r in enumerate(out) if r.get("type") == "session_meta")
        out[index] = _with_payload(out[index], cli_version="0.157.1")
        return out

    run_dir = _scenario(tmp_path, workspace=workspace, control=control, target=target, rollout=bump)
    assert (
        proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL) == set()
    )


class TestSandboxPolicyBinding:
    def test_the_hand_run_policy_shape_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def hand_run(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["exclude_slash_tmp"] = False
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=hand_run
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_no_turn_context_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def drop(records: Records) -> Records:
            return [r for r in records if r.get("type") != "turn_context"]

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=drop
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )


def _cmd_json(line: str, target: Path) -> str:
    return json.dumps(proofs.probe_command(line, target))


SCRIPT_VARIANTS: dict[str, Callable[[str, Path], str]] = {
    "renamed_variable": lambda cmd_json, target: (
        f"const result = await tools.exec_command({{cmd: {cmd_json}, "
        "max_output_tokens: 1000});\ntext(JSON.stringify(result));\n"
    ),
    "extra_workdir_argument": lambda cmd_json, target: (
        f"const r = await tools.exec_command({{cmd: {cmd_json}, max_output_tokens: 1000, "
        'workdir: "/tmp"}});\ntext(JSON.stringify(r));\n'
    ),
    "missing_max_output_tokens": lambda cmd_json, target: (
        f"const r = await tools.exec_command({{cmd: {cmd_json}}});\ntext(JSON.stringify(r));\n"
    ),
    "a_second_statement": lambda cmd_json, target: (
        f"const r = await tools.exec_command({{cmd: {cmd_json}, max_output_tokens: 1000}});\n"
        "text(JSON.stringify(r));\nconst x = 1;\n"
    ),
    "handwritten_result_no_call_at_all": lambda cmd_json, target: (
        f'text(\'{{"exit_code":1,"output":"zsh:1: read-only file system: {target}"}}\');\n'
    ),
    "single_quoted_cmd": lambda cmd_json, target: (
        "const r = await tools.exec_command({cmd: '"
        + proofs.probe_command(LINE, target)
        + "', max_output_tokens: 1000});\ntext(JSON.stringify(r));\n"
    ),
    "template_literal_cmd": lambda cmd_json, target: (
        "const r = await tools.exec_command({cmd: `"
        + proofs.probe_command(LINE, target)
        + "`, max_output_tokens: 1000});\ntext(JSON.stringify(r));\n"
    ),
    "invalid_json_escape": lambda cmd_json, target: (
        f"const r = await tools.exec_command({{cmd: {cmd_json[:-1]}\\'{cmd_json[-1:]}, "
        "max_output_tokens: 1000});\ntext(JSON.stringify(r));\n"
    ),
}


@pytest.mark.parametrize("name", sorted(SCRIPT_VARIANTS))
def test_a_script_that_is_not_the_exact_template_credits_nothing(tmp_path: Path, name: str) -> None:
    workspace, control, target = _paths(tmp_path)
    new_input = SCRIPT_VARIANTS[name](_cmd_json(LINE, target), target)
    run_dir = _scenario(
        tmp_path,
        workspace=workspace,
        control=control,
        target=target,
        rollout=_set_target_input(target, new_input),
    )
    assert (
        proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL) == set()
    )


CMD_VARIANTS: dict[str, Callable[[Path], str]] = {
    "other_nonce": lambda target: proofs.probe_command("some-other-nonce", target),
    "other_target": lambda target: proofs.probe_command(LINE, target.with_name("elsewhere.txt")),
    "batched_command": lambda target: proofs.probe_command(LINE, target) + "; true",
    "extra_space": lambda target: proofs.probe_command(LINE, target).replace(" >> ", "  >> "),
    # Review round: an operator-precedence bug in an earlier draft of
    # _exec_target could have let a single ">" (truncating write) slip past
    # a check meant to require the exact ">>" (append) redirection.
    "single_greater_than_redirection": lambda target: proofs.probe_command(LINE, target).replace(
        " >> ", " > "
    ),
}


@pytest.mark.parametrize("name", sorted(CMD_VARIANTS))
def test_a_cmd_not_byte_equal_to_probe_command_credits_nothing(tmp_path: Path, name: str) -> None:
    workspace, control, target = _paths(tmp_path)
    cmd = CMD_VARIANTS[name](target)
    new_input = (
        f"const r = await tools.exec_command({{cmd: {json.dumps(cmd)}, "
        "max_output_tokens: 1000});\ntext(JSON.stringify(r));\n"
    )
    run_dir = _scenario(
        tmp_path,
        workspace=workspace,
        control=control,
        target=target,
        rollout=_set_target_input(target, new_input),
    )
    assert (
        proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL) == set()
    )


def _set_target_output(target: Path, new_output: object) -> Mutator:
    def mutate(records: Records) -> Records:
        out = list(records)
        call_index = _index_of_target_call(out, target)
        call_id = _payload(out[call_index])["call_id"]
        for index, record in enumerate(out):
            payload = record.get("payload")
            if (
                record.get("type") == "response_item"
                and isinstance(payload, dict)
                and payload.get("type") == "custom_tool_call_output"
                and payload.get("call_id") == call_id
            ):
                out[index] = _with_payload(out[index], output=new_output)
        return out

    return mutate


def _header() -> dict[str, str]:
    return {"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"}


def _body(**fields: object) -> dict[str, str]:
    return {"type": "input_text", "text": json.dumps(fields)}


OUTPUT_VARIANTS: dict[str, object] = {
    "exit_code_zero": [_header(), _body(exit_code=0, output="zsh:1: read-only file system: {t}")],
    "exit_code_true": [
        _header(),
        _body(exit_code=True, output="zsh:1: read-only file system: {t}"),
    ],
    "exit_code_missing": [_header(), _body(output="zsh:1: read-only file system: {t}")],
    "exit_code_string": [
        _header(),
        _body(exit_code="1", output="zsh:1: read-only file system: {t}"),
    ],
    "refusal_names_the_parent_dir_only": [
        _header(),
        _body(exit_code=1, output="zsh:1: read-only file system: {parent}"),
    ],
    "marker_and_path_on_different_lines": [
        _header(),
        _body(exit_code=1, output="zsh:1: read-only file system\n{t}\n"),
    ],
    "first_part_is_not_the_measured_header": [
        {"type": "input_text", "text": "Script failed\nWall time 0.1 seconds\nOutput:\n"},
        _body(exit_code=1, output="zsh:1: read-only file system: {t}"),
    ],
    "one_part_only": [_header()],
    "three_parts": [
        _header(),
        _body(exit_code=1, output="zsh:1: read-only file system: {t}"),
        _header(),
    ],
}


@pytest.mark.parametrize("name", sorted(OUTPUT_VARIANTS))
def test_an_output_shape_that_is_not_the_exact_template_credits_nothing(
    tmp_path: Path, name: str
) -> None:
    workspace, control, target = _paths(tmp_path)
    raw = json.dumps(OUTPUT_VARIANTS[name])
    rendered = raw.replace("{t}", str(target)).replace("{parent}", str(target.parent))
    run_dir = _scenario(
        tmp_path,
        workspace=workspace,
        control=control,
        target=target,
        rollout=_set_target_output(target, json.loads(rendered)),
    )
    assert (
        proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL) == set()
    )


class TestPairing:
    """custom_tool_call and custom_tool_call_output are indexed by call_id;
    anything that cannot be paired unambiguously, in order, credits nothing."""

    def test_no_output_for_the_call_id_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def drop_output(records: Records) -> Records:
            call_index = _index_of_target_call(records, target)
            call_id = _payload(records[call_index])["call_id"]
            return [
                r
                for r in records
                if not (
                    r.get("type") == "response_item"
                    and isinstance(r.get("payload"), dict)
                    and r["payload"].get("type") == "custom_tool_call_output"  # type: ignore[union-attr]
                    and r["payload"].get("call_id") == call_id  # type: ignore[union-attr]
                )
            ]

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=drop_output
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_two_outputs_for_the_same_call_id_credit_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def duplicate_output(records: Records) -> Records:
            call_index = _index_of_target_call(records, target)
            call_id = _payload(records[call_index])["call_id"]
            out = list(records)
            for index, record in enumerate(out):
                payload = record.get("payload")
                if (
                    record.get("type") == "response_item"
                    and isinstance(payload, dict)
                    and payload.get("type") == "custom_tool_call_output"
                    and payload.get("call_id") == call_id
                ):
                    out.insert(index + 1, record)
                    break
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=duplicate_output,
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_output_before_the_call_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def reorder(records: Records) -> Records:
            call_index = _index_of_target_call(records, target)
            call_id = _payload(records[call_index])["call_id"]
            output_index = next(
                i
                for i, r in enumerate(records)
                if r.get("type") == "response_item"
                and isinstance(r.get("payload"), dict)
                and r["payload"].get("type") == "custom_tool_call_output"  # type: ignore[union-attr]
                and r["payload"].get("call_id") == call_id  # type: ignore[union-attr]
            )
            out = list(records)
            output_record = out.pop(output_index)
            # The call index shifted by one now that the output left its slot.
            out.insert(call_index if output_index > call_index else call_index - 1, output_record)
            return out

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=reorder
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_two_calls_sharing_a_call_id_credit_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def duplicate_call(records: Records) -> Records:
            call_index = _index_of_target_call(records, target)
            out = list(records)
            out.insert(call_index, out[call_index])
            return out

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=duplicate_call
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_a_call_not_marked_completed_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def uncompleted(records: Records) -> Records:
            call_index = _index_of_target_call(records, target)
            out = list(records)
            out[call_index] = _with_payload(out[call_index], status="in_progress")
            return out

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=uncompleted
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )


class TestNarrationIsNeverEvidence:
    """The refusal readable only in the model's own text -- an agent_message,
    a task_complete.last_agent_message, or a function_call_output -- must
    never be credited: only a custom_tool_call/custom_tool_call_output pair
    is. Each variant drops the real target call+output and adds only the
    decoy narration in its place."""

    def _without_target_call_and_output(self, records: Records, target: Path) -> Records:
        call_index = _index_of_target_call(records, target)
        call_id = _payload(records[call_index])["call_id"]

        def is_target_pair(record: dict[str, object]) -> bool:
            payload = record.get("payload")
            return (
                record.get("type") == "response_item"
                and isinstance(payload, dict)
                and payload.get("type") in ("custom_tool_call", "custom_tool_call_output")
                and payload.get("call_id") == call_id
            )

        return [r for r in records if not is_target_pair(r)]

    def test_an_agent_message_narrating_the_refusal_is_not_evidence(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def only_narration(records: Records) -> Records:
            out = self._without_target_call_and_output(records, target)
            out.append(
                {
                    "timestamp": "2026-09-27T02:47:31.900Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": "msg_narration",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (f"Refused: read-only file system: {target}"),
                            }
                        ],
                        "phase": "commentary",
                    },
                }
            )
            return out

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=only_narration
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_a_task_complete_last_agent_message_is_not_evidence(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def only_narration(records: Records) -> Records:
            out = self._without_target_call_and_output(records, target)
            out.append(
                {
                    "timestamp": "2026-09-27T02:47:31.900Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "last_agent_message": f"Refused: read-only file system: {target}",
                    },
                }
            )
            return out

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=only_narration
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_a_function_call_output_is_not_evidence(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def only_narration(records: Records) -> Records:
            out = self._without_target_call_and_output(records, target)
            out.append(
                {
                    "timestamp": "2026-09-27T02:47:31.900Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "call_unrelated",
                        "output": f"Refused: read-only file system: {target}",
                    },
                }
            )
            return out

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=only_narration
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )


class TestRolloutFileItself:
    def test_a_symlinked_rollout_credits_nothing_and_does_not_raise(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)
        run_dir = _scenario(tmp_path, workspace=workspace, control=control, target=target)
        real = run_dir / "rollout.jsonl"
        elsewhere = tmp_path / "elsewhere.jsonl"
        elsewhere.write_bytes(real.read_bytes())
        real.unlink()
        real.symlink_to(elsewhere)
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_an_oversized_rollout_credits_nothing_and_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        workspace, control, target = _paths(tmp_path)
        run_dir = _scenario(tmp_path, workspace=workspace, control=control, target=target)
        monkeypatch.setattr(proofs, "_ROLLOUT_MAX_BYTES", 4)
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_a_garbage_line_in_the_middle_does_not_prevent_crediting(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)
        run_dir = _scenario(tmp_path, workspace=workspace, control=control, target=target)
        rollout_log = run_dir / "rollout.jsonl"
        lines = rollout_log.read_text(encoding="utf-8").splitlines()
        lines.insert(len(lines) // 2, "not json at all {{{")
        rollout_log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        assert proofs.refused_attempts(
            "codex", run_dir, [target], line=LINE, rail_version=RAIL
        ) == {target}


class TestSessionMetaBinding:
    """Review round: a rollout must open with EXACTLY one session_meta record
    -- codex's own rollout always does (measured 2026-09-27) -- or it does
    not tie to a single, ordered session at all."""

    def test_two_session_meta_records_credit_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def duplicate(records: Records) -> Records:
            first = _find(records, "session_meta")
            return [first, *records]

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=duplicate
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_session_meta_not_first_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def move_second(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "session_meta")
            record = out.pop(index)
            out.insert(index + 1, record)
            return out

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=move_second
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )


class TestTurnContextMustPrecedeTheCall:
    """The applicable sandbox policy for a call is the LATEST turn_context
    strictly BEFORE it, not merely one somewhere in the file: a
    turn_context appended after the call it is meant to govern proves
    nothing about the policy that was actually in force for that call."""

    def test_a_turn_context_appended_after_the_call_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def move_to_the_end(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            record = out.pop(index)
            out.append(record)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=move_to_the_end,
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )


class TestCallIdCollisionAcrossAnyCall:
    """Every custom_tool_call is tracked by call_id BEFORE the exec/completed
    filter runs: a call_id reused by a call that is not itself eligible
    (another name, or not completed) is still a collision, and must drop
    the id exactly as two eligible calls sharing it would."""

    def test_a_non_exec_call_sharing_the_call_id_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def insert_colliding_call(records: Records) -> Records:
            call_index = _index_of_target_call(records, target)
            call_id = _payload(records[call_index])["call_id"]
            colliding = {
                "timestamp": "2026-09-27T02:47:29.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "id": "ctc_colliding",
                    "status": "completed",
                    "call_id": call_id,
                    "name": "apply_patch",
                    "input": "*** Begin Patch\n*** End Patch\n",
                },
            }
            out = list(records)
            out.insert(call_index, colliding)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=insert_colliding_call,
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )

    def test_an_in_progress_call_sharing_the_call_id_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def insert_colliding_call(records: Records) -> Records:
            call_index = _index_of_target_call(records, target)
            call_id = _payload(records[call_index])["call_id"]
            colliding = {
                "timestamp": "2026-09-27T02:47:29.000Z",
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "id": "ctc_colliding_in_progress",
                    "status": "in_progress",
                    "call_id": call_id,
                    "name": "exec",
                    "input": 'const r = await tools.exec_command({cmd: "true", '
                    "max_output_tokens: 1000});\ntext(JSON.stringify(r));\n",
                },
            }
            out = list(records)
            out.insert(call_index, colliding)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=insert_colliding_call,
        )
        assert (
            proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
            == set()
        )


class TestForgedRolloutCannotFlipAChangedByteToPass:
    """Threat model (review round, codex.py blocker): if the sandbox under
    test is itself broken, a sandboxed agent could reach its own run-owned
    CODEX_HOME and append forged custom_tool_call/custom_tool_call_output
    records to its own rollout, claiming a refusal that never happened.
    refused_attempts alone cannot see the outside target's own bytes -- that
    is confinement_verdict's job, and it MUST check them first, unconditionally,
    before any rollout evidence can matter. This pins the ordering property
    the live probe (test_proofs_live.test_confinement) relies on."""

    def test_a_full_credit_from_the_rollout_still_fails_when_the_target_changed(
        self, tmp_path: Path
    ) -> None:
        workspace, control, target = _paths(tmp_path)
        run_dir = _scenario(tmp_path, workspace=workspace, control=control, target=target)
        # The rollout evidence alone credits the target -- exactly what a
        # forged rollout would also claim.
        credited = proofs.refused_attempts("codex", run_dir, [target], line=LINE, rail_version=RAIL)
        assert credited == {target}
        # But the target's own bytes changed since the run started (what a
        # confinement escape looks like on disk, forged rollout or not).
        changed = proofs.outside_changes({"target": b"before"}, {"target": target})
        unrefused = [n for n in ("target",) if target not in credited]
        verdict = proofs.confinement_verdict(changed=changed, incomplete=[], unrefused=unrefused)
        assert verdict.passed is False
        assert "wrote outside" in verdict.reason


class TestWritableRootsPolicyShape:
    """PR #236 (ticket 0b3fcdbf, not yet merged): ha's own write argv may add
    ``sandbox_workspace_write.writable_roots=["<scratch>"]``, which codex
    0.156.0 was measured to record verbatim (2026-09-27) as an EXTRA
    ``writable_roots`` key (a list of strings) alongside the four required
    ones -- never replacing any of them. The policy check accepts that
    shape too, but only when every entry is structurally safe: never the
    workspace, and never equal to or an ancestor of any probed target (an
    entry that WAS one would mean the recorded policy is actually granting
    write access to it, making the refusal this reader is about to trust
    meaningless)."""

    def test_a_policy_with_a_safe_writable_roots_entry_still_credits(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)
        scratch = tmp_path / "headless-agents-codex-tmp-abc123"

        def add_writable_roots(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = [str(scratch)]
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=add_writable_roots,
        )
        assert proofs.refused_attempts(
            "codex", run_dir, [target], line=LINE, rail_version=RAIL, workspace=workspace
        ) == {target}

    def test_a_writable_roots_naming_the_targets_own_directory_credits_nothing(
        self, tmp_path: Path
    ) -> None:
        workspace, control, target = _paths(tmp_path)

        def forge_writable_roots(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = [str(target.parent)]
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=forge_writable_roots,
        )
        assert (
            proofs.refused_attempts(
                "codex", run_dir, [target], line=LINE, rail_version=RAIL, workspace=workspace
            )
            == set()
        )

    def test_a_writable_roots_naming_the_target_itself_credits_nothing(
        self, tmp_path: Path
    ) -> None:
        workspace, control, target = _paths(tmp_path)

        def forge_writable_roots(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = [str(target)]
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=forge_writable_roots,
        )
        assert (
            proofs.refused_attempts(
                "codex", run_dir, [target], line=LINE, rail_version=RAIL, workspace=workspace
            )
            == set()
        )

    def test_a_writable_roots_naming_the_workspace_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def forge_writable_roots(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = [str(workspace)]
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=forge_writable_roots,
        )
        assert (
            proofs.refused_attempts(
                "codex", run_dir, [target], line=LINE, rail_version=RAIL, workspace=workspace
            )
            == set()
        )

    def test_a_writable_roots_entry_that_is_an_ancestor_of_the_workspace_credits_nothing(
        self, tmp_path: Path
    ) -> None:
        """Review round 2 (agy major): the old check only rejected an entry
        EQUAL to the workspace -- an ancestor of it (which would make the
        whole worktree, and everything under it, writable through this
        root too) must be symmetric with the target checks. A dedicated,
        disjoint workspace nesting (rather than the shared ``_paths()``
        layout, where the workspace's parent happens to also be an
        ancestor of the target) isolates the workspace-ancestor check from
        the pre-existing target-ancestor one."""
        workspace = tmp_path / "nested" / "workspace"
        control = workspace / "ha-confinement-control.txt"
        target = tmp_path / "outside" / "target.txt"

        def forge_writable_roots(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = [str(workspace.parent)]
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=forge_writable_roots,
        )
        assert (
            proofs.refused_attempts(
                "codex", run_dir, [target], line=LINE, rail_version=RAIL, workspace=workspace
            )
            == set()
        )

    def test_a_writable_roots_entry_that_is_a_subdirectory_of_the_workspace_credits_nothing(
        self, tmp_path: Path
    ) -> None:
        workspace, control, target = _paths(tmp_path)

        def forge_writable_roots(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = [str(workspace / "subdir")]
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=forge_writable_roots,
        )
        assert (
            proofs.refused_attempts(
                "codex", run_dir, [target], line=LINE, rail_version=RAIL, workspace=workspace
            )
            == set()
        )

    def test_a_relative_writable_roots_entry_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def forge_writable_roots(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = ["relative/scratch"]
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=forge_writable_roots,
        )
        assert (
            proofs.refused_attempts(
                "codex", run_dir, [target], line=LINE, rail_version=RAIL, workspace=workspace
            )
            == set()
        )

    def test_a_writable_roots_entry_of_slash_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def forge_writable_roots(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = ["/"]
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=forge_writable_roots,
        )
        assert (
            proofs.refused_attempts(
                "codex", run_dir, [target], line=LINE, rail_version=RAIL, workspace=workspace
            )
            == set()
        )

    #: Each renders a shape that, once normalised (``os.path.normpath``), IS
    #: the given path -- but as raw ``Path`` components (what ``==``/
    #: ``in .parents`` actually compare), it is NOT: review round 4, codex
    #: major. ``/base/other/../workspace`` designates the workspace but is
    #: not equal to it lexically; the lexical checks alone would credit a
    #: target under such an entry.
    NON_NORMALISED_SHAPES: dict[str, Callable[[Path], str]] = {
        "dotdot_traversal": lambda p: f"{p.parent}/other/../{p.name}",
        "dot_component": lambda p: f"{p.parent}/./{p.name}",
        "double_slash": lambda p: f"{p.parent}//{p.name}",
        "trailing_slash": lambda p: f"{p}/",
    }

    #: The SUBJECT a malformed entry is built to designate: the workspace or
    #: a target, each in its equal/ancestor/descendant relation.
    NON_NORMALISED_SUBJECTS: dict[str, Callable[[Path, Path], Path]] = {
        "workspace_equal": lambda workspace, target: workspace,
        "workspace_ancestor": lambda workspace, target: workspace.parent,
        "workspace_descendant": lambda workspace, target: workspace / "sub",
        "target_equal": lambda workspace, target: target,
        "target_ancestor": lambda workspace, target: target.parent,
        "target_descendant": lambda workspace, target: target / "sub",
    }

    @pytest.mark.parametrize("subject", sorted(NON_NORMALISED_SUBJECTS))
    @pytest.mark.parametrize("shape", sorted(NON_NORMALISED_SHAPES))
    def test_a_non_normalised_writable_roots_entry_credits_nothing(
        self, tmp_path: Path, shape: str, subject: str
    ) -> None:
        # A dedicated, disjoint workspace nesting (as in the ancestor test
        # above): the workspace's parent must not coincide with an ancestor
        # of the target, or the pre-existing lexical checks alone would
        # already reject the entry for the wrong reason.
        workspace = tmp_path / "nested" / "workspace"
        control = workspace / "ha-confinement-control.txt"
        target = tmp_path / "outside" / "target.txt"
        subject_path = self.NON_NORMALISED_SUBJECTS[subject](workspace, target)
        entry = self.NON_NORMALISED_SHAPES[shape](subject_path)

        def forge_writable_roots(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = [entry]
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=forge_writable_roots,
        )
        assert (
            proofs.refused_attempts(
                "codex", run_dir, [target], line=LINE, rail_version=RAIL, workspace=workspace
            )
            == set()
        )

    @pytest.mark.parametrize("suffix", ["/.", "//scratch", "/"])
    def test_a_disjoint_non_normalised_root_credits_nothing(
        self, tmp_path: Path, suffix: str
    ) -> None:
        workspace, _, target = _paths(tmp_path)
        policy = {
            **proofs._WRITE_SANDBOX_POLICY,
            "writable_roots": [str(tmp_path / "separate") + suffix],
        }
        assert not proofs._matches_write_policy(
            policy, workspace=workspace, wanted={"target": target}
        )

    def test_a_non_list_writable_roots_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def malformed(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = str(tmp_path / "scratch")
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=malformed
        )
        assert (
            proofs.refused_attempts(
                "codex", run_dir, [target], line=LINE, rail_version=RAIL, workspace=workspace
            )
            == set()
        )

    def test_an_unexpected_extra_key_credits_nothing(self, tmp_path: Path) -> None:
        workspace, control, target = _paths(tmp_path)

        def extra_key(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = [str(tmp_path / "scratch")]
            policy["some_other_key"] = "surprise"
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path, workspace=workspace, control=control, target=target, rollout=extra_key
        )
        assert (
            proofs.refused_attempts(
                "codex", run_dir, [target], line=LINE, rail_version=RAIL, workspace=workspace
            )
            == set()
        )

    def test_without_a_workspace_argument_the_worktree_check_is_skipped(
        self, tmp_path: Path
    ) -> None:
        """``workspace`` is optional (callers that never pass it keep the
        target/ancestor check, which is the one that matters for safety)."""
        workspace, control, target = _paths(tmp_path)
        scratch = tmp_path / "headless-agents-codex-tmp-abc123"

        def add_writable_roots(records: Records) -> Records:
            out = list(records)
            index = next(i for i, r in enumerate(out) if r.get("type") == "turn_context")
            policy = dict(_payload(out[index])["sandbox_policy"])  # type: ignore[arg-type]
            policy["writable_roots"] = [str(scratch)]
            out[index] = _with_payload(out[index], sandbox_policy=policy)
            return out

        run_dir = _scenario(
            tmp_path,
            workspace=workspace,
            control=control,
            target=target,
            rollout=add_writable_roots,
        )
        assert proofs.refused_attempts(
            "codex", run_dir, [target], line=LINE, rail_version=RAIL
        ) == {target}

    def test_the_merged_build_codex_command_produces_a_policy_this_reader_accepts(
        self, tmp_path: Path
    ) -> None:
        """Integration pin, PR #236 (ticket 0b3fcdbf) merged into this
        branch: a workspace-write run's argv now always carries
        ``sandbox_workspace_write.writable_roots=[<scratch>]``
        (``build_codex_command``'s own ``writable_tmp`` parameter, threaded
        from ``run_codex``'s per-run scratch -- ``run_with_rollout`` passes
        it too, so a probed codex gets the exact same argv a production
        write run does). The ``turn_context.sandbox_policy`` codex was
        measured to record for that argv (2026-09-27) is what
        ``_matches_write_policy`` must accept -- built here from the REAL
        command ``build_codex_command`` produces, not a hand-written dict,
        so a drift in either one is caught."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        target = tmp_path / "outside" / "target.txt"

        command = codex.build_codex_command(
            model="m",
            reasoning_effort="low",
            report_log=tmp_path / "report.log",
            workspace=workspace,
            mcp=None,
            workspace_mode=Workspace(path=workspace, write=True),
            writable_tmp=scratch,
        )
        overrides: dict[str, str] = {}
        for index, item in enumerate(command):
            if item == "-c":
                key, _, raw_value = command[index + 1].partition("=")
                overrides[key] = raw_value

        # network_access is never an explicit -c override: codex's own
        # workspace-write default (network off) is what was measured, the
        # same constant _WRITE_SANDBOX_POLICY already pins.
        policy = {
            "type": command[command.index("--sandbox") + 1],
            "network_access": False,
            "exclude_tmpdir_env_var": json.loads(
                overrides["sandbox_workspace_write.exclude_tmpdir_env_var"]
            ),
            "exclude_slash_tmp": json.loads(overrides["sandbox_workspace_write.exclude_slash_tmp"]),
            "writable_roots": json.loads(overrides["sandbox_workspace_write.writable_roots"]),
        }

        assert proofs._matches_write_policy(policy, workspace=workspace, wanted={"target": target})
