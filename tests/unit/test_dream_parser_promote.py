"""Tests for dream_parser.extract_promote_report (T10 of Dream v3 plan).

A permissive parser: returns None on missing markers or malformed JSON so
dream.sh can decide whether the absence is expected (killswitch, dry_run=0)
or an error.
"""

from __future__ import annotations

from brain_v42.metrics.dream_parser import extract_promote_report


def test_extract_happy_path() -> None:
    log = """
some log output
=== PROMOTE REPORT ===
{"dry_run": false, "candidate_id": "abc", "target_type": "adr", "target_id": "xyz"}
=== END ===
trailing output
"""
    assert extract_promote_report(log) == {
        "dry_run": False,
        "candidate_id": "abc",
        "target_type": "adr",
        "target_id": "xyz",
    }


def test_extract_missing_markers_returns_none() -> None:
    assert extract_promote_report("no report here") is None


def test_extract_malformed_json_returns_none() -> None:
    log = "=== PROMOTE REPORT ===\n{not: valid, json}\n=== END ===\n"
    assert extract_promote_report(log) is None


def test_extract_multiline_json() -> None:
    log = """
=== PROMOTE REPORT ===
{
  "dry_run": false,
  "candidate_id": "abc",
  "target_type": "skipped_dedup",
  "cosine_observed": 0.92
}
=== END ===
"""
    r = extract_promote_report(log)
    assert r is not None
    assert r["target_type"] == "skipped_dedup"
    assert r["cosine_observed"] == 0.92
