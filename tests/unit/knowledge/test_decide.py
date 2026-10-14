"""Exercise lifecycle precedence using only evidence from one evaluation."""

import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date
from itertools import permutations

from brain_v42.knowledge.decide import RULES_VERSION, Decision, Observation, decide
from brain_v42.knowledge.ranks import (
    RANKS,
    Centrality,
    Polarity,
    State,
    Temporal,
    ValidFromPrecision,
)

DAY = date(2026, 10, 14)
ENDED = date(2026, 10, 1)


def evidence(
    id: str = "check",
    *,
    kind: str = "config",
    polarity: Polarity = Polarity.CONTRADICTS,
    key: str | None = "assertion",
    centrality: Centrality | None = Centrality.CORE,
    temporal: Temporal | None = Temporal.DURABLE,
) -> Observation:
    return Observation(
        id=id,
        assertion_key=key,
        polarity=polarity,
        source_kind=kind,
        source_rank=RANKS[kind],
        source_ref="embedding.provider",
        centrality=centrality,
        temporal=temporal,
    )


class DecisionTests(unittest.TestCase):
    def test_incidental_transient_cannot_mask_refutation_in_the_top_group(self) -> None:
        incidental = evidence("a", centrality=Centrality.INCIDENTAL, temporal=Temporal.TRANSIENT)
        core = evidence("z")
        expected = Decision(State.REFUTED, "contradicted_by_embedding.provider", ("z",))
        for observations in permutations([incidental, core]):
            with self.subTest(ids=[o.id for o in observations]):
                self.assertEqual(decide(iter(observations), DAY), expected)

    def test_refutation_reports_only_qualifying_top_group_observations(self) -> None:
        core = evidence("a")
        other_core = evidence("z")
        incidental = evidence("b", centrality=Centrality.INCIDENTAL)
        weaker = evidence("lower", kind="code")
        self.assertEqual(
            decide([core, incidental, weaker, other_core], DAY),
            Decision(State.REFUTED, "contradicted_by_embedding.provider", ("a", "z")),
        )

    def test_incidental_transient_cannot_mask_pending_end_in_the_top_group(self) -> None:
        incidental = evidence(
            "a", kind="ticket", centrality=Centrality.INCIDENTAL, temporal=Temporal.TRANSIENT
        )
        core = replace(
            evidence("z", kind="ticket", temporal=Temporal.TRANSIENT),
            valid_from=ENDED,
            valid_from_precision=ValidFromPrecision.EXACT,
        )
        expected = Decision(
            State.OBSOLETE, "pending_state_ended_ticket", ("z",), ENDED, ValidFromPrecision.EXACT
        )
        for observations in permutations([incidental, core]):
            with self.subTest(ids=[o.id for o in observations]):
                self.assertEqual(decide(iter(observations), DAY), expected)

    def test_pending_end_uses_latest_exact_date_from_qualifying_observations(self) -> None:
        first = replace(
            evidence("a", temporal=Temporal.TRANSIENT),
            valid_from=ENDED,
            valid_from_precision=ValidFromPrecision.EXACT,
        )
        latest = replace(first, id="z", valid_from=date(2026, 10, 7))
        undated = replace(first, id="b", valid_from=None)
        incidental = replace(first, id="c", centrality=Centrality.INCIDENTAL, valid_from=DAY)
        durable = replace(
            first, id="d", centrality=Centrality.INCIDENTAL, temporal=Temporal.DURABLE
        )
        weaker = replace(
            first, id="lower", source_kind="code", source_rank=RANKS["code"], valid_from=DAY
        )
        self.assertEqual(
            decide([first, latest, undated, incidental, durable, weaker], DAY),
            Decision(
                State.OBSOLETE,
                "pending_state_ended_config",
                ("a", "b", "z"),
                date(2026, 10, 7),
                ValidFromPrecision.EXACT,
            ),
        )

    def test_pending_end_ignores_exact_dates_from_nonqualifying_observations(self) -> None:
        incidental = replace(
            evidence("a", centrality=Centrality.INCIDENTAL, temporal=Temporal.TRANSIENT),
            valid_from=ENDED,
            valid_from_precision=ValidFromPrecision.EXACT,
        )
        core = evidence("z", temporal=Temporal.TRANSIENT)
        self.assertEqual(
            decide([incidental, core], DAY),
            Decision(
                State.OBSOLETE,
                "pending_state_ended_config",
                ("z",),
                DAY,
                ValidFromPrecision.FIRST_OBSERVED,
            ),
        )

    def test_contested_exemption_requires_all_top_contradictions_incidental_transient(self) -> None:
        incidental = evidence(
            "a", kind="code", centrality=Centrality.INCIDENTAL, temporal=Temporal.TRANSIENT
        )
        for centrality in (Centrality.CORE, Centrality.INCIDENTAL):
            durable = replace(incidental, id="z", centrality=centrality, temporal=Temporal.DURABLE)
            for observations in permutations([incidental, durable]):
                with self.subTest(centrality=centrality, ids=[o.id for o in observations]):
                    self.assertEqual(
                        decide(observations, DAY),
                        Decision(State.CONTESTED, "contested_by_code", ("z",)),
                    )

    def test_all_incidental_transient_top_contradictions_remain_exempt(self) -> None:
        first = evidence("a", centrality=Centrality.INCIDENTAL, temporal=Temporal.TRANSIENT)
        second = replace(first, id="z")
        self.assertEqual(
            decide([first, second], DAY), Decision(State.VALID, "declaration_only", ())
        )

    def test_propagation_cannot_carry_a_structural_rule(self) -> None:
        for polarity in Polarity:
            for rule in ("merged", "decay_archived"):
                with self.subTest(polarity=polarity, rule=rule):
                    observation = replace(
                        evidence(kind="propagation", polarity=polarity),
                        propagated_from="parent",
                        rule=rule,
                    )
                    with self.assertRaisesRegex(ValueError, "structural rule"):
                        decide([observation], DAY)

    def test_every_end_requires_a_rule_even_when_lower_ranked(self) -> None:
        valid = replace(
            evidence("valid", kind="structure", key=None, polarity=Polarity.ENDS), rule="merged"
        )
        missing_rule = evidence("missing", kind="pr", key=None, polarity=Polarity.ENDS)
        for observations in permutations([valid, missing_rule]):
            with self.subTest(ids=[o.id for o in observations]):
                with self.assertRaisesRegex(ValueError, "structural rule"):
                    decide(iter(observations), DAY)

    def test_new_row_ids_cannot_change_the_selected_reason(self) -> None:
        first = replace(evidence("a"), source_ref="first")
        second = replace(evidence("b"), source_ref="second")
        renamed = [replace(first, id="z"), replace(second, id="a")]
        self.assertEqual(decide([first, second], DAY).reason, decide(renamed, DAY).reason)

    def test_transient_contradiction_below_rank_three_only_contests(self) -> None:
        observation = replace(evidence(kind="ticket", temporal=Temporal.TRANSIENT), source_rank=2)
        self.assertEqual(decide([observation], DAY).state, State.CONTESTED)

    def test_decay_without_archival_cannot_contest(self) -> None:
        self.assertEqual(decide([evidence(kind="decay")], DAY).state, State.VALID)

    def test_propagation_contradiction_only_contests(self) -> None:
        observation = replace(
            evidence("propagation", kind="propagation", key=None, centrality=Centrality.INCIDENTAL),
            propagated_from="parent",
        )
        self.assertEqual(
            decide([observation], DAY),
            Decision(
                State.CONTESTED,
                "propagated_from_parent",
                ("propagation",),
                layer="propagation",
            ),
        )

    def test_propagation_cannot_refute_even_with_core_metadata(self) -> None:
        observation = replace(evidence(kind="propagation"), source_rank=6, propagated_from="parent")
        self.assertEqual(decide([observation], DAY).state, State.CONTESTED)

    def test_propagation_cannot_end_a_transient_assertion(self) -> None:
        observation = replace(
            evidence(kind="propagation", temporal=Temporal.TRANSIENT),
            source_rank=5,
            propagated_from="parent",
        )
        self.assertEqual(decide([observation], DAY).state, State.CONTESTED)

    def test_lifted_propagation_is_reported_in_its_own_layer(self) -> None:
        observation = replace(
            evidence(kind="propagation", key=None, polarity=Polarity.NONE),
            propagated_from="parent",
        )
        self.assertEqual(
            decide([observation], DAY),
            Decision(State.VALID, "declaration_only", (), layer="propagation"),
        )

    def test_propagation_cannot_be_mixed_with_other_layers(self) -> None:
        propagation = replace(evidence(kind="propagation"), propagated_from="parent")
        end = replace(
            evidence("end", kind="structure", key=None, polarity=Polarity.ENDS),
            rule="merged",
            valid_from=ENDED,
        )
        for other in (evidence(), evidence(kind="decay"), end):
            for observations in permutations([propagation, other]):
                with self.subTest(other=other.source_kind, ids=[o.id for o in observations]):
                    with self.assertRaisesRegex(ValueError, "Propagation.*mixed"):
                        decide(iter(observations), DAY)

    def test_propagation_requires_a_parent_for_every_polarity(self) -> None:
        for polarity in Polarity:
            with self.subTest(polarity=polarity):
                observation = evidence(kind="propagation", polarity=polarity)
                with self.assertRaisesRegex(ValueError, "propagated_from"):
                    decide([observation], DAY)

    def test_every_propagation_observation_requires_a_parent(self) -> None:
        valid = replace(evidence("valid", kind="propagation"), propagated_from="parent")
        missing_parent = evidence("missing", kind="propagation")
        for observations in permutations([valid, missing_parent]):
            with self.subTest(ids=[o.id for o in observations]):
                with self.assertRaisesRegex(ValueError, "propagated_from"):
                    decide(observations, DAY)

    def test_core_durable_rank_five_contradiction_refutes(self) -> None:
        self.assertEqual(
            decide([evidence()], DAY),
            Decision(State.REFUTED, "contradicted_by_embedding.provider", ("check",)),
        )

    def test_stronger_support_beats_rank_five_contradiction(self) -> None:
        support = evidence("support", kind="measured", polarity=Polarity.SUPPORTS)
        self.assertEqual(
            decide([evidence(), support], DAY),
            Decision(State.VALID, "core_checked", ("support",)),
        )

    def test_equal_top_support_and_contradiction_are_tied(self) -> None:
        support = evidence("support", polarity=Polarity.SUPPORTS)
        self.assertEqual(
            decide([evidence(), support], DAY),
            Decision(State.CONTESTED, "tied", ("check", "support")),
        )

    def test_stronger_contradiction_beats_support(self) -> None:
        support = evidence("support", kind="code", polarity=Polarity.SUPPORTS)
        self.assertEqual(decide([support, evidence()], DAY).state, State.REFUTED)

    def test_lower_rank_tie_does_not_overrule_stronger_support(self) -> None:
        lower_support = evidence("lower", polarity=Polarity.SUPPORTS)
        support = evidence("support", kind="measured", polarity=Polarity.SUPPORTS)
        self.assertEqual(decide([evidence(), lower_support, support], DAY).reason, "core_checked")

    def test_core_durable_rank_four_contradiction_contests(self) -> None:
        self.assertEqual(
            decide([evidence(kind="code")], DAY),
            Decision(State.CONTESTED, "contested_by_code", ("check",)),
        )

    def test_incidental_durable_contradiction_only_contests(self) -> None:
        observation = evidence(kind="measured", centrality=Centrality.INCIDENTAL)
        self.assertEqual(decide([observation], DAY).state, State.CONTESTED)

    def test_decision_durable_contradiction_cannot_contest(self) -> None:
        support = evidence("support", key="other", kind="code", polarity=Polarity.SUPPORTS)
        self.assertEqual(
            decide([evidence(kind="decision"), support], DAY),
            Decision(State.VALID, "core_checked", ("support",)),
        )

    def test_decision_durable_contradiction_cannot_tie(self) -> None:
        decision = evidence(kind="decision")
        support = evidence("support", kind="pr", polarity=Polarity.SUPPORTS)
        other = evidence("other", key="other", kind="code", polarity=Polarity.SUPPORTS)
        self.assertEqual(decide([decision, support, other], DAY).reason, "core_checked")

    def test_pr_durable_contradiction_is_not_a_decision_exemption(self) -> None:
        self.assertEqual(decide([evidence(kind="pr")], DAY).state, State.CONTESTED)

    def test_core_transient_rank_three_contradiction_ends(self) -> None:
        observation = replace(
            evidence(kind="ticket", temporal=Temporal.TRANSIENT),
            valid_from=ENDED,
            valid_from_precision=ValidFromPrecision.EXACT,
        )
        self.assertEqual(
            decide([observation], DAY),
            Decision(
                State.OBSOLETE,
                "pending_state_ended_ticket",
                ("check",),
                ENDED,
                ValidFromPrecision.EXACT,
            ),
        )

    def test_transient_end_falls_back_to_evaluation_day(self) -> None:
        observation = replace(
            evidence(kind="pr", temporal=Temporal.TRANSIENT),
            valid_from=ENDED,
            valid_from_precision=ValidFromPrecision.FIRST_OBSERVED,
        )
        decision = decide([observation], DAY)
        self.assertEqual(decision.end_date, DAY)
        self.assertEqual(decision.end_date_precision, ValidFromPrecision.FIRST_OBSERVED)

    def test_transient_unknown_end_is_dated(self) -> None:
        observation = evidence(kind="pr", temporal=Temporal.TRANSIENT)
        self.assertEqual(decide([observation], DAY).end_date, DAY)

    def test_decision_transient_contradiction_is_not_exempt(self) -> None:
        observation = evidence(kind="decision", temporal=Temporal.TRANSIENT)
        self.assertEqual(decide([observation], DAY).state, State.OBSOLETE)

    def test_incidental_transient_contradiction_changes_nothing(self) -> None:
        incidental = evidence(centrality=Centrality.INCIDENTAL, temporal=Temporal.TRANSIENT)
        support = evidence("support", key="other", kind="code", polarity=Polarity.SUPPORTS)
        self.assertEqual(
            decide([incidental, support], DAY),
            Decision(State.VALID, "core_checked", ("support",)),
        )

    def test_incidental_transient_tie_is_contested(self) -> None:
        contradiction = evidence(centrality=Centrality.INCIDENTAL, temporal=Temporal.TRANSIENT)
        support = replace(contradiction, id="support", polarity=Polarity.SUPPORTS)
        self.assertEqual(decide([contradiction, support], DAY).reason, "tied")

    def test_refutation_beats_transient_end_of_another_assertion(self) -> None:
        transient = evidence("end", key="other", kind="pr", temporal=Temporal.TRANSIENT)
        self.assertEqual(decide([transient, evidence()], DAY).state, State.REFUTED)

    def test_transient_end_beats_tie_on_another_assertion(self) -> None:
        transient = evidence("end", key="other", kind="pr", temporal=Temporal.TRANSIENT)
        support = evidence("support", polarity=Polarity.SUPPORTS)
        self.assertEqual(decide([evidence(), support, transient], DAY).state, State.OBSOLETE)

    def test_rank_four_core_support_is_checked(self) -> None:
        observation = evidence(kind="code", polarity=Polarity.SUPPORTS)
        self.assertEqual(decide([observation], DAY).reason, "core_checked")

    def test_rank_three_core_support_is_declaration_only(self) -> None:
        observation = evidence(kind="pr", polarity=Polarity.SUPPORTS)
        self.assertEqual(decide([observation], DAY).reason, "declaration_only")

    def test_incidental_support_is_declaration_only(self) -> None:
        observation = evidence(polarity=Polarity.SUPPORTS, centrality=Centrality.INCIDENTAL)
        self.assertEqual(decide([observation], DAY).reason, "declaration_only")

    def test_none_does_not_mask_a_lower_rank_contradiction(self) -> None:
        unchecked = evidence("none", kind="measured", polarity=Polarity.NONE)
        self.assertEqual(decide([unchecked, evidence()], DAY).state, State.REFUTED)

    def test_observation_order_does_not_change_decision(self) -> None:
        left = evidence("left", key="left")
        right = evidence("right", key="right", kind="code")
        self.assertEqual(decide([left, right], DAY), decide([right, left], DAY))

    def test_structural_end_uses_its_reason_and_exact_date(self) -> None:
        observation = replace(
            evidence(kind="structure", key=None, polarity=Polarity.ENDS),
            rule="merged",
            valid_from=ENDED,
            valid_from_precision=ValidFromPrecision.EXACT,
        )
        self.assertEqual(
            decide([observation], DAY),
            Decision(
                State.OBSOLETE,
                "merged",
                ("check",),
                ENDED,
                ValidFromPrecision.EXACT,
                "structural",
            ),
        )

    def test_structural_end_preserves_first_observed_precision(self) -> None:
        observation = replace(
            evidence(kind="structure", key=None, polarity=Polarity.ENDS),
            rule="superseded_decision",
            valid_from=ENDED,
            valid_from_precision=ValidFromPrecision.FIRST_OBSERVED,
        )
        decision = decide([observation], DAY)
        self.assertEqual(decision.end_date, ENDED)
        self.assertEqual(decision.end_date_precision, ValidFromPrecision.FIRST_OBSERVED)

    def test_structural_end_without_a_date_uses_the_observed_day(self) -> None:
        for precision in ValidFromPrecision:
            with self.subTest(precision=precision):
                observation = replace(
                    evidence(kind="structure", key=None, polarity=Polarity.ENDS),
                    rule="merged",
                    valid_from_precision=precision,
                )
                self.assertEqual(
                    decide([observation], DAY),
                    Decision(
                        State.OBSOLETE,
                        "merged",
                        ("check",),
                        DAY,
                        ValidFromPrecision.FIRST_OBSERVED,
                        "structural",
                    ),
                )

    def test_structural_end_requires_a_rule(self) -> None:
        observation = evidence(kind="structure", key=None, polarity=Polarity.ENDS)
        with self.assertRaisesRegex(ValueError, "structural rule"):
            decide([observation], DAY)

    def test_structural_ends_choose_highest_rank_then_lowest_id(self) -> None:
        selected = replace(
            evidence("b", kind="structure", key=None, polarity=Polarity.ENDS),
            source_ref="z",
            rule="superseded_adr",
            valid_from=ENDED,
            valid_from_precision=ValidFromPrecision.EXACT,
        )
        tied = replace(selected, id="c", source_ref="a", rule="merged", valid_from=DAY)
        weaker = replace(selected, id="a", source_kind="pr", source_rank=RANKS["pr"])
        expected = Decision(
            State.OBSOLETE,
            "superseded_adr",
            ("b",),
            ENDED,
            ValidFromPrecision.EXACT,
            "structural",
        )
        for observations in permutations([weaker, tied, selected]):
            with self.subTest(ids=[o.id for o in observations]):
                self.assertEqual(decide(observations, DAY), expected)

    def test_superseded_adr_reason_is_preserved(self) -> None:
        observation = replace(
            evidence(kind="structure", key=None, polarity=Polarity.ENDS),
            rule="superseded_adr",
            valid_from=ENDED,
        )
        self.assertEqual(decide([observation], DAY).reason, "superseded_adr")

    def test_deprecated_adr_reason_is_preserved(self) -> None:
        observation = replace(
            evidence(kind="structure", key=None, polarity=Polarity.ENDS),
            rule="deprecated_adr",
            valid_from=ENDED,
        )
        self.assertEqual(decide([observation], DAY).reason, "deprecated_adr")

    def test_structural_end_beats_refutation(self) -> None:
        end = replace(
            evidence("end", kind="structure", key=None, polarity=Polarity.ENDS),
            rule="merged",
            valid_from=ENDED,
        )
        self.assertEqual(decide([evidence(), end], DAY).observation_ids, ("end",))

    def test_decay_is_unverifiable_instead_of_contested(self) -> None:
        decay = replace(
            evidence("decay", kind="decay", key=None, polarity=Polarity.NONE),
            rule="decay_archived",
        )
        self.assertEqual(
            decide([decay], DAY),
            Decision(State.UNVERIFIABLE, "decay_archived", ("decay",), layer="structural"),
        )

    def test_decay_has_no_support_exemption(self) -> None:
        decay = replace(
            evidence("decay", kind="decay", key=None, polarity=Polarity.NONE),
            rule="decay_archived",
        )
        support = evidence("support", kind="measured", polarity=Polarity.SUPPORTS)
        self.assertEqual(decide([support, decay], DAY).state, State.UNVERIFIABLE)

    def test_decay_archival_beats_refutation_grade_contradiction(self) -> None:
        decay = replace(
            evidence("decay", kind="decay", key=None, polarity=Polarity.NONE),
            rule="decay_archived",
        )
        self.assertEqual(
            decide([evidence(), decay], DAY),
            Decision(State.UNVERIFIABLE, "decay_archived", ("decay",), layer="structural"),
        )

    def test_structural_end_beats_decay(self) -> None:
        end = replace(
            evidence("end", kind="structure", key=None, polarity=Polarity.ENDS),
            rule="merged",
            valid_from=ENDED,
        )
        decay = replace(evidence(kind="decay", key=None), rule="decay_archived")
        self.assertEqual(decide([decay, end], DAY).state, State.OBSOLETE)

    def test_no_evidence_is_declaration_only(self) -> None:
        self.assertEqual(
            decide([], DAY),
            Decision(State.VALID, "declaration_only", (), layer="check"),
        )

    def test_rules_version_is_pinned(self) -> None:
        self.assertEqual(RULES_VERSION, "v1")

    def test_observation_is_frozen(self) -> None:
        observation = evidence()
        with self.assertRaises(FrozenInstanceError):
            observation.id = "changed"  # type: ignore[misc]

    def test_decision_is_frozen(self) -> None:
        decision = decide([], DAY)
        with self.assertRaises(FrozenInstanceError):
            decision.reason = "changed"  # type: ignore[misc]
