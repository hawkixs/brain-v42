"""Compare typed claims deterministically, after provenance guards."""

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime

from brain_v42.knowledge.checks import Sources, check
from brain_v42.knowledge.decide import Observation, decide
from brain_v42.knowledge.facts_registry import REGISTRY
from brain_v42.knowledge.ranks import Centrality, Polarity, State, Temporal, ValidFromPrecision

TEXT = "The server exists. The PR is open. Embedding is local. The ticket is open."
DAY = date(2026, 10, 1)
AT = datetime(2026, 10, 1, 12, tzinfo=UTC)


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = Sources(
            tree=frozenset({"src/brain_v42/mcp/server.py"}),
            identifiers=frozenset({"Server"}),
            prs={42: ("merged", AT)},
            tickets={"abcdef12": ("resolved", AT)},
            facts={"embedding.provider": "mistral-api", "release.live_sha": "abc123"},
            registry=REGISTRY,
            origin_main_sha="snapshot-sha",
        )
        self.assertion: dict[str, object] = {
            "quote": "The server exists.",
            "check": "code_path",
            "repo": "brain-v42",
            "ref": "mcp/server.py",
            "asserts": "exists",
            "centrality": "core",
            "temporal": "durable",
        }

    def evaluate(self, **fields: object) -> Observation | None:
        return check({**self.assertion, **fields}, self.sources, TEXT, entry_id="entry")

    def observation(self, **fields: object) -> Observation:
        result = self.evaluate(**fields)
        self.assertIsNotNone(result)
        assert result is not None
        return result

    def test_unquoted_assertion_is_dropped(self) -> None:
        self.assertIsNone(self.evaluate(quote="Invented quotation"))

    def test_quote_normalizes_case_and_whitespace(self) -> None:
        self.assertEqual(
            self.observation(quote="THE  server\n EXISTS.").polarity, Polarity.SUPPORTS
        )

    def test_empty_quote_is_dropped(self) -> None:
        self.assertIsNone(self.evaluate(quote="  "))

    def test_repo_guard_never_creates_absence_contradiction(self) -> None:
        for kind in ("code_path", "code_symbol", "pr"):
            for repo in (None, "other"):
                with self.subTest(kind=kind, repo=repo):
                    self.assertIsNone(
                        self.evaluate(
                            check=kind,
                            repo=repo,
                            ref=42 if kind == "pr" else "missing",
                            claimed_status="open",
                        )
                    )

    def test_existing_path_supports_existence(self) -> None:
        observation = self.observation()
        self.assertEqual(
            (
                observation.polarity,
                observation.source_kind,
                observation.source_rank,
                observation.observed_value,
            ),
            (Polarity.SUPPORTS, "code", 4, "exists"),
        )
        self.assertIn("snapshot-sha", observation.source_ref)

    def test_missing_path_contradicts_existence(self) -> None:
        self.assertEqual(self.observation(ref="missing.py").polarity, Polarity.CONTRADICTS)

    def test_missing_path_supports_absence(self) -> None:
        self.assertEqual(
            self.observation(ref="missing.py", asserts="absent").polarity, Polarity.SUPPORTS
        )

    def test_existing_path_contradicts_absence(self) -> None:
        self.assertEqual(self.observation(asserts="absent").polarity, Polarity.CONTRADICTS)

    def test_directory_suffix_counts_as_present(self) -> None:
        self.assertEqual(self.observation(ref="mcp/").polarity, Polarity.SUPPORTS)

    def test_file_suffix_requires_path_component_boundary(self) -> None:
        self.assertEqual(self.observation(ref="erver.py").polarity, Polarity.CONTRADICTS)

    def test_empty_path_is_unchecked(self) -> None:
        self.assertIsNone(self.evaluate(ref=""))

    def test_missing_or_none_ref_is_unchecked(self) -> None:
        for kind in ("code_path", "code_symbol", "pr", "ticket"):
            with self.subTest(kind=kind):
                self.assertIsNone(self.evaluate(check=kind, ref=None, claimed_status="open"))

    def test_root_code_refs_are_unchecked(self) -> None:
        for kind in ("code_path", "code_symbol"):
            for ref in (".", "/"):
                with self.subTest(kind=kind, ref=ref):
                    self.assertIsNone(self.evaluate(check=kind, ref=ref))

    def test_trailing_dot_symbol_is_unchecked(self) -> None:
        for ref in ("Server.", "module."):
            with self.subTest(ref=ref):
                self.assertIsNone(self.evaluate(check="code_symbol", ref=ref))

    def test_invalid_existence_claim_is_unchecked(self) -> None:
        self.assertIsNone(self.evaluate(asserts="maybe"))

    def test_existing_symbol_supports_existence(self) -> None:
        self.assertEqual(
            self.observation(check="code_symbol", ref="Server").polarity, Polarity.SUPPORTS
        )

    def test_dotted_symbol_uses_last_component(self) -> None:
        self.assertEqual(
            self.observation(check="code_symbol", ref="module.Server").polarity, Polarity.SUPPORTS
        )

    def test_symbol_absence_is_inverted(self) -> None:
        self.assertEqual(
            self.observation(check="code_symbol", ref="Server", asserts="absent").polarity,
            Polarity.CONTRADICTS,
        )
        self.assertEqual(
            self.observation(check="code_symbol", ref="Missing", asserts="absent").polarity,
            Polarity.SUPPORTS,
        )

    def test_path_like_symbol_is_routed_to_tree(self) -> None:
        for ref in ("mcp/server.py", "server.py"):
            with self.subTest(ref=ref):
                self.assertEqual(
                    self.observation(check="code_symbol", ref=ref).polarity, Polarity.SUPPORTS
                )

    def test_fact_equality_supports(self) -> None:
        observation = self.observation(
            check="fact", fact_key="embedding.provider", claimed_value="mistral-api"
        )
        self.assertEqual(
            (
                observation.polarity,
                observation.source_kind,
                observation.source_rank,
                observation.observed_value,
            ),
            (Polarity.SUPPORTS, "config", 5, "mistral-api"),
        )

    def test_fact_difference_contradicts(self) -> None:
        observation = self.observation(
            check="fact", fact_key="embedding.provider", claimed_value="local-gpu-shim"
        )
        self.assertEqual(observation.polarity, Polarity.CONTRADICTS)
        self.assertEqual(decide([observation], DAY).state, State.REFUTED)

    def test_unknown_fact_key_is_unchecked(self) -> None:
        self.assertIsNone(self.evaluate(check="fact", fact_key="unknown", claimed_value="value"))

    def test_non_string_fact_claim_is_unchecked(self) -> None:
        for claimed in (None, False, 42, ["abc123"], {"sha": "abc123"}):
            with self.subTest(claimed=claimed):
                self.assertIsNone(
                    self.evaluate(check="fact", fact_key="release.live_sha", claimed_value=claimed)
                )

    def test_out_of_domain_claim_is_unchecked(self) -> None:
        self.assertIsNone(
            self.evaluate(check="fact", fact_key="embedding.provider", claimed_value="invented")
        )

    def test_unmeasurable_fact_is_unchecked(self) -> None:
        self.sources = replace(self.sources, facts={"embedding.provider": None})
        self.assertIsNone(
            self.evaluate(check="fact", fact_key="embedding.provider", claimed_value="mistral-api")
        )

    def test_out_of_domain_current_value_is_unchecked(self) -> None:
        self.sources = replace(self.sources, facts={"embedding.provider": "invented"})
        self.assertIsNone(
            self.evaluate(check="fact", fact_key="embedding.provider", claimed_value="mistral-api")
        )

    def test_open_domain_fact_uses_equality(self) -> None:
        observation = self.observation(
            check="fact", fact_key="release.live_sha", claimed_value="abc123"
        )
        self.assertEqual(
            (observation.source_kind, observation.source_rank, observation.polarity),
            ("measured", 6, Polarity.SUPPORTS),
        )
        self.assertEqual(
            self.observation(
                check="fact", fact_key="release.live_sha", claimed_value="other"
            ).polarity,
            Polarity.CONTRADICTS,
        )

    def test_decision_fact_has_decision_rank(self) -> None:
        self.sources = replace(self.sources, facts={"dream.nightly": "suspended"})
        observation = self.observation(
            check="fact", fact_key="dream.nightly", claimed_value="running"
        )
        self.assertEqual((observation.source_kind, observation.source_rank), ("decision", 3))

    def test_pr_status_comparison(self) -> None:
        for current in ("open", "merged", "closed"):
            self.sources = replace(self.sources, prs={42: (current, AT)})
            for claimed in ("open", "merged", "closed"):
                with self.subTest(current=current, claimed=claimed):
                    observation = self.observation(check="pr", ref=42, claimed_status=claimed)
                    self.assertEqual(
                        observation.polarity,
                        Polarity.SUPPORTS if claimed == current else Polarity.CONTRADICTS,
                    )
                    self.assertEqual(
                        (
                            observation.source_kind,
                            observation.source_rank,
                            observation.observed_value,
                        ),
                        ("pr", 3, current),
                    )

    def test_pr_end_date_is_exact(self) -> None:
        observation = self.observation(
            check="pr", ref="#42", claimed_status="open", temporal="transient"
        )
        self.assertEqual(
            (observation.valid_from, observation.valid_from_precision),
            (DAY, ValidFromPrecision.EXACT),
        )
        self.assertEqual(decide([observation], DAY).state, State.OBSOLETE)

    def test_open_pr_has_no_end_date(self) -> None:
        self.sources = replace(self.sources, prs={42: ("open", AT)})
        observation = self.observation(check="pr", ref=42, claimed_status="open")
        self.assertEqual(
            (observation.valid_from, observation.valid_from_precision),
            (None, ValidFromPrecision.UNKNOWN),
        )

    def test_undated_closed_pr_is_unknown(self) -> None:
        self.sources = replace(self.sources, prs={42: ("closed", None)})
        observation = self.observation(check="pr", ref=42, claimed_status="open")
        self.assertEqual(observation.valid_from_precision, ValidFromPrecision.UNKNOWN)

    def test_missing_or_invalid_pr_is_unchecked(self) -> None:
        for ref in (99, "invalid", -1):
            with self.subTest(ref=ref):
                self.assertIsNone(self.evaluate(check="pr", ref=ref, claimed_status="open"))
        self.assertIsNone(self.evaluate(check="pr", ref=42, claimed_status="invalid"))

    def test_ticket_terminal_states_contradict_open(self) -> None:
        for current in ("resolved", "closed", "wontfix"):
            self.sources = replace(self.sources, tickets={"abcdef12": (current, AT)})
            with self.subTest(current=current):
                observation = self.observation(
                    check="ticket", ref="abcdef12", claimed_status="open", repo="other"
                )
                self.assertEqual(
                    (
                        observation.polarity,
                        observation.source_kind,
                        observation.source_rank,
                        observation.observed_value,
                    ),
                    (Polarity.CONTRADICTS, "ticket", 3, current),
                )

    def test_ticket_terminal_states_are_equivalent(self) -> None:
        self.assertEqual(
            self.observation(check="ticket", ref="abcdef12", claimed_status="closed").polarity,
            Polarity.SUPPORTS,
        )

    def test_open_ticket_supports_open_claim(self) -> None:
        self.sources = replace(self.sources, tickets={"abcdef12": ("open", None)})
        self.assertEqual(
            self.observation(check="ticket", ref="abcdef12", claimed_status="open").polarity,
            Polarity.SUPPORTS,
        )

    def test_open_ticket_contradicts_terminal_claim(self) -> None:
        self.sources = replace(self.sources, tickets={"abcdef12": ("open", None)})
        self.assertEqual(
            self.observation(check="ticket", ref="abcdef12", claimed_status="resolved").polarity,
            Polarity.CONTRADICTS,
        )

    def test_ticket_date_is_exact_when_supplied(self) -> None:
        observation = self.observation(check="ticket", ref="ABCDEF12", claimed_status="open")
        self.assertEqual(
            (observation.valid_from, observation.valid_from_precision),
            (DAY, ValidFromPrecision.EXACT),
        )

    def test_ticket_without_date_is_unknown(self) -> None:
        self.sources = replace(self.sources, tickets={"abcdef12": ("closed", None)})
        self.assertEqual(
            self.observation(
                check="ticket", ref="abcdef12", claimed_status="open"
            ).valid_from_precision,
            ValidFromPrecision.UNKNOWN,
        )

    def test_missing_invalid_or_unknown_ticket_is_unchecked(self) -> None:
        for ref in ("12345678", "abcdef", "abcdefgh"):
            with self.subTest(ref=ref):
                self.assertIsNone(self.evaluate(check="ticket", ref=ref, claimed_status="open"))
        self.sources = replace(self.sources, tickets={"abcdef12": ("unknown", None)})
        self.assertIsNone(self.evaluate(check="ticket", ref="abcdef12", claimed_status="open"))

    def test_unknown_check_is_unchecked(self) -> None:
        self.assertIsNone(self.evaluate(check="invented"))

    def test_assertion_key_uses_unambiguous_normalized_components(self) -> None:
        expected = hashlib.sha256(
            json.dumps(
                ["entry", "code_path", "mcp/server.py", "the server exists."], separators=(",", ":")
            ).encode()
        ).hexdigest()
        self.assertEqual(self.observation().assertion_key, expected)
        self.assertEqual(self.observation(quote="THE server\nexists.").assertion_key, expected)

    def test_assertion_key_is_entry_scoped(self) -> None:
        other = check(self.assertion, self.sources, TEXT, entry_id="other")
        assert other is not None
        self.assertNotEqual(other.assertion_key, self.observation().assertion_key)

    def test_classification_is_preserved_as_enums(self) -> None:
        observation = self.observation(centrality="incidental", temporal="transient")
        self.assertEqual(
            (observation.centrality, observation.temporal),
            (Centrality.INCIDENTAL, Temporal.TRANSIENT),
        )

    def test_invalid_classification_is_unchecked(self) -> None:
        self.assertIsNone(self.evaluate(centrality="invented"))

    def test_observation_id_distinguishes_measured_values(self) -> None:
        before = self.observation()
        self.sources = replace(self.sources, tree=frozenset())
        after = self.observation()
        self.assertEqual(before.assertion_key, after.assertion_key)
        self.assertNotEqual(before.id, after.id)

    def test_sources_are_frozen(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            self.sources.origin_main_sha = "changed"  # type: ignore[misc]
