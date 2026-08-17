"""Unit tests for scripts.dream.dream_preflight — the Opus-phase pre-flight gate.

The gate skips synth/promote/reorg when the brain corpus is provably unchanged
since the previous dream run (~40% of nights are zero-mutation no-ops that burn
~$1 each — 2026-06-22 audit). Conservative by design: any uncertainty => run.
"""

from __future__ import annotations

import datetime as dt

from scripts.dream.dream_preflight import should_skip_opus_phases

_LAST_RUN = dt.datetime(2026, 6, 20, 4, 0, 0)


class TestShouldSkipOpusPhases:
    def test_skip_when_corpus_unchanged_since_last_run(self) -> None:
        # Latest entity mutation is BEFORE the last run -> nothing new -> skip.
        latest = dt.datetime(2026, 6, 19, 12, 0, 0)
        assert should_skip_opus_phases(latest, _LAST_RUN) is True

    def test_run_when_corpus_changed_after_last_run(self) -> None:
        # An entity changed after the last run -> there is work -> run.
        latest = dt.datetime(2026, 6, 20, 10, 0, 0)
        assert should_skip_opus_phases(latest, _LAST_RUN) is False

    def test_run_when_no_prior_run(self) -> None:
        # No previous dream run to compare against -> never skip.
        latest = dt.datetime(2026, 6, 19, 12, 0, 0)
        assert should_skip_opus_phases(latest, None) is False

    def test_run_when_latest_mutation_unknown(self) -> None:
        # Empty corpus / NULL timestamps -> cannot prove staleness -> run.
        assert should_skip_opus_phases(None, _LAST_RUN) is False

    def test_run_when_both_unknown(self) -> None:
        assert should_skip_opus_phases(None, None) is False

    def test_boundary_equal_timestamps_skips(self) -> None:
        # Nothing strictly newer than the last run -> safe to skip.
        assert should_skip_opus_phases(_LAST_RUN, _LAST_RUN) is True
