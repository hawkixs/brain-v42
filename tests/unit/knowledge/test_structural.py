"""Structural facts terminate entries without interpreting their prose."""

import unittest
from datetime import UTC, date, datetime

from brain_v42.knowledge.decide import decide
from brain_v42.knowledge.ranks import Polarity, State, ValidFromPrecision
from brain_v42.knowledge.structural import structural_observations

DAY = date(2026, 10, 1)
AT = datetime(2026, 10, 1, 12, tzinfo=UTC)


class StructuralTests(unittest.TestCase):
    def test_merge_ends_any_table(self) -> None:
        for table in ("learnings", "decisions", "snippets", "runbooks", "adrs"):
            with self.subTest(table=table):
                observations = structural_observations(table, {"id": "old", "merged_into": "new"})
                self.assertEqual(len(observations), 1)
                observation = observations[0]
                self.assertEqual(
                    (
                        observation.rule,
                        observation.polarity,
                        observation.source_kind,
                        observation.source_rank,
                    ),
                    ("merged", Polarity.ENDS, "structure", 7),
                )
                self.assertIsNone(observation.assertion_key)
                self.assertEqual(observation.observed_value, "new")
                self.assertEqual(decide(observations, DAY).state, State.OBSOLETE)

    def test_merge_date_comes_from_freshness_change(self) -> None:
        observation = structural_observations(
            "learnings", {"merged_into": "new", "freshness_status_updated_at": AT}
        )[0]
        self.assertEqual(
            (observation.valid_from, observation.valid_from_precision),
            (DAY, ValidFromPrecision.EXACT),
        )

    def test_generic_update_does_not_date_a_merge(self) -> None:
        observation = structural_observations(
            "learnings", {"merged_into": "new", "updated_at": AT}
        )[0]
        self.assertEqual(
            (observation.valid_from, observation.valid_from_precision),
            (None, ValidFromPrecision.UNKNOWN),
        )

    def test_superseded_decision_requires_existing_successor(self) -> None:
        observations = structural_observations(
            "decisions",
            {
                "status": "superseded",
                "superseded_by": "new",
                "successor_exists": True,
                "successor_created_at": AT,
            },
        )
        self.assertEqual(observations[0].rule, "superseded_decision")
        self.assertEqual(
            (observations[0].valid_from, observations[0].valid_from_precision),
            (DAY, ValidFromPrecision.EXACT),
        )

    def test_supersession_guards(self) -> None:
        for table in ("decisions", "adrs"):
            for row in (
                {"status": "superseded"},
                {"status": "superseded", "superseded_by": 5},
                {"status": "superseded", "superseded_by": 5, "successor_exists": False},
                {"status": "active", "superseded_by": 5, "successor_exists": True},
            ):
                with self.subTest(table=table, row=row):
                    self.assertEqual(structural_observations(table, row), [])

    def test_superseded_adr_keeps_numeric_successor(self) -> None:
        observation = structural_observations(
            "adrs", {"status": "superseded", "superseded_by": 34, "successor_exists": True}
        )[0]
        self.assertEqual((observation.rule, observation.observed_value), ("superseded_adr", 34))
        self.assertEqual(observation.valid_from_precision, ValidFromPrecision.UNKNOWN)

    def test_deprecated_adr_ends_without_successor(self) -> None:
        observation = structural_observations(
            "adrs", {"status": "deprecated", "freshness_status_updated_at": AT}
        )[0]
        self.assertEqual(
            (observation.rule, observation.polarity), ("deprecated_adr", Polarity.ENDS)
        )
        self.assertEqual(observation.valid_from, DAY)

    def test_deprecation_does_not_invent_date(self) -> None:
        observation = structural_observations("adrs", {"status": "deprecated", "updated_at": AT})[0]
        self.assertEqual(
            (observation.valid_from, observation.valid_from_precision),
            (None, ValidFromPrecision.UNKNOWN),
        )

    def test_non_adr_deprecation_does_not_end(self) -> None:
        self.assertEqual(structural_observations("decisions", {"status": "deprecated"}), [])

    def test_archival_is_decay_not_a_structural_end(self) -> None:
        observations = structural_observations(
            "learnings", {"freshness_status": "archived", "freshness_status_updated_at": AT}
        )
        observation = observations[0]
        self.assertEqual(
            (
                observation.rule,
                observation.polarity,
                observation.source_kind,
                observation.source_rank,
            ),
            ("decay_archived", Polarity.CONTRADICTS, "decay", 1),
        )
        self.assertEqual(
            (observation.valid_from, observation.valid_from_precision),
            (DAY, ValidFromPrecision.EXACT),
        )
        self.assertEqual(decide(observations, DAY).state, State.UNVERIFIABLE)

    def test_merged_archival_does_not_emit_decay(self) -> None:
        observations = structural_observations(
            "learnings", {"merged_into": "new", "freshness_status": "archived"}
        )
        self.assertEqual([observation.rule for observation in observations], ["merged"])

    def test_empty_or_fresh_row_has_no_observation(self) -> None:
        for row in ({}, {"freshness_status": "fresh"}):
            with self.subTest(row=row):
                self.assertEqual(structural_observations("learnings", row), [])

    def test_structural_ids_are_stable_and_scoped_to_entry(self) -> None:
        row = {"id": "old", "merged_into": "new"}
        first = structural_observations("learnings", row)
        self.assertEqual(first, structural_observations("learnings", row))
        self.assertNotEqual(
            first[0].id, structural_observations("learnings", {**row, "id": "other"})[0].id
        )
