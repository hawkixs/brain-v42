"""Measure pinned inputs without carrying yesterday's values forward."""

import unittest
from pathlib import Path

from brain_v42.knowledge.facts_registry import FactInputs, measure_all


class FactsRegistryTests(unittest.TestCase):
    def measure(self, key: str, text: str, path: str = "deploy/compose.yaml") -> str | None:
        return measure_all(FactInputs({path: text}, frozenset(), {}, {})).get(key)

    def test_production_host_comes_from_rail_site(self) -> None:
        inputs = FactInputs({"rail.yaml": "deploy:\n  site: red-base\n"}, frozenset(), {}, {})
        self.assertEqual(measure_all(inputs)["production.host"], "red-base")

    def test_host_accepts_quotes_and_comments(self) -> None:
        self.assertEqual(
            self.measure("production.host", 'deploy:\n  site: "dev-pc" # site\n', "rail.yaml"),
            "dev-pc",
        )

    def test_embedding_mistral_is_identified_by_url(self) -> None:
        self.assertEqual(
            self.measure(
                "embedding.provider",
                "EMBEDDING_BACKEND: openai\nEMBEDDING_SERVICE_URL: https://api.mistral.ai\n",
            ),
            "mistral-api",
        )

    def test_embedding_named_backends(self) -> None:
        for provider in ("local-gpu-shim", "dev-pc-supervisor", "pytorch-service"):
            with self.subTest(provider=provider):
                self.assertEqual(
                    self.measure("embedding.provider", f"EMBEDDING_BACKEND: {provider}\n"), provider
                )

    def test_embedding_local_shim_url(self) -> None:
        self.assertEqual(
            self.measure(
                "embedding.provider", "EMBEDDING_SERVICE_URL: http://embedding-shim:8003\n"
            ),
            "local-gpu-shim",
        )

    def test_embedding_unknown_url_is_unmeasurable(self) -> None:
        self.assertIsNone(
            self.measure(
                "embedding.provider", "EMBEDDING_SERVICE_URL: https://api.mistral.ai.evil.example\n"
            )
        )

    def test_rerank_backends_are_measured(self) -> None:
        for provider in ("voyage", "local-onnx", "local-gpu", "none"):
            with self.subTest(provider=provider):
                self.assertEqual(
                    self.measure("rerank.provider", f'BRAIN_RERANK_BACKEND: "{provider}"\n'),
                    provider,
                )

    def test_github_ci_requires_gitlab_absence(self) -> None:
        inputs = FactInputs({}, frozenset({".github/workflows/ci.yml"}), {}, {})
        self.assertEqual(measure_all(inputs).get("ci.system"), "github-actions")

    def test_gitlab_ci_is_measurable(self) -> None:
        self.assertEqual(
            measure_all(FactInputs({}, frozenset({".gitlab-ci.yml"}), {}, {})).get("ci.system"),
            "gitlab-ci",
        )

    def test_competing_ci_systems_are_unmeasurable(self) -> None:
        tree = frozenset({".github/workflows/ci.yml", ".gitlab-ci.yml"})
        self.assertIsNone(measure_all(FactInputs({}, tree, {}, {})).get("ci.system"))

    def test_hosted_runner(self) -> None:
        self.assertEqual(
            self.measure(
                "ci.runner",
                "jobs:\n  test:\n    runs-on: ubuntu-24.04\n",
                ".github/workflows/ci.yml",
            ),
            "hosted",
        )

    def test_hosted_runner_list_and_flow_forms(self) -> None:
        for labels in (
            "\n      - ubuntu-24.04",
            '\n      - "windows-2025"\n      - macos-15',
            "[ubuntu-24.04]",
            '["ubuntu-24.04", macos-15]',
        ):
            with self.subTest(labels=labels):
                text = f"jobs:\n  test:\n    runs-on: {labels}\n"
                self.assertEqual(
                    self.measure("ci.runner", text, ".github/workflows/ci.yml"), "hosted"
                )

    def test_unknown_runner_label_prevents_hosted_classification(self) -> None:
        for labels in ("[ubuntu-24.04, custom]", "\n      - ubuntu-24.04\n      - custom"):
            with self.subTest(labels=labels):
                text = f"jobs:\n  test:\n    runs-on: {labels}\n"
                self.assertIsNone(self.measure("ci.runner", text, ".github/workflows/ci.yml"))

    def test_empty_runner_does_not_prove_hosted(self) -> None:
        for labels in ("", "[]"):
            with self.subTest(labels=labels):
                text = (
                    f"jobs:\n  test:\n    runs-on: {labels}\n  deploy:\n    runs-on: ubuntu-24.04\n"
                )
                self.assertIsNone(self.measure("ci.runner", text, ".github/workflows/ci.yml"))

    def test_self_hosted_label_wins_in_list_and_flow_forms(self) -> None:
        for labels in (
            "[ubuntu-24.04, self-hosted]",
            '\n      - ubuntu-24.04\n      - "self-hosted"',
        ):
            with self.subTest(labels=labels):
                text = f"jobs:\n  test:\n    runs-on: {labels}\n"
                self.assertEqual(
                    self.measure("ci.runner", text, ".github/workflows/ci.yml"), "self-hosted"
                )

    def test_any_self_hosted_runner_wins(self) -> None:
        text = "jobs:\n  test:\n    runs-on: ubuntu-24.04\n  deploy:\n    runs-on: [self-hosted, linux]\n"
        self.assertEqual(self.measure("ci.runner", text, ".github/workflows/ci.yml"), "self-hosted")

    def test_multiline_runner_labels(self) -> None:
        text = "jobs:\n  test:\n    runs-on:\n      - self-hosted\n      - linux\n"
        self.assertEqual(self.measure("ci.runner", text, ".github/workflows/ci.yml"), "self-hosted")

    def test_dynamic_runner_is_unmeasurable(self) -> None:
        self.assertIsNone(
            self.measure("ci.runner", "runs-on: ${{ matrix.os }}\n", ".github/workflows/ci.yml")
        )

    def test_runner_comments_do_not_count(self) -> None:
        self.assertEqual(
            self.measure(
                "ci.runner",
                "# runs-on: self-hosted\nruns-on: ubuntu-24.04 # self-hosted\n",
                ".github/workflows/ci.yml",
            ),
            "hosted",
        )

    def test_graph_boolean(self) -> None:
        for flag, value in (("true", "enabled"), ("false", "disabled")):
            with self.subTest(flag=flag):
                self.assertEqual(
                    self.measure("graph.neo4j", f'GRAPH_PROJECTOR_ENABLED: "{flag}"\n'), value
                )

    def test_graph_uses_mcp_configuration_not_metrics(self) -> None:
        text = 'x-graph-env: &graph-env\n  GRAPH_PROJECTOR_ENABLED: "true"\nservices:\n  mcp:\n    environment: *graph-env\n  metrics:\n    environment:\n      GRAPH_PROJECTOR_ENABLED: "false"\n'
        self.assertEqual(self.measure("graph.neo4j", text), "enabled")

    def test_missing_shared_anchor_does_not_read_another_service(self) -> None:
        for mcp in (
            "    image: brain-v42\n",
            '    environment:\n      GRAPH_ENABLED: "false"\n',
            "    environment: *graph-env\n",
        ):
            with self.subTest(mcp=mcp):
                text = (
                    "services:\n  mcp:\n"
                    + mcp
                    + '  metrics:\n    environment:\n      GRAPH_PROJECTOR_ENABLED: "false"\n'
                )
                self.assertIsNone(self.measure("graph.neo4j", text))

    def test_services_without_mcp_do_not_read_another_service(self) -> None:
        text = 'services:\n  metrics:\n    environment:\n      GRAPH_PROJECTOR_ENABLED: "false"\n'
        self.assertIsNone(self.measure("graph.neo4j", text))

    def test_mcp_override_has_precedence(self) -> None:
        text = 'x-graph-env: &graph-env\n  GRAPH_PROJECTOR_ENABLED: "true"\nservices:\n  mcp:\n    environment:\n      <<: *graph-env\n      GRAPH_PROJECTOR_ENABLED: "false"\n'
        self.assertEqual(self.measure("graph.neo4j", text), "disabled")

    def test_measured_values_are_passed_through(self) -> None:
        inputs = FactInputs(
            {}, frozenset(), {"live_release_sha": "revision-123", "alembic_head": "071"}, {}
        )
        values = measure_all(inputs)
        self.assertEqual(values.get("release.live_sha"), "revision-123")
        self.assertEqual(values.get("schema.alembic_head"), "071")

    def test_dream_is_an_operator_decision(self) -> None:
        for state in ("running", "suspended"):
            with self.subTest(state=state):
                self.assertEqual(
                    measure_all(FactInputs({}, frozenset(), {}, {"dream": state})).get(
                        "dream.nightly"
                    ),
                    state,
                )

    def test_missing_inputs_are_unmeasurable(self) -> None:
        values = measure_all(FactInputs({}, frozenset(), {}, {}))
        self.assertEqual(len(values), 9)
        self.assertTrue(all(value is None for value in values.values()))

    def test_out_of_domain_measurements_are_unmeasurable(self) -> None:
        self.assertIsNone(self.measure("production.host", "site: unsupported\n", "rail.yaml"))

    def test_prompt_sheet_only_lists_keys_and_domains(self) -> None:
        from brain_v42.knowledge.facts_registry import REGISTRY, REGISTRY_VERSION, prompt_sheet

        self.assertEqual(REGISTRY_VERSION, "v1")
        sheet = prompt_sheet()
        self.assertEqual(len(sheet.splitlines()), 9)
        for key, definition in REGISTRY.items():
            self.assertIn(key, sheet)
            for value in definition.domain or ():
                self.assertIn(value, sheet)
        self.assertIn("open string", sheet)
        self.assertNotIn("revision-123", sheet)

    def test_real_checkout_configuration(self) -> None:
        root = Path(__file__).resolve().parents[3]
        paths = ["rail.yaml", "deploy/compose.yaml"]
        paths.extend(
            path.relative_to(root).as_posix() for path in (root / ".github/workflows").glob("*.yml")
        )
        files = {path: (root / path).read_text() for path in paths}
        tree = frozenset(paths + ([".gitlab-ci.yml"] if (root / ".gitlab-ci.yml").exists() else []))
        values = measure_all(FactInputs(files, tree, {}, {}))
        self.assertEqual(
            {key: value for key, value in values.items() if value is not None},
            {
                "production.host": "red-base",
                "embedding.provider": "mistral-api",
                "rerank.provider": "voyage",
                "ci.system": "github-actions",
                "ci.runner": "hosted",
                "graph.neo4j": "enabled",
            },
        )

    def test_every_multiline_runner_is_examined(self) -> None:
        text = "jobs:\n  test:\n    runs-on:\n      - ubuntu-24.04\n  deploy:\n    runs-on:\n      - self-hosted\n"
        self.assertEqual(self.measure("ci.runner", text, ".github/workflows/ci.yml"), "self-hosted")

    def test_runner_expression_is_not_a_literal_label(self) -> None:
        text = "runs-on: ${{ inputs.self-hosted }}\n"
        self.assertIsNone(self.measure("ci.runner", text, ".github/workflows/ci.yml"))

    def test_competing_literal_and_dynamic_values_are_unknown(self) -> None:
        text = "EMBEDDING_BACKEND: local-gpu-shim\nEMBEDDING_BACKEND: ${BACKEND}\n"
        self.assertIsNone(self.measure("embedding.provider", text))

    def test_shim_reranker_maps_to_local_onnx(self) -> None:
        self.assertEqual(
            self.measure("rerank.provider", "BRAIN_RERANK_BACKEND: shim\n"), "local-onnx"
        )

    def test_unused_anchor_does_not_measure_mcp(self) -> None:
        text = 'x-graph-env: &graph-env\n  GRAPH_PROJECTOR_ENABLED: "true"\nservices:\n  mcp:\n    environment:\n      GRAPH_ENABLED: "false"\n'
        self.assertIsNone(self.measure("graph.neo4j", text))

    def test_missing_workflow_text_does_not_prove_hosted_runners(self) -> None:
        inputs = FactInputs(
            {".github/workflows/ci.yml": "runs-on: ubuntu-24.04\n"},
            frozenset({".github/workflows/ci.yml", ".github/workflows/deploy.yml"}),
            {},
            {},
        )
        self.assertIsNone(measure_all(inputs)["ci.runner"])
