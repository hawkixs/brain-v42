"""`extract` has a standby model too, and a night served by it must say so.

Residue of `e7006388`, ticket `455e5bf7`. Measured 2026-09-03: `ticket_extract`
DOES carry a fallback -- `BRAIN_NVIDIA_FALLBACK_MODEL`, defaulting to
`mistralai/mistral-nemotron` -- and it has used it. Eight nights of
`logs/dream/*_extract.log` carry `bascule sur …`, 2026-08-13 through 08-21.

What `dream_runs` recorded on three of those nights:

    2026-08-13 | done | model=NULL | error_message=NULL
    2026-08-16 | done | model=NULL | error_message=NULL
    2026-08-17 | done | model=NULL | error_message=NULL

Silent. The same shape roadmap had before its mark was wired: the run succeeds on
the standby, `status` reads `done`, and the morning rubric says nothing on
exactly the night it should speak.

One difference from roadmap, and it is not cosmetic. Roadmap falls back when the
primary does not ANSWER -- a timeout, which may pass. Extract falls back when the
primary is WITHDRAWN, HTTP 404/410: the model is gone and will not come back, and
the run is living on a substitute until somebody changes the configuration. If
anything that deserves the rubric more, not less.

The mark reuses the shared prefix and the stored form the other rail already
uses: `DÉGRADÉ …` in `error_message`, no `! ` (that belongs to the journal alone
-- `4add5dd`), and `status` left at `done`, since a successful fallback broke
nothing.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts" / "dream"))

import post_run_alert  # noqa: E402

from brain_v42.dream_degradation import DEGRADED_PREFIX  # noqa: E402
from brain_v42.scripts.ticket_extract import _degradation_notice  # noqa: E402

WITHDRAWN = "deepseek-ai/deepseek-v4-pro"
STANDBY = "meta/llama-3.3-70b-instruct"


class TestItSpeaksOnlyWhenTheStandbyServed:
    def test_a_nominal_run_produces_no_notice(self) -> None:
        """An alarm that fires every night stops being read (Dream postmortem 08-04)."""
        assert (
            _degradation_notice(
                primary=WITHDRAWN, fallback=STANDBY, switched=False, scanned=19, cause="HTTP 410"
            )
            is None
        )

    def test_a_run_with_no_fallback_configured_produces_no_notice(self) -> None:
        assert (
            _degradation_notice(
                primary=WITHDRAWN, fallback=None, switched=False, scanned=19, cause=None
            )
            is None
        )

    def test_a_run_served_by_the_standby_produces_one(self) -> None:
        notice = _degradation_notice(
            primary=WITHDRAWN, fallback=STANDBY, switched=True, scanned=19, cause="HTTP 410"
        )
        assert notice is not None
        assert notice.startswith(DEGRADED_PREFIX)


class TestTheSentenceNamesWhatAnOperatorNeeds:
    def test_it_names_the_withdrawn_primary_and_the_standby(self) -> None:
        notice = _degradation_notice(
            primary=WITHDRAWN, fallback=STANDBY, switched=True, scanned=19, cause="HTTP 410"
        )
        assert notice is not None
        assert WITHDRAWN in notice
        assert STANDBY in notice

    def test_it_carries_the_cause_and_says_so_when_there_is_none(self) -> None:
        with_cause = _degradation_notice(
            primary=WITHDRAWN, fallback=STANDBY, switched=True, scanned=19, cause="HTTP 410"
        )
        without = _degradation_notice(
            primary=WITHDRAWN, fallback=STANDBY, switched=True, scanned=19, cause=None
        )
        assert with_cause is not None and "HTTP 410" in with_cause
        assert without is not None and "cause non capturée" in without

    def test_it_counts_TICKETS_because_that_is_what_extract_scans(self) -> None:
        """Roadmap counts batches; extract counts tickets. Neither borrows the other's word."""
        notice = _degradation_notice(
            primary=WITHDRAWN, fallback=STANDBY, switched=True, scanned=19, cause="HTTP 410"
        )
        assert notice is not None
        assert "19 tickets" in notice
        assert "batches" not in notice


class TestTheStoredFormMatchesTheOtherRail:
    """The contract `4add5dd` pinned for roadmap, extended to the second rail."""

    def test_the_stored_sentence_never_carries_the_journals_exclamation(self) -> None:
        notice = _degradation_notice(
            primary=WITHDRAWN, fallback=STANDBY, switched=True, scanned=19, cause="HTTP 410"
        )
        assert notice is not None
        assert not notice.startswith("! ")

    def test_the_morning_reader_lists_the_phase(self) -> None:
        notice = _degradation_notice(
            primary=WITHDRAWN, fallback=STANDBY, switched=True, scanned=19, cause="HTTP 410"
        )
        (degraded,) = post_run_alert.degraded_rows(
            [
                {
                    "phase": "extract",
                    "project_key": "*",
                    "status": "done",
                    "model": STANDBY,
                    "error_message": notice,
                }
            ]
        )
        assert degraded.phase == "extract"
        assert degraded.served_model == STANDBY

    def test_the_rubric_renders_the_whole_sentence(self) -> None:
        """The structured ratio is not parsed -- the reader's regex says `batches`.

        Stated rather than hidden: the operator sees the full sentence, and
        teaching `_DEGRADED_SHAPE` the word `tickets` is a reader-side follow-up,
        outside this lot's surface.
        """
        notice = _degradation_notice(
            primary=WITHDRAWN, fallback=STANDBY, switched=True, scanned=19, cause="HTTP 410"
        )
        (degraded,) = post_run_alert.degraded_rows(
            [
                {
                    "phase": "extract",
                    "project_key": "*",
                    "status": "done",
                    "model": STANDBY,
                    "error_message": notice,
                }
            ]
        )
        block = "\n".join(post_run_alert.build_degraded_block(dt.date(2026, 8, 21), [degraded]))
        assert "extract" in block
        assert WITHDRAWN in block
        assert degraded.fallback_batches is None, "the ticket ratio is not parsed today"


class TestTheWriterCarriesItToTheColumn:
    """The helper existing proves nothing: the row is what the reader queries.

    `record_dream_run` is best-effort -- a bare `except` that prints and
    continues -- so a test asserting "no exception" would pass against a writer
    that binds nothing. This reads the bound parameters.
    """

    @staticmethod
    def _capture() -> tuple[list[dict[str, object]], object]:
        bound: list[dict[str, object]] = []

        class _Nothing:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *_exc: object) -> bool:
                return False

        class _Session:
            async def execute(self, _statement: object, parameters: dict[str, object]) -> None:
                bound.append(parameters)

            def begin(self) -> _Nothing:
                return _Nothing()

            async def __aenter__(self) -> _Session:
                return self

            async def __aexit__(self, *_exc: object) -> bool:
                return False

        return bound, (lambda: _Session())

    async def test_a_degraded_night_stores_the_mark_in_error_message(self) -> None:
        from brain_v42.scripts.ticket_extract import record_dream_run

        bound, factory = self._capture()
        notice = _degradation_notice(
            primary=WITHDRAWN, fallback=STANDBY, switched=True, scanned=19, cause="HTTP 410"
        )
        await record_dream_run(
            factory, "done", dry=False, duration_s=1.0, error=notice, model=STANDBY
        )
        stored = bound[0]["error_message"]
        assert isinstance(stored, str)
        assert stored.startswith(DEGRADED_PREFIX)
        assert not stored.startswith("! ")
        assert bound[0]["status"] == "done", "a successful fallback broke nothing"
        assert bound[0]["model"] == STANDBY, "the model recorded is the one that SERVED"

    async def test_a_nominal_night_stores_nothing_in_that_column(self) -> None:
        from brain_v42.scripts.ticket_extract import record_dream_run

        bound, factory = self._capture()
        await record_dream_run(
            factory, "done", dry=False, duration_s=1.0, error=None, model="primary"
        )
        assert bound[0]["error_message"] is None
