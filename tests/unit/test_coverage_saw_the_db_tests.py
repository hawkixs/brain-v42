"""The guardrail that refuses a coverage measured on a subset.

It is worth more than the percentage it protects: a test that SKIPS does not redden
a job, so without it CI stays green on partial coverage. That is exactly the state
`test-coverage` was in — no Postgres service, no `BRAIN_V42_TEST_DB_URL`, hence 60
tests skipped in silence (measured 2026-08-22).

The fixtures below are MINIMAL JUnit reports written by hand, and that is a
deliberate choice: what is under test is the READING of the report, not pytest. The
proof that the check bites on real reports was made by mutation in both directions,
on the JUnit files of both real worlds.
"""

from __future__ import annotations

from pathlib import Path

from scripts.check_coverage_saw_the_db_tests import db_skipped_tests, main

_DB_SKIP = "BRAIN_V42_TEST_DB_URL not set — skipping DB-backed tests to avoid polluting prod"


def _report(tmp_path: Path, *cases: str) -> Path:
    path = tmp_path / "junit.xml"
    path.write_text(f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>")
    return path


def _skipped(name: str, message: str) -> str:
    return f'<testcase classname="tests.unit.t" name="{name}"><skipped message="{message}"/></testcase>'


def _passed(name: str) -> str:
    return f'<testcase classname="tests.unit.t" name="{name}"/>'


class TestItRefusesASubset:
    def test_a_db_skip_fails_the_job(self, tmp_path: Path, capsys) -> None:
        report = _report(tmp_path, _passed("ok"), _skipped("needs_db", _DB_SKIP))
        assert main(["prog", str(report)]) == 1
        assert "1 DB-backed test(s) were SKIPPED" in capsys.readouterr().err

    def test_no_skip_passes(self, tmp_path: Path) -> None:
        """NEGATIVE WITNESS: without it, a check that ALWAYS reddens would pass."""
        report = _report(tmp_path, _passed("ok"), _passed("also_ok"))
        assert main(["prog", str(report)]) == 0

    def test_an_unrelated_skip_is_left_alone(self, tmp_path: Path) -> None:
        """A legitimate skip — no GPU, platform — is none of its business.

        Without this bound, the guardrail would become "no skip anywhere" and would
        redden for reasons that have nothing to do with coverage.
        """
        report = _report(tmp_path, _skipped("no_gpu", "embedding service unavailable"))
        assert main(["prog", str(report)]) == 0


class TestItFailsClosed:
    def test_a_missing_report_fails(self, tmp_path: Path, capsys) -> None:
        """A missing report = we do NOT KNOW whether the tests ran.

        Passing here would make the check hollow in exactly the case where pytest
        collapsed before writing its report.
        """
        assert main(["prog", str(tmp_path / "absent.xml")]) == 1
        assert "cannot prove" in capsys.readouterr().err

    def test_wrong_usage_is_not_a_pass(self, tmp_path: Path) -> None:
        assert main(["prog"]) == 2


class TestItNamesWhatItFound:
    def test_it_lists_the_skipped_tests(self, tmp_path: Path, capsys) -> None:
        report = _report(tmp_path, _skipped("needs_db", _DB_SKIP))
        main(["prog", str(report)])
        assert "tests.unit.t::needs_db" in capsys.readouterr().err

    def test_a_long_list_declares_what_it_truncates(self, tmp_path: Path, capsys) -> None:
        """NO SILENT CAP: a list truncated without its remainder lies."""
        report = _report(tmp_path, *(_skipped(f"t{i}", _DB_SKIP) for i in range(25)))
        main(["prog", str(report)])
        assert "... and 15 more" in capsys.readouterr().err


def test_the_parser_reads_the_marker_not_the_wording(tmp_path: Path) -> None:
    """It recognises the skip by the VARIABLE NAME, the only stable string.

    `require_test_db_url()`'s wording can be rewritten; the name of the variable it
    cites cannot — that is the contract between the guard and this check.
    """
    report = _report(tmp_path, _skipped("x", "please export BRAIN_V42_TEST_DB_URL first"))
    assert db_skipped_tests(report) == ["tests.unit.t::x"]
