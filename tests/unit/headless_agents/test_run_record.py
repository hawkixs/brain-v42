"""What every rail does after its process exits: read the answer, record the run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from headless_agents.result import RunResult
from headless_agents.run_record import answer_text, record, run_id_of
from headless_agents.spec import RunSpec


def _result(**overrides: object) -> RunResult:
    fields: dict[str, object] = {
        "exit_code": 0,
        "provider": "codex",
        "model": "m",
        "report_path": None,
        "events_log": None,
        "tokens": None,
        "duration_seconds": 0.5,
        "tool_call_completed": False,
    }
    fields.update(overrides)
    return RunResult(**fields)  # type: ignore[arg-type]


class TestAnswerText:
    def test_an_answer_that_is_json_comes_back_verbatim(self, tmp_path: Path) -> None:
        path = tmp_path / "report.log"
        path.write_text('{"result": "pass", "score": 3}\n', encoding="utf-8")
        assert answer_text(path, exit_code=0) == '{"result": "pass", "score": 3}\n'

    def test_a_failed_run_has_no_answer_even_with_a_report(self, tmp_path: Path) -> None:
        path = tmp_path / "report.log"
        path.write_text("half an answer", encoding="utf-8")
        assert answer_text(path, exit_code=1) is None
        assert answer_text(path, exit_code=3) is None
        assert answer_text(path, exit_code=124) is None

    def test_an_absent_or_blank_file_is_no_answer(self, tmp_path: Path) -> None:
        blank = tmp_path / "blank.log"
        blank.write_text(" \n\t\n", encoding="utf-8")
        assert answer_text(None, exit_code=0) is None
        assert answer_text(tmp_path / "absent.log", exit_code=0) is None
        assert answer_text(blank, exit_code=0) is None

    def test_offset_skips_what_a_previous_run_left_in_a_reused_log(self, tmp_path: Path) -> None:
        path = tmp_path / "raw.log"
        path.write_text("PREVIOUS RUN\n", encoding="utf-8")
        offset = path.stat().st_size
        with path.open("a", encoding="utf-8") as stream:
            stream.write("THIS RUN\n")
        assert answer_text(path, exit_code=0, offset=offset) == "THIS RUN\n"

    def test_undecodable_bytes_are_replaced_never_raised(self, tmp_path: Path) -> None:
        path = tmp_path / "report.log"
        path.write_bytes(b"ok \xff\n")
        assert answer_text(path, exit_code=0) == "ok �\n"


class TestRunIdOf:
    def test_is_the_name_of_the_run_directory(self, tmp_path: Path) -> None:
        assert run_id_of(RunSpec(prompt="P", run_dir=tmp_path / "20260923-a1")) == "20260923-a1"

    def test_is_none_without_a_run_directory(self) -> None:
        assert run_id_of(RunSpec(prompt="P")) is None


class TestRecord:
    def test_without_run_dir_nothing_is_written(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = _result()
        assert record(RunSpec(prompt="P"), result) is result
        assert list(tmp_path.iterdir()) == []

    def test_writes_the_schema_1_dict_into_a_run_dir_it_creates(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "runs" / "r1"
        result = _result(text="déjà ✓", run_id="r1")
        assert record(RunSpec(prompt="P", run_dir=run_dir), result) is result
        written = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert written == result.to_dict()
        assert sorted(path.name for path in run_dir.iterdir()) == ["result.json"]
