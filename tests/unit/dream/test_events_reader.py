"""Both dream wire dialects normalise to the same ``ToolCall`` shape.

The dream pool has written two incompatible event shapes to
``logs/dream/*.events.jsonl`` since the codex/agy rail split: the codex rail
(``codex exec --json``, live default) emits ``item.started``/``item.completed``
records, while the agy rail (``agy --output-format stream-json``, older/
fallback path) emits ``step_update`` records with the tool output nested
under ``tool_info.output``. A census that reads only one shape silently drops
every call written by the other rail — this module's fixtures pin that both
shapes normalise identically for the SAME logical call, so a regression in
either branch of the reader shows up here rather than in a night's totals.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from scripts.dream import events_reader
from scripts.dream.events_reader import (
    AGY,
    CODEX,
    PRE_POOL_PROJECT,
    ToolCall,
    UnknownDialectError,
    _Counts,
    detect_dialect,
    emptiness_unknown,
    format_census_report,
    is_empty_search,
    iter_tool_calls,
    run_census,
)

_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "dream_events"
_AGY_FILE = _FIXTURES / "2026-08-12_demo-project_scan.events.jsonl"
_CODEX_FILE = _FIXTURES / "2026-09-05_demo-project_scan.events.jsonl"
_CODEX_CONNECT_FILE = _FIXTURES / "2026-09-05_demo-project_connect.events.jsonl"
_UNKNOWN_FILE = _FIXTURES / "2026-09-05_demo-project_mystery.events.jsonl"
_EMPTY_FILE = _FIXTURES / "2026-09-05_demo-project_empty.events.jsonl"
#: Pre-pool naming shape (``<date>_<phase>.events.jsonl``), still on disk for
#: 165 of 1684 real files across 28 nights starting 2026-07-13.
_LEGACY_FILE = _FIXTURES / "2026-07-20_scan.events.jsonl"
#: Matches neither the pool-era nor the pre-pool naming convention.
_UNNAMED_FILE = _FIXTURES / "2026-09-04_totallybogus.events.jsonl"
#: First line is truncated, invalid JSON.
_MALFORMED_FILE = _FIXTURES / "2026-07-21_demo-project_scan.events.jsonl"
#: Codex ``item.completed`` records with ``status: "failed"`` and a null
#: ``error`` -- the real corpus shape (105/22719 real codex mcp_tool_call
#: items, 103 with a null error) that the ``error is not None`` check alone
#: cannot see.
_CODEX_FAILED_STATUS_FILE = _FIXTURES / "2026-09-06_demo-project_promote.events.jsonl"

_SEARCH_OUTPUT = (
    '## 2 results for "clean phase status" (across all types)\n\n'
    "### Learnings (2)\n"
    "[s:0.91] 1. **Fixture learning** [low] (id:11111111-1111-1111-1111-111111111111)\n"
    "   project:demo-project | tags: dream:generated"
)
_EMPTY_OUTPUT = '## 0 results for "nonexistent fixture query" (across all types)'


def test_agy_fixture_parses_as_the_agy_dialect() -> None:
    calls = list(iter_tool_calls(_AGY_FILE))
    assert calls
    assert all(call.dialect == AGY for call in calls)


def test_codex_fixture_parses_as_the_codex_dialect() -> None:
    calls = list(iter_tool_calls(_CODEX_FILE))
    assert calls
    assert all(call.dialect == CODEX for call in calls)


def test_both_dialects_normalise_the_same_logical_search_call_identically() -> None:
    """The non-empty ``brain_search`` call is byte-identical across dialects, dialect field excepted.

    Asserting via ``dataclasses.replace`` (rather than a hand-picked field
    list) means any future :class:`ToolCall` field is covered by
    construction: a new field that diverges between rails fails this test
    even if nobody remembers to add an assertion for it.
    """
    agy_call = next(
        c for c in iter_tool_calls(_AGY_FILE) if c.tool == "brain_search" and not is_empty_search(c)
    )
    codex_call = next(
        c
        for c in iter_tool_calls(_CODEX_FILE)
        if c.tool == "brain_search" and not is_empty_search(c)
    )

    assert dataclasses.replace(agy_call, dialect=CODEX) == codex_call
    assert agy_call.dialect == AGY
    assert codex_call.dialect == CODEX
    assert agy_call.output == codex_call.output == _SEARCH_OUTPUT


def test_empty_search_detected_identically_on_both_dialects() -> None:
    agy_empty = next(c for c in iter_tool_calls(_AGY_FILE) if is_empty_search_candidate(c))
    codex_empty = next(c for c in iter_tool_calls(_CODEX_FILE) if is_empty_search_candidate(c))

    assert agy_empty.output == codex_empty.output == _EMPTY_OUTPUT
    assert is_empty_search(agy_empty) is True
    assert is_empty_search(codex_empty) is True


def is_empty_search_candidate(call) -> bool:  # noqa: ANN001 - tiny local helper
    return call.tool == "brain_search" and call.output == _EMPTY_OUTPUT


def test_is_empty_search_rejects_non_search_tools() -> None:
    non_search_calls = [c for c in iter_tool_calls(_AGY_FILE) if c.tool != "brain_search"]
    assert non_search_calls
    for call in non_search_calls:
        assert is_empty_search(call) is False


def test_is_empty_search_rejects_error_results() -> None:
    error_calls = [c for c in iter_tool_calls(_AGY_FILE) if c.is_error]
    assert error_calls
    for call in error_calls:
        assert is_empty_search(call) is False


def test_agy_error_call_is_flagged_and_carries_the_validation_message() -> None:
    error_call = next(c for c in iter_tool_calls(_AGY_FILE) if c.tool == "brain_list")
    assert error_call.is_error is True
    assert error_call.output is not None
    assert "Missing required argument" in error_call.output


def test_codex_error_call_is_flagged_with_no_result_payload() -> None:
    error_call = next(c for c in iter_tool_calls(_CODEX_FILE) if c.tool == "list_mcp_resources")
    assert error_call.is_error is True
    assert error_call.output == "resources/list failed: unknown MCP server 'brain_v42'"


def test_unknown_dialect_file_raises_a_named_error() -> None:
    with pytest.raises(UnknownDialectError):
        list(iter_tool_calls(_UNKNOWN_FILE))


def test_empty_file_raises_a_named_error_rather_than_a_silent_zero() -> None:
    with pytest.raises(UnknownDialectError):
        list(iter_tool_calls(_EMPTY_FILE))


def test_iter_tool_calls_raises_eagerly_not_only_when_the_iterator_is_advanced() -> None:
    """``iter_tool_calls`` must not be a generator function.

    A generator function only starts executing its body -- and so only
    raises -- once its returned iterator is first advanced. A caller
    wrapping the call itself in ``try/except UnknownDialectError`` (rather
    than the eventual ``list(...)``/``for`` loop) must see the error here.
    """
    with pytest.raises(UnknownDialectError):
        iter_tool_calls(_UNKNOWN_FILE)


def test_agy_successful_search_with_no_recorded_output_is_flagged_unknown() -> None:
    """A DONE, non-error agy ``brain_search`` call can still omit ``tool_info.output``.

    Measured on the real corpus: 105 of 694 agy ``brain_search`` calls are in
    this state. Such a call must never be scored as "not empty" by
    :func:`is_empty_search` without a companion signal saying its emptiness
    was never actually measured.
    """
    call = next(
        c
        for c in iter_tool_calls(_AGY_FILE)
        if c.tool == "brain_search" and not c.is_error and c.output is None
    )
    assert emptiness_unknown(call) is True
    assert is_empty_search(call) is False


def test_emptiness_unknown_rejects_error_calls_and_non_search_tools() -> None:
    calls = list(iter_tool_calls(_AGY_FILE))
    error_call = next(c for c in calls if c.is_error)
    non_search_call = next(c for c in calls if c.tool != "brain_search")
    assert emptiness_unknown(error_call) is False
    assert emptiness_unknown(non_search_call) is False


def test_run_census_reports_unknown_emptiness_separately_from_empties() -> None:
    report = run_census(logs_dir=_FIXTURES, night="2026-08-12", tool_filter="brain_search")
    assert report.total_unknown == 1
    assert report.calls_by_phase["scan"].unknown == 1
    # The genuinely empty search is still counted as an empty, not folded
    # into "unknown".
    assert report.total_empties == 1

    text = format_census_report(report, logs_dir=_FIXTURES)
    assert "unknown=1" in text
    assert "UNMEASURED emptiness: 1 call(s)" in text


def test_run_census_zero_fills_phases_with_no_matching_calls_under_a_tool_filter() -> None:
    """A phase whose file was parsed but had zero matching calls still renders.

    Reproduces the W11-investigation confusion where the CLEAN phase vanished
    from a ``--tool brain_search`` census: here the classified ``connect``
    file has no ``brain_search`` call at all, and must still show
    ``calls=0`` rather than being absent from the report.
    """
    assert _CODEX_CONNECT_FILE.exists()

    report = run_census(logs_dir=_FIXTURES, night="2026-09-05", tool_filter="brain_search")

    assert "connect" in report.calls_by_phase
    assert report.calls_by_phase["connect"] == _Counts()

    text = format_census_report(report, logs_dir=_FIXTURES)
    assert "connect: calls=0 empties=0 unknown=0" in text


def test_run_census_loads_each_classified_file_exactly_once(monkeypatch) -> None:  # noqa: ANN001
    """``run_census`` must not parse the same file's JSON twice.

    Before the fix, ``detect_dialect(path)`` and ``iter_tool_calls(path)``
    each called the private loader independently for the very same path.
    """
    load_calls: list[Path] = []
    original_load_records = events_reader._load_records

    def counting_load_records(path: Path) -> list[dict]:  # noqa: ANN001
        load_calls.append(path)
        return original_load_records(path)

    monkeypatch.setattr(events_reader, "_load_records", counting_load_records)

    run_census(logs_dir=_FIXTURES, night="2026-08-12")

    assert load_calls == [_AGY_FILE]


def test_legacy_filename_shape_parses_the_real_phase_with_a_pre_pool_project_label() -> None:
    """The pre-pool ``<date>_<phase>.events.jsonl`` shape must not collapse to "unknown".

    165 of 1684 real files (28 nights starting 2026-07-13) predate the dream
    pool and carry no project component at all. Before this fix, such a name
    fell through ``_FILENAME_RE`` to the ``("unknown", "unknown")`` fallback
    and a census rendered it as if "unknown" were a genuinely measured
    phase — indistinguishable from a real one.
    """
    calls = list(iter_tool_calls(_LEGACY_FILE))
    assert calls
    assert all(call.phase == "scan" for call in calls)
    assert all(call.project == PRE_POOL_PROJECT for call in calls)


def test_run_census_on_a_legacy_night_names_the_real_phase_not_unknown() -> None:
    report = run_census(logs_dir=_FIXTURES, night="2026-07-20")

    assert "scan" in report.calls_by_phase
    assert report.calls_by_phase["scan"].calls == 1
    assert PRE_POOL_PROJECT in report.calls_by_project
    assert "unknown" not in report.calls_by_phase
    assert "unknown" not in report.calls_by_project

    text = format_census_report(report, logs_dir=_FIXTURES)
    assert "unknown:" not in text
    assert "scan: calls=1" in text


def test_filename_matching_neither_shape_is_reported_as_unmeasured_naming() -> None:
    """A name matching neither known convention must never fall into "unknown".

    Reproduces the naming-layer version of the module's own stated failure
    mode: a degraded reading (a fabricated "unknown" phase/project) that
    reads exactly like a real measurement. Instead it must be surfaced in
    its own rendered bucket, by name, the same way an unrecognised dialect
    already is via ``unclassified_files``.
    """
    assert _UNNAMED_FILE.exists()

    report = run_census(logs_dir=_FIXTURES, night="2026-09-04")

    assert report.files_total == 1
    assert str(_UNNAMED_FILE) in report.unnamed_files
    assert report.calls_by_phase == {}
    assert report.calls_by_project == {}
    assert report.total_calls == 0

    text = format_census_report(report, logs_dir=_FIXTURES)
    assert "UNMEASURED naming: 1 file(s)" in text
    assert str(_UNNAMED_FILE) in text
    assert "unknown:" not in text


def test_run_census_on_a_night_with_no_files_is_unmeasured_not_a_silent_zero(
    tmp_path: Path,
) -> None:
    """The lot's headline anti-silent-zero contract, previously untested.

    Nothing forced ``format_census_report``'s ``files_total == 0`` branch to
    stay in place: without this test, a refactor could delete it and the
    report would instead print ``files: 0 total`` / ``total: calls=0
    empties=0 unknown=0`` — a rendering indistinguishable from a real, idle
    night.
    """
    report = run_census(logs_dir=tmp_path, night="2099-01-01")

    assert report.files_total == 0

    text = format_census_report(report, logs_dir=tmp_path)
    assert "UNMEASURED: no events.jsonl files found" in text
    assert "files: 0 total" not in text


def test_main_exits_2_for_a_night_with_no_files(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    exit_code = events_reader.main(["--night", "2099-01-01", "--logs-dir", str(tmp_path)])

    assert exit_code == 2
    assert "UNMEASURED: no events.jsonl files found" in capsys.readouterr().out


def test_is_empty_search_detects_the_group_by_type_zero_items_rendering() -> None:
    """``brain_search(group_by_type=True)`` has a SECOND empty rendering.

    ``format_knowledge_by_type`` (formatters.py:750) renders
    ``## Everything known about "<topic>" (0 items)`` on an empty result,
    distinct from ``format_search_results``'s ``## 0 results for ...``.
    Measured on the real corpus: 5 ``brain_search`` calls used
    ``group_by_type=True``, 1 of which returned 0 items and was silently
    miscounted as "not empty" before this fix.
    """
    call = ToolCall(
        dialect=CODEX,
        phase="scan",
        project="demo-project",
        tool="brain_search",
        output='## Everything known about "backup strategy" (0 items)',
    )
    assert is_empty_search(call) is True


def test_is_empty_search_group_by_type_rendering_rejects_error_and_non_search() -> None:
    error_call = ToolCall(
        dialect=CODEX,
        phase="scan",
        project="demo-project",
        tool="brain_search",
        output='## Everything known about "backup strategy" (0 items)',
        is_error=True,
    )
    non_search_call = ToolCall(
        dialect=CODEX,
        phase="scan",
        project="demo-project",
        tool="brain_list",
        output='## Everything known about "backup strategy" (0 items)',
    )
    assert is_empty_search(error_call) is False
    assert is_empty_search(non_search_call) is False


def test_detect_dialect_returns_the_known_tag_for_each_fixture() -> None:
    assert detect_dialect(_AGY_FILE) == AGY
    assert detect_dialect(_CODEX_FILE) == CODEX


def test_detect_dialect_raises_for_a_file_matching_neither_shape() -> None:
    with pytest.raises(UnknownDialectError):
        detect_dialect(_UNKNOWN_FILE)


def test_run_census_tracks_files_by_dialect_and_unclassified_files() -> None:
    """Pins ``files_by_dialect`` and ``unclassified_files``, exercised but unasserted before this fix."""
    report = run_census(logs_dir=_FIXTURES, night="2026-09-05")

    assert report.files_by_dialect == {"codex": 2}
    assert str(_UNKNOWN_FILE) in report.unclassified_files
    assert str(_EMPTY_FILE) in report.unclassified_files

    text = format_census_report(report, logs_dir=_FIXTURES)
    assert "UNMEASURED dialect: 2 file(s)" in text
    assert str(_UNKNOWN_FILE) in text
    assert str(_EMPTY_FILE) in text


def test_run_census_isolates_a_malformed_json_line_to_its_own_file() -> None:
    """One corrupt line must cost one file, not the whole night's census.

    Before this fix, ``_load_records`` raised a bare ``ValueError`` that
    ``run_census`` did not catch (only ``UnknownDialectError`` was caught),
    so one truncated line aborted the entire night with a traceback instead
    of filing that one file under ``unclassified_files``.
    """
    report = run_census(logs_dir=_FIXTURES, night="2026-07-21")

    assert report.files_total == 1
    assert str(_MALFORMED_FILE) in report.unclassified_files
    assert "invalid JSON" in report.unclassified_files[str(_MALFORMED_FILE)]
    assert report.total_calls == 0


def test_codex_status_failed_with_null_error_is_flagged_as_an_error() -> None:
    """A codex call with ``status: "failed"`` and a null ``error`` must not read as a success.

    Measured on the real corpus (``logs/dream``, 1684 files): 105 codex
    ``mcp_tool_call`` items carry ``status == "failed"``, and only 2 of them
    also carry a non-null ``error`` -- so deriving ``is_error`` from
    ``item.error`` alone missed 103 of 105 (98%) real codex errors. The agy
    branch already consults its own status field (``state in ("DONE",
    "ERROR")``); the codex branch must do the same instead of disagreeing
    with agy on the same logical event.
    """
    call = next(
        c for c in iter_tool_calls(_CODEX_FAILED_STATUS_FILE) if c.tool == "brain_assign_domain"
    )
    assert call.is_error is True
    assert call.output == "Error calling tool 'brain_assign_domain'"


def test_codex_status_failed_search_with_null_result_is_neither_a_success_nor_unmeasured() -> None:
    """A failed ``brain_search`` with ``status: "failed"``, null ``error`` and null ``result``.

    Before the fix this normalised to ``is_error=False, output=None``, which
    made it disappear into the ``UNMEASURED emptiness`` bucket
    (:func:`emptiness_unknown`) instead of being counted as the error it
    actually is -- a second silent misclassification from the same root
    cause, one call away from a genuine "we never measured this" case.
    """
    call = next(c for c in iter_tool_calls(_CODEX_FAILED_STATUS_FILE) if c.tool == "brain_search")
    assert call.is_error is True
    assert is_empty_search(call) is False
    assert emptiness_unknown(call) is False


def test_run_census_counts_a_failed_status_null_error_codex_call_as_an_error_not_a_call_zero_fill() -> (
    None
):
    """The census must not let a ``status: "failed"``/null-``error`` call hide inside phase totals."""
    report = run_census(logs_dir=_FIXTURES, night="2026-09-06")

    assert report.calls_by_phase["promote"].calls == 2
    # Neither the assign-domain failure nor the failed search is folded into
    # empties or unknown -- both are errors, not measurements.
    assert report.calls_by_phase["promote"].empties == 0
    assert report.calls_by_phase["promote"].unknown == 0


def test_unnamed_files_message_names_the_unrecognised_phase_for_a_pre_pool_shaped_name() -> None:
    """The ``unnamed_files`` reason must say *which* check failed.

    ``2026-09-08_extract.events.jsonl`` DOES match the pre-pool
    ``<date>_<phase>.events.jsonl`` frame -- its phase segment,
    ``extract``, is simply outside the six-phase allowlist. The old
    "matches neither ... naming convention" wording was factually wrong for
    this case and would send an operator debugging a new loop phase looking
    for a malformed date or a missing project segment instead.
    """
    report = run_census(logs_dir=_FIXTURES, night="2026-09-08")

    assert report.files_total == 1
    reason = next(iter(report.unnamed_files.values()))
    assert "extract" in reason
    assert "unrecognised phase" in reason
    assert "matches neither" not in reason


def test_unnamed_files_message_keeps_the_generic_wording_when_no_frame_matches_at_all() -> None:
    """A name with no date-shaped prefix at all still gets the generic "matches neither" wording."""
    report = run_census(logs_dir=_FIXTURES, night="not-a-real-night")

    assert report.files_total == 1
    reason = next(iter(report.unnamed_files.values()))
    assert "matches neither" in reason


def test_total_line_gets_a_marker_when_files_were_excluded_from_the_totals() -> None:
    """Files filed under ``unclassified_files``/``unnamed_files`` silently shrink the ``total:`` line.

    Reproduced on night 2026-09-05: 2 of 4 files are unclassified, so the
    calls they may have carried never enter ``total_calls``. Unlike
    ``UNMEASURED emptiness``, this under-count previously had no marker
    adjacent to the greppable ``total:`` line -- the count silently read as
    a complete measurement instead of the lower bound it actually is.
    """
    report = run_census(logs_dir=_FIXTURES, night="2026-09-05")
    assert len(report.unclassified_files) == 2
    assert report.unnamed_files == {}

    text = format_census_report(report, logs_dir=_FIXTURES)
    lines = text.splitlines()
    total_index = next(i for i, line in enumerate(lines) if line.startswith("total:"))
    assert lines[total_index + 1] == (
        "UNMEASURED files: 2 file(s) excluded from the totals above (see UNMEASURED dialect/naming)"
    )


def test_total_line_has_no_marker_when_no_files_were_excluded() -> None:
    report = run_census(logs_dir=_FIXTURES, night="2026-07-20")
    assert report.unclassified_files == {}
    assert report.unnamed_files == {}

    text = format_census_report(report, logs_dir=_FIXTURES)
    assert "UNMEASURED files" not in text
