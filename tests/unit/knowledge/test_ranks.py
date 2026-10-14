"""Pin the vocabulary shared with the persistence contract."""

import unittest

from brain_v42.knowledge.ranks import (
    RANKS,
    RANKS_VERSION,
    Centrality,
    Polarity,
    State,
    Temporal,
    ValidFromPrecision,
)


class RankTests(unittest.TestCase):
    def test_source_reliability_order(self) -> None:
        self.assertEqual(RANKS_VERSION, "v1")
        self.assertEqual(
            dict(RANKS),
            {
                "structure": 7,
                "measured": 6,
                "config": 5,
                "code": 4,
                "pr": 3,
                "ticket": 3,
                "decision": 3,
                "propagation": 2,
                "decay": 1,
            },
        )

    def test_state_vocabulary(self) -> None:
        self.assertEqual(
            {state.value for state in State},
            {"valid", "contested", "obsolete", "refuted", "unverifiable"},
        )

    def test_polarity_vocabulary(self) -> None:
        self.assertEqual(
            {polarity.value for polarity in Polarity},
            {"supports", "contradicts", "ends", "none"},
        )

    def test_centrality_vocabulary(self) -> None:
        self.assertEqual({value.value for value in Centrality}, {"core", "incidental"})

    def test_temporal_vocabulary(self) -> None:
        self.assertEqual({value.value for value in Temporal}, {"transient", "durable"})

    def test_precision_vocabulary(self) -> None:
        self.assertEqual(
            {value.value for value in ValidFromPrecision},
            {"exact", "first_observed", "unknown"},
        )

    def test_enums_are_strings(self) -> None:
        for enum in (State, Polarity, Centrality, Temporal, ValidFromPrecision):
            for value in enum:
                self.assertIsInstance(value, str)

    def test_enum_string_representation_is_its_persisted_value(self) -> None:
        for enum in (State, Polarity, Centrality, Temporal, ValidFromPrecision):
            with self.subTest(enum=enum.__name__):
                for value in enum:
                    self.assertEqual(str(value), value.value)
