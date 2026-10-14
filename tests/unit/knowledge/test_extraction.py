"""Exercise extraction without an SDK, credentials, or network access."""

import hashlib
import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from brain_v42.knowledge import extraction, facts_registry
from brain_v42.knowledge.checks import Sources, check

DAY = date(2026, 10, 14)


def entry(number: int, text: str = "The server exists.") -> extraction.EntryText:
    return extraction.EntryText("learning", f"{number:08x}-0000-0000-0000-000000000000", text, DAY)


class FakeClient:
    def __init__(self) -> None:
        self.messages = self
        self.batches = self
        self.calls: list[tuple[str, Any]] = []
        self.items: list[Any] = []
        self.processing_status = "ended"

    def count_tokens(self, **params: Any) -> SimpleNamespace:
        self.calls.append(("count", params))
        return SimpleNamespace(input_tokens=100)

    def create(self, **params: Any) -> SimpleNamespace:
        self.calls.append(("create", params))
        return SimpleNamespace(id="batch-1")

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        self.calls.append(("retrieve", batch_id))
        return SimpleNamespace(processing_status=self.processing_status)

    def results(self, batch_id: str) -> list[Any]:
        self.calls.append(("results", batch_id))
        return self.items


class RequestBuildingTests(unittest.TestCase):
    def test_deterministic_order_and_eight_entry_chunks(self) -> None:
        entries = [entry(i) for i in range(10)]
        manifest = extraction.build_requests("run", reversed(entries), facts_registry)
        self.assertEqual([r.custom_id for r in manifest.requests], ["run-00000", "run-00001"])
        self.assertEqual([len(r.entries) for r in manifest.requests], [8, 2])
        self.assertEqual(manifest.requests[0].entries[0].entity_id, entries[0].entity_id)
        self.assertEqual(
            extraction.manifest_digest(manifest),
            extraction.manifest_digest(extraction.build_requests("run", entries, facts_registry)),
        )
        self.assertNotEqual(
            extraction.manifest_digest(manifest),
            extraction.manifest_digest(extraction.build_requests("other", entries, facts_registry)),
        )

    def test_sent_text_digest_and_truncation(self) -> None:
        original = entry(1, "é" * 6001)
        manifest = extraction.build_requests("run", [original], facts_registry)
        payload = json.loads(manifest.requests[0].params["messages"][0]["content"])
        sent = payload["entries"][0]["text"]
        self.assertEqual(sent, "é" * 6000 + "[truncated]")
        self.assertEqual(original.digest, hashlib.sha256(sent.encode()).hexdigest())
        self.assertEqual(manifest.requests[0].entries[0].digest, original.digest)
        self.assertEqual(payload["entries"][0]["created_day"], DAY.isoformat())
        with self.assertRaises(FrozenInstanceError):
            original.text = "changed"  # type: ignore[misc]

    def test_collision_is_refused_within_request(self) -> None:
        other = extraction.EntryText("decision", entry(1).entity_id, "Other text", DAY)
        with self.assertRaisesRegex(ValueError, "collision"):
            extraction.build_requests("run", [entry(1), other], facts_registry)

    def test_prompt_only_shows_domains_and_typed_claims(self) -> None:
        params = extraction.build_requests("run", [entry(1)], facts_registry).requests[0].params
        prompt = params["system"][0]
        self.assertIn(facts_registry.prompt_sheet(), prompt["text"])
        self.assertIn(extraction.PROMPT_VERSION, prompt["text"])
        self.assertEqual(prompt["cache_control"], {"type": "ephemeral"})
        self.assertEqual(params["model"], "claude-sonnet-5-5")
        self.assertEqual(params["output_config"]["effort"], "medium")
        self.assertEqual(params["output_config"]["format"]["type"], "json_schema")
        schema = json.dumps(params["output_config"]["format"]["schema"])
        self.assertIn("fact_key", schema)
        self.assertIn("claimed_value", schema)
        self.assertNotIn("fact_verdict", schema)
        self.assertNotIn("temperature", params)

    def test_empty_manifest_has_no_requests(self) -> None:
        self.assertEqual(extraction.build_requests("run", [], facts_registry).requests, ())

    def test_invalid_run_id_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            extraction.build_requests("", [entry(1)], facts_registry)


class CostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = extraction.build_requests(
            "run", [entry(i) for i in range(9)], facts_registry
        )
        self.client = FakeClient()

    def test_counts_every_request_with_the_same_schema(self) -> None:
        counts = extraction.count(self.manifest, self.client)
        self.assertEqual(dict(counts.input_tokens), {"run-00000": 100, "run-00001": 100})
        self.assertAlmostEqual(counts.worst_case_usd, 200e-6 + 2 * extraction.MAX_TOKENS * 5e-6)
        for (_, params), request in zip(self.client.calls, self.manifest.requests, strict=True):
            self.assertEqual(params["messages"], request.params["messages"])
            self.assertEqual(params["system"], request.params["system"])
            self.assertEqual(params["output_config"], request.params["output_config"])
            self.assertNotIn("max_tokens", params)

    def test_guard_pays_for_one_ambiguous_duplicate(self) -> None:
        counts = extraction.count(self.manifest, self.client)
        with self.assertRaisesRegex(ValueError, "ceiling"):
            extraction.admit(self.manifest, counts, 2 * counts.worst_case_usd - 1e-9)
        extraction.admit(self.manifest, counts, 2 * counts.worst_case_usd)

    def test_guard_refuses_counts_for_another_manifest(self) -> None:
        counts = extraction.count(self.manifest, self.client)
        other = extraction.build_requests("other", [entry(1)], facts_registry)
        with self.assertRaises(ValueError):
            extraction.admit(other, counts, 100)

    def test_guard_refuses_nonfinite_or_negative_ceiling(self) -> None:
        counts = extraction.count(self.manifest, self.client)
        for ceiling in (float("nan"), float("inf"), -1):
            with self.subTest(ceiling=ceiling), self.assertRaises(ValueError):
                extraction.admit(self.manifest, counts, ceiling)

    def test_submit_creates_one_batch(self) -> None:
        self.assertEqual(extraction.submit(self.manifest, self.client), "batch-1")
        self.assertEqual(len(self.client.calls), 1)
        requests = self.client.calls[0][1]["requests"]
        self.assertEqual([r["custom_id"] for r in requests], ["run-00000", "run-00001"])
        self.assertEqual(requests[0]["params"], self.manifest.requests[0].params)

    def test_empty_manifest_is_not_submitted(self) -> None:
        empty = extraction.build_requests("run", [], facts_registry)
        with self.assertRaises(ValueError):
            extraction.submit(empty, self.client)
        self.assertEqual(self.client.calls, [])


class ScreeningTests(unittest.TestCase):
    def test_safe_text_passes(self) -> None:
        self.assertTrue(
            extraction.screen("The server exists. Credentials come from the environment.")
        )

    def test_secret_like_text_is_never_sent_even_past_truncation(self) -> None:
        secret_like = "-----BEGIN PRIVATE KEY-----"
        unsafe = entry(2, "a" * 6100 + secret_like)
        self.assertFalse(extraction.screen(unsafe.text))
        manifest = extraction.build_requests("run", [unsafe, entry(1)], facts_registry)
        self.assertEqual(len(manifest.requests[0].entries), 1)
        self.assertEqual(manifest.screened[0].entity_id, unsafe.entity_id)
        self.assertNotIn(secret_like, repr(manifest))
        client = FakeClient()
        extraction.count(manifest, client)
        extraction.submit(manifest, client)
        self.assertNotIn(secret_like, repr(client.calls))

    def test_all_screened_entries_produce_no_requests(self) -> None:
        manifest = extraction.build_requests(
            "run", [entry(1, "-----BEGIN RSA PRIVATE KEY-----")], facts_registry
        )
        self.assertEqual(manifest.requests, ())
        self.assertEqual(len(manifest.screened), 1)

    def test_common_token_shapes_and_password_assignments_are_screened(self) -> None:
        for prefix in ("sk-", "ghp_", "github_pat_", "glpat-", "xoxb-"):
            with self.subTest(prefix=prefix):
                self.assertFalse(extraction.screen(prefix + "a" * 40))
        self.assertFalse(extraction.screen('password="synthetic-password"'))
        self.assertFalse(extraction.screen("postgresql://user:synthetic-password@db/database"))

    def test_bench_pattern_screens_each_synthetic_positive(self) -> None:
        positives = (
            "sk-" + "a" * 16,
            "sk-ant-" + "b" * 8,
            "ghp_" + "c" * 20,
            "github_pat_" + "d" * 20,
            "AKIA" + "E" * 16,
            "xoxb-" + "f" * 10,
            "-----BEGIN " + "RSA " + "PRIVATE KEY-----",
            "Bearer " + "g" * 20,
            "token: " + "h" * 8,
            "a" * 48,
            "j" * 60 + "==",
            "https" + "://" + "user" + ":" + "k" * 6 + "@",
            "glpat-" + "l" * 20,
            "ASIA" + "M" * 16,
        )
        for value in positives:
            with self.subTest(value=value):
                self.assertFalse(extraction.screen(value))

    def test_bench_pattern_passes_innocent_shapes(self) -> None:
        for value in ("a" * 40, "A" * 48, "token: ${VAR}"):
            with self.subTest(value=value):
                self.assertTrue(extraction.screen(value))


def assertion(**fields: str) -> dict[str, str]:
    return {
        "quote": "The server exists.",
        "check": "code_path",
        "repo": "brain-v42",
        "ref": "src/server.py",
        "asserts": "exists",
        "claimed_status": "unknown",
        "fact_key": "",
        "claimed_value": "",
        "centrality": "core",
        "temporal": "durable",
        **fields,
    }


def successful(custom_id: str, payload: Any, *, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        custom_id=custom_id,
        result=SimpleNamespace(
            type="succeeded",
            message=SimpleNamespace(
                stop_reason=stop_reason,
                content=[
                    SimpleNamespace(type="thinking"),
                    SimpleNamespace(type="text", text=json.dumps(payload)),
                ],
                usage=SimpleNamespace(
                    input_tokens=100,
                    output_tokens=20,
                    cache_read_input_tokens=200,
                    cache_creation_input_tokens=80,
                ),
            ),
        ),
    )


class CollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = extraction.build_requests("run", [entry(1)], facts_registry)
        self.client = FakeClient()
        self.payload = {"entries": [{"id": "00000001", "assertions": [assertion()]}]}
        self.client.items = [successful("run-00000", self.payload)]

    def collect(self) -> Any:
        return extraction.collect("batch-1", self.manifest, self.client)

    def test_success_maps_revision_and_is_consumable_by_check(self) -> None:
        results = self.collect()
        result = results["run-00000"]
        self.assertEqual(result.status, "succeeded")
        extracted = result.extractions[0]
        self.assertEqual(
            (extracted.entity_type, extracted.entity_id, extracted.digest),
            ("learning", entry(1).entity_id, entry(1).digest),
        )
        sources = Sources(
            frozenset({"src/server.py"}),
            frozenset(),
            {},
            {},
            {},
            facts_registry.REGISTRY,
            "snapshot",
        )
        observed = check(
            extracted.assertions[0], sources, entry(1).text, entry_id=entry(1).entity_id
        )
        self.assertIsNotNone(observed)
        assert observed is not None
        self.assertEqual(observed.polarity, "supports")

    def test_fact_claim_maps_without_a_model_verdict(self) -> None:
        self.payload["entries"][0]["assertions"] = [
            assertion(
                check="fact",
                ref="embedding.provider",
                fact_key="embedding.provider",
                claimed_value="local-gpu-shim",
            )
        ]
        self.client.items = [successful("run-00000", self.payload)]
        extracted = self.collect()["run-00000"].extractions[0]
        sources = Sources(
            frozenset(),
            frozenset(),
            {},
            {},
            {"embedding.provider": "mistral-api"},
            facts_registry.REGISTRY,
            "snapshot",
        )
        observed = check(
            extracted.assertions[0], sources, entry(1).text, entry_id=entry(1).entity_id
        )
        assert observed is not None
        self.assertEqual(observed.polarity, "contradicts")

    def test_terminal_error_statuses_have_no_extraction(self) -> None:
        for status in ("errored", "expired", "canceled"):
            self.client.items = [
                SimpleNamespace(custom_id="run-00000", result=SimpleNamespace(type=status))
            ]
            with self.subTest(status=status):
                result = self.collect()["run-00000"]
                self.assertEqual(result.status, status)
                self.assertEqual(result.extractions, ())

    def test_ended_batch_marks_absent_requests_missing(self) -> None:
        self.client.items = []
        self.assertEqual(self.collect()["run-00000"].status, "missing")

    def test_running_batch_is_not_declared_missing(self) -> None:
        self.client.processing_status = "in_progress"
        with self.assertRaisesRegex(ValueError, "ended"):
            self.collect()
        self.assertFalse(any(call[0] == "results" for call in self.client.calls))

    def test_first_duplicate_wins_even_if_it_is_errored(self) -> None:
        self.client.items.insert(
            0, SimpleNamespace(custom_id="run-00000", result=SimpleNamespace(type="errored"))
        )
        results = self.collect()
        self.assertEqual(results["run-00000"].status, "errored")
        self.assertEqual(results.duplicate_count, 1)

    def test_foreign_or_unknown_request_is_never_attributed(self) -> None:
        self.client.items = [
            successful(i, self.payload) for i in ("other-00000", "runner-00000", "run-99999")
        ]
        results = self.collect()
        self.assertEqual(list(results), ["run-00000"])
        self.assertEqual(results["run-00000"].status, "missing")
        self.assertEqual(results.rejected_count, 3)

    def test_unknown_entry_is_dropped_and_counted(self) -> None:
        self.payload["entries"].append({"id": "ffffffff", "assertions": [assertion()]})
        self.client.items = [successful("run-00000", self.payload)]
        results = self.collect()
        self.assertEqual(len(results["run-00000"].extractions), 1)
        self.assertEqual(results.dropped_count, 1)

    def test_unknown_only_entry_is_counted_even_when_request_is_invalid(self) -> None:
        self.client.items = [
            successful("run-00000", {"entries": [{"id": "ffffffff", "assertions": []}]})
        ]
        results = self.collect()
        self.assertEqual(results["run-00000"].status, "invalid")
        self.assertEqual(results.dropped_count, 1)

    def test_schema_invalid_results_preserve_usage_but_no_extraction(self) -> None:
        bad_claims = [
            {**assertion(), "fact_verdict": "supports"},
            assertion(centrality="invented"),
            assertion(quote=42),
            {key: value for key, value in assertion().items() if key != "quote"},
        ]
        for bad in bad_claims:
            self.client.items = [
                successful("run-00000", {"entries": [{"id": "00000001", "assertions": [bad]}]})
            ]
            with self.subTest(bad=bad):
                result = self.collect()["run-00000"]
                self.assertEqual(result.status, "invalid")
                self.assertEqual(result.extractions, ())
                self.assertEqual(result.usage.output_tokens, 20)

    def test_max_tokens_is_invalid_even_with_valid_json(self) -> None:
        self.client.items = [successful("run-00000", self.payload, stop_reason="max_tokens")]
        self.assertEqual(self.collect()["run-00000"].status, "invalid")

    def test_malformed_json_or_missing_text_is_invalid(self) -> None:
        for text in ("not JSON", "", '{"entries":null}'):
            item = successful("run-00000", self.payload)
            item.result.message.content = [SimpleNamespace(type="text", text=text)]
            self.client.items = [item]
            with self.subTest(text=text):
                self.assertEqual(self.collect()["run-00000"].status, "invalid")

    def test_empty_assertions_are_a_successful_extraction(self) -> None:
        self.payload["entries"][0]["assertions"] = []
        self.client.items = [successful("run-00000", self.payload)]
        result = self.collect()["run-00000"]
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.extractions[0].assertions, ())

    def test_missing_or_duplicate_entry_is_invalid(self) -> None:
        for entries in ([], self.payload["entries"] * 2):
            self.client.items = [successful("run-00000", {"entries": entries})]
            with self.subTest(entries=entries):
                self.assertEqual(self.collect()["run-00000"].status, "invalid")

    def test_real_cost_includes_cache_reads_and_writes(self) -> None:
        results = self.collect()
        self.assertAlmostEqual(
            extraction.real_cost_usd(results), (100 + 200 * 0.1 + 80 * 1.25 + 20 * 5) * 1e-6
        )
        self.client.items = []
        self.assertEqual(extraction.real_cost_usd(self.collect()), 0)

    def test_assertion_key_matches_check_and_normalizes_quote(self) -> None:
        self.payload["entries"][0]["assertions"][0]["quote"] = "THE  server\nexists."
        self.client.items = [successful("run-00000", self.payload)]
        extracted = self.collect()["run-00000"].extractions[0]
        sources = Sources(
            frozenset({"src/server.py"}),
            frozenset(),
            {},
            {},
            {},
            facts_registry.REGISTRY,
            "snapshot",
        )
        observed = check(
            extracted.assertions[0], sources, entry(1).text, entry_id=entry(1).entity_id
        )
        assert observed is not None
        self.assertEqual(extracted.assertions[0]["assertion_key"], observed.assertion_key)

    def test_repeated_json_keys_are_invalid(self) -> None:
        item = successful("run-00000", self.payload)
        item.result.message.content = [
            SimpleNamespace(type="text", text='{"entries": [], "entries": []}')
        ]
        self.client.items = [item]
        self.assertEqual(self.collect()["run-00000"].status, "invalid")

    def test_fact_key_normalizes_assertion_identity(self) -> None:
        self.payload["entries"][0]["assertions"] = [
            assertion(
                check="fact",
                ref="legacy-label",
                fact_key="embedding.provider",
                claimed_value="local-gpu-shim",
            )
        ]
        self.client.items = [successful("run-00000", self.payload)]
        extracted = self.collect()["run-00000"].extractions[0]
        sources = Sources(
            frozenset(),
            frozenset(),
            {},
            {},
            {"embedding.provider": "mistral-api"},
            facts_registry.REGISTRY,
            "snapshot",
        )
        observed = check(
            extracted.assertions[0], sources, entry(1).text, entry_id=entry(1).entity_id
        )
        assert observed is not None
        self.assertEqual(extracted.assertions[0]["assertion_key"], observed.assertion_key)

    def test_unmappable_fact_claim_is_dropped(self) -> None:
        for key, value in (("unknown", "local-gpu-shim"), ("embedding.provider", "invented")):
            self.payload["entries"][0]["assertions"] = [
                assertion(
                    check="fact",
                    ref=key,
                    fact_key=key,
                    claimed_value=value,
                )
            ]
            self.client.items = [successful("run-00000", self.payload)]
            with self.subTest(key=key, value=value):
                result = self.collect()["run-00000"]
                self.assertEqual(result.status, "succeeded")
                self.assertEqual(result.extractions[0].assertions, ())

    def test_import_does_not_require_the_sdk(self) -> None:
        import builtins
        import importlib

        original = builtins.__import__

        def guarded(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "anthropic" or name.startswith("anthropic."):
                raise AssertionError("SDK imported during module loading")
            return original(name, *args, **kwargs)

        with patch.object(builtins, "__import__", guarded):
            importlib.reload(extraction)


if __name__ == "__main__":
    unittest.main()
