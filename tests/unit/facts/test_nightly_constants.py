"""Issuer and idempotency-key formats for the nightly claim-verification step.

Module constants only (ADR 27 lot C, T1.3): the orchestrator, the CLI and the
`claims_verification_last_night` probe (PR 3) all import these instead of
re-deriving the format, so the fact's exact-match query stays exact (spec §7.2).
"""

from __future__ import annotations

from datetime import date

from brain_v42.facts.nightly import ISSUER_PREFIX, KEY_PREFIX, issuer_for, key_for
from brain_v42.models.claim_verdict import validate_caller_string

_MAX_POSTGRES_SERIAL_ID = 2**31 - 1


def test_issuer_prefix_and_key_prefix_are_the_documented_literals() -> None:
    assert ISSUER_PREFIX == "dream:verify:"
    assert KEY_PREFIX == "dream-verify:v1:"


def test_issuer_for_builds_the_dream_verify_issuer() -> None:
    assert issuer_for(812) == "dream:verify:812"


def test_issuer_for_the_largest_run_id_passes_validate_caller_string() -> None:
    issuer = issuer_for(_MAX_POSTGRES_SERIAL_ID)
    assert validate_caller_string(issuer) == issuer


def test_key_for_formats_the_run_date_as_yyyy_mm_dd() -> None:
    assert key_for(date(2026, 9, 27)) == "dream-verify:v1:2026-09-27"


def test_key_for_passes_validate_caller_string() -> None:
    key = key_for(date(2026, 9, 27))
    assert validate_caller_string(key) == key
