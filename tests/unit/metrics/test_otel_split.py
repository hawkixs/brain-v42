"""Tests for brain_v42.metrics.otel_split.

The splitter consumes the combined stdout produced by
`claude -p` when OTEL console exporters are enabled: report text (what
Claude printed as the final message and tool output) and OTEL JS-object
blobs (what the console exporter flushed) are interleaved on the same
stream. The splitter separates them into two strings so human-readable
logs no longer drown in telemetry noise (2026-04-09 postmortem: a
156KB synth log had 0 bytes of actual report).
"""

from __future__ import annotations

import pytest

from brain_v42.metrics.otel_split import split_log


def test_pure_report_text_stays_in_report() -> None:
    """Report text with no OTEL blocks must go entirely to the report file."""
    raw = (
        "## DREAM AGENT — SYNTH PHASE REPORT\n"
        "| Call | entity_type | Entities |\n"
        "| 1 | Decision | 36 |\n"
        "Done.\n"
    )
    report, otel = split_log(raw)
    assert report == raw
    assert otel == ""


def test_pure_otel_blob_stays_in_otel() -> None:
    """A single top-level OTEL blob must go entirely to the otel file."""
    raw = (
        "{\n"
        "  resource: {\n"
        '    "service.name": "claude-code",\n'
        "  },\n"
        '  body: "claude_code.api_request",\n'
        "}\n"
    )
    report, otel = split_log(raw)
    assert report == ""
    assert otel == raw


def test_mixed_report_and_otel_are_separated() -> None:
    """Realistic mixed stream (OTEL before + after report text) splits cleanly."""
    raw = (
        "{\n"
        '  body: "claude_code.user_prompt",\n'
        "}\n"
        "## SYNTH report\n"
        "cluster 1: brain-v42 core\n"
        "{\n"
        '  body: "claude_code.api_request",\n'
        '  input_tokens: "42",\n'
        "}\n"
        "Done.\n"
    )
    report, otel = split_log(raw)
    assert report == "## SYNTH report\ncluster 1: brain-v42 core\nDone.\n"
    assert "claude_code.user_prompt" in otel
    assert "claude_code.api_request" in otel
    assert "cluster 1" not in otel
    assert "body:" not in report


def test_nested_braces_inside_otel_block_do_not_confuse_splitter() -> None:
    """Nested `{` inside an OTEL block (indented) must not close the block."""
    raw = (
        "{\n"
        "  resource: {\n"
        "    attributes: {\n"
        '      "os.type": "linux",\n'
        "    },\n"
        "  },\n"
        '  body: "claude_code.tool_result",\n'
        "}\n"
        "report line\n"
    )
    report, otel = split_log(raw)
    assert report == "report line\n"
    assert otel.count("\n") == 8  # 8 lines of OTEL
    assert '"os.type"' in otel
    assert '"os.type"' not in report


def test_multiple_sequential_otel_blocks() -> None:
    """Back-to-back OTEL blobs both land in the otel file."""
    raw = '{\n  body: "claude_code.api_request",\n}\n{\n  body: "claude_code.tool_result",\n}\n'
    report, otel = split_log(raw)
    assert report == ""
    assert otel == raw
    assert otel.count("body:") == 2


def test_promote_report_json_body_survives_split() -> None:
    """PROMOTE agent emits a JSON body between markers. otel_split must keep it.

    Root cause of 2026-04-20 PROMOTE failures: the splitter treated any
    line that was exactly `{` as the start of an OTEL block, including the
    opening brace of the PROMOTE REPORT JSON. The closing `}` ended the
    phantom block, so the entire JSON payload ended up in .otel.log and
    the validator saw empty markers in .log.
    """
    raw = (
        "Dedup passed. Emitting dry-run report.\n"
        "\n"
        "=== PROMOTE REPORT ===\n"
        "{\n"
        '  "dry_run": true,\n'
        '  "candidate_id": "abc-123",\n'
        '  "target_type": "runbook",\n'
        '  "draft_title": "Deploy something"\n'
        "}\n"
        "=== END ===\n"
    )
    report, otel = split_log(raw)
    # The entire PROMOTE block must be in report, not otel.
    assert "=== PROMOTE REPORT ===" in report
    assert '"dry_run": true' in report
    assert '"target_type": "runbook"' in report
    assert "=== END ===" in report
    assert otel == ""


def test_otel_block_detection_distinguishes_js_from_json_syntax() -> None:
    """OTEL uses JS-object syntax (unquoted keys). JSON reports use quoted keys.

    The splitter uses this distinction to decide whether `{` on its own
    line opens a telemetry block or a report payload.
    """
    # JSON report — quoted keys — stays in report
    json_raw = '{\n  "foo": 1,\n  "bar": 2\n}\n'
    report, otel = split_log(json_raw)
    assert report == json_raw
    assert otel == ""

    # OTEL blob — unquoted keys — goes to otel
    otel_raw = "{\n  resource: { foo: 1 },\n  body: 2\n}\n"
    report, otel = split_log(otel_raw)
    assert report == ""
    assert otel == otel_raw


def test_mixed_otel_and_promote_report_splits_correctly() -> None:
    """Realistic dream.sh stream: OTEL events + PROMOTE REPORT in one stdout."""
    raw = (
        "{\n"
        '  body: "claude_code.user_prompt",\n'
        '  attributes: { "event.name": "user_prompt" },\n'
        "}\n"
        "Dedup passed. Emitting report.\n"
        "=== PROMOTE REPORT ===\n"
        "{\n"
        '  "dry_run": true,\n'
        '  "target_type": "adr"\n'
        "}\n"
        "=== END ===\n"
        "{\n"
        '  body: "claude_code.api_request",\n'
        "}\n"
    )
    report, otel = split_log(raw)
    assert "claude_code.user_prompt" in otel
    assert "claude_code.api_request" in otel
    assert "=== PROMOTE REPORT ===" in report
    assert '"target_type": "adr"' in report
    assert "=== PROMOTE REPORT" not in otel


def test_report_line_containing_brace_is_not_misidentified() -> None:
    """A report line that merely CONTAINS `{` (not equals) stays in report."""
    raw = "Here is a code example: `foo = { bar: 1 }`\nAnd a cluster theme: {mixed technology}\n"
    report, otel = split_log(raw)
    assert report == raw
    assert otel == ""


def test_empty_input_produces_empty_outputs() -> None:
    report, otel = split_log("")
    assert report == ""
    assert otel == ""


def test_unclosed_otel_block_is_captured_to_otel_to_end() -> None:
    """If an OTEL block is truncated (e.g. mid-flush at timeout), everything
    from `{` to EOF is treated as OTEL so the report file stays clean."""
    raw = (
        'Phase started\n{\n  body: "claude_code.api_request",\n  input_tokens: "12",\n'
        # No closing brace — truncated
    )
    report, otel = split_log(raw)
    assert report == "Phase started\n"
    assert "claude_code.api_request" in otel


def test_split_log_cli_writes_two_files(tmp_path) -> None:
    """The `split_log_cli` helper must read raw path and write two files."""
    from brain_v42.metrics.otel_split import split_log_cli

    raw_path = tmp_path / "raw.log"
    report_path = tmp_path / "report.log"
    otel_path = tmp_path / "otel.log"

    raw_path.write_text('Report header\n{\n  body: "claude_code.api_request",\n}\nReport footer\n')

    split_log_cli(str(raw_path), str(report_path), str(otel_path))

    assert report_path.read_text() == "Report header\nReport footer\n"
    assert "claude_code.api_request" in otel_path.read_text()


def test_split_log_cli_missing_input_creates_empty_outputs(tmp_path) -> None:
    """If the raw file does not exist, the CLI must still produce empty
    output files so downstream tools do not crash on ENOENT."""
    from brain_v42.metrics.otel_split import split_log_cli

    raw_path = tmp_path / "does_not_exist.log"
    report_path = tmp_path / "report.log"
    otel_path = tmp_path / "otel.log"

    split_log_cli(str(raw_path), str(report_path), str(otel_path))

    assert report_path.exists()
    assert otel_path.exists()
    assert report_path.read_text() == ""
    assert otel_path.read_text() == ""


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
