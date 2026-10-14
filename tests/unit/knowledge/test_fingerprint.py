"""Confirmation follows effective assertions rather than evidence row identity."""

import unittest
from dataclasses import replace
from datetime import date

from brain_v42.knowledge.decide import Decision, Observation, decide, fingerprint
from brain_v42.knowledge.ranks import Centrality, Polarity, State, Temporal

DAY = date(2026, 10, 14)


def support(key: str = "assertion") -> Observation:
    return Observation(
        id="support",
        assertion_key=key,
        polarity=Polarity.SUPPORTS,
        source_kind="measured",
        source_rank=6,
        source_ref="embedding.provider",
        centrality=Centrality.CORE,
        temporal=Temporal.DURABLE,
    )


class FingerprintTests(unittest.TestCase):
    def test_propagation_assertion_key_is_part_of_fingerprint(self) -> None:
        first = replace(
            support("a"),
            source_kind="propagation",
            source_rank=2,
            centrality=Centrality.INCIDENTAL,
            polarity=Polarity.CONTRADICTS,
            propagated_from="parent",
        )
        second = replace(first, assertion_key="b")
        decision = decide([first], DAY)
        self.assertNotEqual(fingerprint(decision, [first]), fingerprint(decision, [second]))

    def test_unchecked_assertion_key_is_part_of_fingerprint(self) -> None:
        first = replace(support("a"), polarity=Polarity.NONE)
        second = replace(first, assertion_key="b")
        decision = decide([first], DAY)
        self.assertNotEqual(fingerprint(decision, [first]), fingerprint(decision, [second]))

    def test_fingerprint_is_sha256_hex(self) -> None:
        digest = fingerprint(decide([], DAY), [])
        self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_observation_order_is_irrelevant(self) -> None:
        first, second = support("a"), replace(support("b"), id="second")
        decision = decide([first, second], DAY)
        self.assertEqual(
            fingerprint(decision, [first, second]), fingerprint(decision, [second, first])
        )

    def test_observation_ids_are_irrelevant(self) -> None:
        first = support()
        second = replace(first, id="next-day")
        self.assertEqual(
            fingerprint(decide([first], DAY), [first]),
            fingerprint(decide([second], DAY), [second]),
        )

    def test_weaker_contradiction_does_not_change_fingerprint(self) -> None:
        observation = support()
        contradiction = replace(
            observation,
            id="weak",
            source_kind="config",
            source_rank=5,
            polarity=Polarity.CONTRADICTS,
        )
        self.assertEqual(
            fingerprint(decide([observation], DAY), [observation]),
            fingerprint(decide([observation, contradiction], DAY), [observation, contradiction]),
        )

    def test_changed_assertion_key_changes_fingerprint(self) -> None:
        first, second = support("a"), support("b")
        decision = decide([first], DAY)
        self.assertNotEqual(fingerprint(decision, [first]), fingerprint(decision, [second]))

    def test_changed_effective_polarity_changes_fingerprint(self) -> None:
        observation = support()
        contradiction = replace(observation, polarity=Polarity.CONTRADICTS)
        decision = Decision(State.VALID, "core_checked", ())
        self.assertNotEqual(
            fingerprint(decision, [observation]), fingerprint(decision, [contradiction])
        )

    def test_a_tie_differs_from_a_contradiction(self) -> None:
        observation = support()
        contradiction = replace(observation, id="contradiction", polarity=Polarity.CONTRADICTS)
        decision = Decision(State.CONTESTED, "tied", ())
        self.assertNotEqual(
            fingerprint(decision, [observation, contradiction]),
            fingerprint(decision, [contradiction]),
        )

    def test_decision_durable_exemption_cannot_change_fingerprint(self) -> None:
        observation = replace(support(), source_kind="pr", source_rank=3)
        exempt = replace(
            observation, id="exempt", source_kind="decision", polarity=Polarity.CONTRADICTS
        )
        self.assertEqual(
            fingerprint(decide([observation], DAY), [observation]),
            fingerprint(decide([observation, exempt], DAY), [observation, exempt]),
        )

    def test_changed_state_changes_fingerprint(self) -> None:
        decision = decide([], DAY)
        self.assertNotEqual(
            fingerprint(decision, []), fingerprint(replace(decision, state=State.CONTESTED), [])
        )

    def test_changed_reason_changes_fingerprint(self) -> None:
        decision = decide([], DAY)
        self.assertNotEqual(
            fingerprint(decision, []), fingerprint(replace(decision, reason="core_checked"), [])
        )
