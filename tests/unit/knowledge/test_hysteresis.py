"""Count agreement over distinct days without reviving an interrupted streak."""

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date

from brain_v42.knowledge.hysteresis import EvaluationRecord, confirmed

FIRST = date(2026, 10, 12)
SECOND = date(2026, 10, 13)
THIRD = date(2026, 10, 14)


def record(day: date = THIRD) -> EvaluationRecord:
    return EvaluationRecord(
        observed_day=day,
        entry_digest="entry",
        fingerprint="candidate",
        complete=True,
        invalidated=False,
        layer="check",
    )


class HysteresisTests(unittest.TestCase):
    def test_structural_candidate_applies_immediately(self) -> None:
        self.assertTrue(confirmed([], replace(record(), layer="structural")))

    def test_structural_history_can_confirm_matching_check_agreement(self) -> None:
        structural = replace(record(SECOND), layer="structural")
        self.assertTrue(confirmed([structural], record()))

    def test_structural_disagreement_resets_check_agreement(self) -> None:
        structural = replace(record(SECOND), layer="structural", fingerprint="other")
        self.assertFalse(confirmed([record(FIRST), structural], record()))

    def test_propagation_candidate_applies_immediately(self) -> None:
        self.assertTrue(confirmed([], replace(record(), layer="propagation")))

    def test_single_check_day_is_not_confirmed(self) -> None:
        self.assertFalse(confirmed([], record()))

    def test_two_runs_on_one_day_are_not_confirmed(self) -> None:
        self.assertFalse(confirmed([record()], record()))

    def test_two_distinct_days_confirm(self) -> None:
        self.assertTrue(confirmed([record(SECOND)], record()))

    def test_candidate_already_in_history_is_not_counted_twice(self) -> None:
        candidate = record()
        self.assertFalse(confirmed([candidate, candidate], candidate))

    def test_incomplete_record_does_not_count(self) -> None:
        self.assertFalse(confirmed([replace(record(SECOND), complete=False)], record()))

    def test_incomplete_disagreement_does_not_reset(self) -> None:
        incomplete = replace(record(SECOND), complete=False, fingerprint="other")
        self.assertTrue(confirmed([record(FIRST), incomplete], record()))

    def test_incomplete_digest_change_does_not_reset(self) -> None:
        incomplete = replace(record(SECOND), complete=False, entry_digest="other")
        self.assertTrue(confirmed([record(FIRST), incomplete], record()))

    def test_incomplete_candidate_is_not_confirmed(self) -> None:
        self.assertFalse(
            confirmed([record(FIRST), record(SECOND)], replace(record(), complete=False))
        )

    def test_invalidated_record_does_not_count(self) -> None:
        self.assertFalse(confirmed([replace(record(SECOND), invalidated=True)], record()))

    def test_invalidated_disagreement_does_not_reset(self) -> None:
        invalidated = replace(record(SECOND), invalidated=True, fingerprint="other")
        self.assertTrue(confirmed([record(FIRST), invalidated], record()))

    def test_invalidated_candidate_is_not_confirmed(self) -> None:
        self.assertFalse(confirmed([record(SECOND)], replace(record(), invalidated=True)))

    def test_complete_disagreement_resets(self) -> None:
        disagreement = replace(record(SECOND), fingerprint="other")
        self.assertFalse(confirmed([record(FIRST), disagreement], record()))

    def test_digest_change_resets(self) -> None:
        changed = replace(record(SECOND), entry_digest="changed")
        self.assertFalse(confirmed([record(FIRST), changed], record()))

    def test_two_new_days_after_reset_confirm(self) -> None:
        disagreement = replace(record(FIRST), fingerprint="other")
        self.assertTrue(confirmed([disagreement, record(SECOND)], record()))

    def test_old_agreement_cannot_cross_a_new_disagreement(self) -> None:
        disagreement = replace(record(), fingerprint="other")
        self.assertFalse(confirmed([record(FIRST), record(SECOND), disagreement], record()))

    def test_propagation_does_not_confirm_check_candidates(self) -> None:
        propagation = replace(record(SECOND), layer="propagation")
        self.assertFalse(confirmed([propagation], record()))

    def test_propagation_does_not_reset_check_agreement(self) -> None:
        propagation = replace(record(SECOND), layer="propagation", fingerprint="other")
        self.assertTrue(confirmed([record(FIRST), propagation], record()))

    def test_records_are_frozen(self) -> None:
        evaluation = record()
        with self.assertRaises(FrozenInstanceError):
            evaluation.complete = False  # type: ignore[misc]
