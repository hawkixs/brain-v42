"""Causal contracts for the read-only staged delivery canary."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
import uvicorn
from fastmcp import FastMCP
from fastmcp.client.auth import BearerAuth
from fastmcp.server.dependencies import get_http_headers
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import RedirectResponse
from starlette.routing import Route

from brain_v42.mcp.http_security import BearerTokenGuard
from brain_v42.models.delivery import (
    ArtifactBinding,
    BindingEvidence,
    ContextPredicate,
    ContractRevision,
    DeliveryAssessment,
    DeliveryView,
    EvaluationInput,
    MilestoneReceipt,
    ObservationConfirmation,
    PullRequestEvidence,
    RepositoryContextEvidence,
    RepositoryDocumentFact,
    RepositoryDocumentReference,
    context_reference_digest,
    context_reference_identity,
)
from brain_v42.models.delivery_evaluator import evaluate_delivery
from brain_v42.models.delivery_hashes import delivery_digest

SCRIPT = Path(__file__).parents[2] / "scripts" / "verify_delivery_canary.py"
TICKET = UUID("11111111-1111-1111-1111-111111111111")
BINDING = UUID("33333333-3333-3333-3333-333333333333")
CURRENT_CONFIRMATION = UUID("22222222-2222-2222-2222-222222222222")
FROZEN_CONFIRMATION = UUID("12121212-1212-1212-1212-121212121212")
CURRENT_SNAPSHOT = UUID("44444444-4444-4444-4444-444444444444")
FROZEN_SNAPSHOT = UUID("14141414-1414-1414-1414-141414141414")
ASSESSMENT = "a" * 64
FROZEN_ASSESSMENT = "1" * 64
HEAD = "d" * 40
BASE = "e" * 40
MERGE = "f" * 40
SYNTHETIC = "9" * 40
REPOSITORY_ID = 1337360966
PR_NUMBER = 7
APP_ID = 15368
NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)
CHECK_NAMES = (
    "lint-ruff",
    "lint-mypy",
    "test-unit",
    "test-integration",
    "test-coverage",
    "security-bandit",
    "security-gitleaks",
    "security-pip-audit",
    "security-pip-audit-embedding-supervisor",
)


@pytest.fixture()
def canary() -> ModuleType:
    assert SCRIPT.is_file(), "the standalone canary verifier must exist"
    spec = importlib.util.spec_from_file_location("delivery_canary", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["delivery_canary"] = module
    spec.loader.exec_module(module)
    return module


def test_missing_proof_is_expected_only_for_the_exact_contract(canary: ModuleType) -> None:
    view = _view(missing=True)
    result = canary.verify_view(view, _scope(view, positive=False), "missing-proof", now=NOW)
    assert result["outcome"] == "expected_missing_proof"
    assert "attempt" not in result["contract"]
    assert "pr_number" not in result["contract"]

    wrong = _scope(view, positive=False) | {"contract_revision": 2}
    with pytest.raises(canary.CanaryFailure, match="canary_identity_mismatch"):
        canary.verify_view(view, wrong, "missing-proof", now=NOW)


def test_missing_proof_refuses_an_unexpected_fresh_proof(canary: ModuleType) -> None:
    view = _view()
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        canary.verify_view(view, _scope(view, positive=False), "missing-proof", now=NOW)


@pytest.mark.parametrize(
    "mutation",
    ("completion_eligible_now", "contract_fulfilled", "integration_receipt", "fulfillment_receipt"),
)
def test_missing_proof_refuses_any_completion_or_receipt_capability(
    canary: ModuleType, mutation: str
) -> None:
    view = _view(missing=True)
    if mutation in {"integration_receipt", "fulfillment_receipt"}:
        receipt_view = _view(stage="integrated", accepted=True, receipts=True)
        view[mutation] = receipt_view[mutation]
    else:
        view["assessment"][mutation] = True
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        canary.verify_view(view, _scope(view, positive=False), "missing-proof", now=NOW)


@pytest.mark.parametrize("health", ["stale", "error", "disabled", "never_observed"])
def test_observed_refuses_nonfresh_current_evidence(canary: ModuleType, health: str) -> None:
    view = _view(health=health)
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        _verify(canary, view, "observed")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("bindings", 0, "binding", "pr_number"), 99),
        (("bindings", 0, "binding", "head_sha"), "0" * 40),
        (("bindings", 0, "confirmation", "evidence", "base_sha"), "0" * 40),
        (("bindings", 0, "confirmation", "evidence", "repository_id"), 99),
        (("bindings", 0, "confirmation", "evidence", "integration_sha"), "0" * 40),
    ],
)
def test_observed_refuses_unrelated_or_internally_split_identity(
    canary: ModuleType, path: tuple[str | int, ...], value: object
) -> None:
    view = _view()
    _set(view, path, value)
    with pytest.raises(canary.CanaryFailure, match="canary_identity_mismatch"):
        _verify(canary, view, "observed")


def test_observed_recomputes_delivery_digest_instead_of_trusting_two_matching_claims(
    canary: ModuleType,
) -> None:
    view = _view()
    forged = "c" * 64
    view["assessment"]["delivery_digest"] = forged
    scope = _scope(view) | {"expected_delivery_digest": forged}
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        canary.verify_view(view, scope, "observed", github_evidence=_evidence(view), now=NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("assessed_at", NOW + timedelta(seconds=1)),
        ("observed_at", NOW + timedelta(seconds=1)),
        ("fresh_until", NOW),
    ],
)
def test_observed_refuses_expired_or_future_assessment_times(
    canary: ModuleType, field: str, value: datetime
) -> None:
    view = _view()
    view["assessment"][field] = value.isoformat()
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        _verify(canary, view, "observed")


def test_observed_requires_a_newer_confirmation_when_polling_proof_is_requested(
    canary: ModuleType,
) -> None:
    view = _view()
    scope = _scope(view) | {"prior_success_confirmation_id": str(CURRENT_CONFIRMATION)}
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        canary.verify_view(view, scope, "observed", github_evidence=_evidence(view), now=NOW)

    scope = _scope(view) | {"not_before": NOW.isoformat()}
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        canary.verify_view(view, scope, "observed", github_evidence=_evidence(view), now=NOW)


@pytest.mark.parametrize("required", [True, False])
def test_observed_accepts_real_aggregate_context_freshness(
    canary: ModuleType, required: bool
) -> None:
    view = _evaluated_context_view(required=required)
    expected = NOW - timedelta(seconds=300 if required else 2)
    assert view["assessment"]["observed_at"] == expected.isoformat().replace("+00:00", "Z")
    assert view["assessment"]["requirements_satisfied"] is True
    result = _verify(canary, view, "observed")
    assert result["assessment"]["observed_at"] == expected.isoformat()
    assert result["current_confirmation"]["id"] == str(CURRENT_CONFIRMATION)


def test_observed_refuses_an_assessment_older_than_its_current_confirmation(
    canary: ModuleType,
) -> None:
    view = _evaluated_context_view(required=False)
    view["assessment"]["assessed_at"] = (NOW - timedelta(minutes=1)).isoformat()
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        _verify(canary, view, "observed")


def test_observed_refuses_ignoring_an_older_required_context(
    canary: ModuleType,
) -> None:
    view = _evaluated_context_view()
    view["assessment"]["observed_at"] = (NOW - timedelta(seconds=2)).isoformat()
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        _verify(canary, view, "observed")


@pytest.mark.parametrize("age_seconds", [601, -1])
def test_verified_refuses_real_stale_or_future_required_context(
    canary: ModuleType, age_seconds: int
) -> None:
    view = _evaluated_context_view(context_age_seconds=age_seconds, merged=False)
    assert view["assessment"]["observation_health"] in {"stale", "error"}
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        _verify(canary, view, "verified")


def test_verified_accepts_a_real_open_green_pr_before_integration(
    canary: ModuleType,
) -> None:
    view = _evaluated_context_view(merged=False)
    assert view["assessment"]["delivery_stage"] == "verified"
    assert view["assessment"]["requirements_satisfied"] is False
    assert {item["code"] for item in view["assessment"]["blockers"]} == {"pr_not_merged"}
    result = _verify(canary, view, "verified")
    assert result["outcome"] == "verified"
    assert len(result["required_checks"]) == len(CHECK_NAMES)


def test_verified_refuses_other_real_context_blockers_even_with_green_checks(
    canary: ModuleType,
) -> None:
    view = _evaluated_context_view(merged=False, context_missing=True)
    assert view["assessment"]["delivery_stage"] == "verified"
    assert "context_predicate_missing" in {item["code"] for item in view["assessment"]["blockers"]}
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        _verify(canary, view, "verified")


def test_observed_requires_fresh_github_identity_but_not_passing_ci(canary: ModuleType) -> None:
    view = _view()
    evidence = _evidence(view)
    failed = evidence.model_copy(
        update={
            "checks": tuple(
                check.model_copy(update={"conclusion": "failure"}) for check in evidence.checks
            )
        }
    )
    result = canary.verify_view(view, _scope(view), "observed", github_evidence=failed, now=NOW)
    assert result["outcome"] == "observed"
    assert result["github"]["head_sha"] == HEAD


def test_positive_phase_refuses_a_live_github_identity_mismatch(canary: ModuleType) -> None:
    view = _view()
    unrelated = _evidence(view).model_copy(update={"pr_number": 99})
    with pytest.raises(canary.CanaryFailure, match="current_github_proof_invalid"):
        canary.verify_view(view, _scope(view), "observed", github_evidence=unrelated, now=NOW)


def test_verified_uses_the_canonical_check_selector_on_current_github_facts(
    canary: ModuleType,
) -> None:
    view = _view()
    evidence = _evidence(view)
    wrong_app = evidence.checks[0].model_copy(update={"provider_id": 1})
    evidence = evidence.model_copy(update={"checks": (wrong_app, *evidence.checks[1:])})
    with pytest.raises(canary.CanaryFailure, match="required_checks_unverified"):
        canary.verify_view(view, _scope(view), "verified", github_evidence=evidence, now=NOW)


def test_verified_requires_current_assessment_requirements(canary: ModuleType) -> None:
    view = _view(requirements=False)
    with pytest.raises(canary.CanaryFailure, match="current_generation_unverified"):
        _verify(canary, view, "verified")


def test_integrated_rejects_receipt_for_another_generation(canary: ModuleType) -> None:
    view = _view(stage="integrated", receipts=True)
    view["integration_receipt"]["attempt"] = 2
    view["integration_receipt"]["proof"]["attempt"] = 2
    with pytest.raises(canary.CanaryFailure, match="receipt_invalid_or_missing"):
        _verify(canary, view, "integrated")


def test_receipts_remain_valid_after_a_newer_same_head_successful_poll(
    canary: ModuleType,
) -> None:
    view = _view(stage="integrated", accepted=True, receipts=True)
    result = _verify(canary, view, "accepted")
    assert result["outcome"] == "accepted"
    assert result["binding"]["binding_version"] == 3
    assert result["integration_receipt"]["binding_version"] == 1
    assert result["current_confirmation"]["id"] == str(CURRENT_CONFIRMATION)
    assert result["integration_receipt"]["success_confirmation_id"] == str(FROZEN_CONFIRMATION)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("integration_receipt", "proof", "workflow_version"), 4),
        (("integration_receipt", "proof", "artifact_proofs", 0, "binding_version"), 4),
        (("integration_receipt", "proof", "artifact_proofs", 0, "head_sha"), "0" * 40),
        (("integration_receipt", "proof", "artifact_proofs", 0, "base_sha"), "0" * 40),
        (("integration_receipt", "proof", "artifact_proofs", 0, "integration_sha"), "0" * 40),
    ],
)
def test_integrated_rejects_future_or_split_frozen_identity(
    canary: ModuleType, path: tuple[str | int, ...], value: object
) -> None:
    view = _view(stage="integrated", receipts=True)
    _set(view, path, value)
    with pytest.raises(canary.CanaryFailure, match="receipt_invalid_or_missing"):
        _verify(canary, view, "integrated")


def test_accepted_requires_explicit_current_and_frozen_requester_decision(
    canary: ModuleType,
) -> None:
    view = _view(stage="integrated", accepted=True, receipts=True)
    for receipt in (view["fulfillment_receipt"], view["fulfillment_receipt"]["proof"]):
        receipt["explicit_acceptance"]["requester_project"] = "other-project"
    with pytest.raises(canary.CanaryFailure, match="receipt_invalid_or_missing"):
        _verify(canary, view, "accepted")


@pytest.mark.parametrize(
    "value",
    (
        "http://example.test/mcp",
        "https://token@example.test/mcp",
        "https://example.test/mcp?next=https://elsewhere.test",
        "https://example.test/mcp#fragment",
        "https://example.test/mcp\\escape",
    ),
)
def test_transport_url_refuses_unsafe_origins(canary: ModuleType, value: str) -> None:
    with pytest.raises(canary.CanaryFailure, match="brain_transport_or_shape_invalid"):
        canary.validate_mcp_url(value)


def test_transport_factory_preserves_only_declared_headers_and_bearer(canary: ModuleType) -> None:
    factory = canary.strict_http_client_factory()
    auth = BearerAuth("fixture-secret")
    client = factory(
        follow_redirects=True,
        headers={
            "x-brain-tool-profile": "native",
            "x-brain-agent": "delivery-canary-verifier",
            "Cookie": "ambient",
        },
        auth=auth,
    )
    try:
        assert client.follow_redirects is False
        assert client.trust_env is False
        assert client.headers["x-brain-tool-profile"] == "native"
        assert client.headers["x-brain-agent"] == "delivery-canary-verifier"
        assert client.headers.get("cookie") is None
        assert client._auth is auth
    finally:
        asyncio.run(client.aclose())


def test_transport_factory_rejects_ambient_auth(canary: ModuleType) -> None:
    with pytest.raises(ValueError, match="bearer"):
        canary.strict_http_client_factory()(auth=httpx.BasicAuth("user", "secret"))


@pytest.mark.asyncio
async def test_missing_mcp_credential_is_a_safe_transport_failure(
    canary: ModuleType, tmp_path: Path
) -> None:
    config = _canary_config(
        canary,
        tmp_path / "missing.token",
        "http://127.0.0.1:1/mcp",
        tmp_path,
    )
    with pytest.raises(canary.CanaryFailure, match="brain_transport_or_shape_invalid"):
        await canary._read_view(config)


def test_malformed_delivery_shape_is_a_safe_transport_failure(canary: ModuleType) -> None:
    with pytest.raises(canary.CanaryFailure, match="brain_transport_or_shape_invalid"):
        canary.verify_view(
            {"provider_secret": "must-not-escape"},
            _scope(_view()),
            "observed",
            now=NOW,
        )


def test_delivery_shape_does_not_coerce_numeric_identities(canary: ModuleType) -> None:
    view = _view()
    view["contract"]["deliverables"][0]["repository_id"] = str(REPOSITORY_ID)
    view["bindings"][0]["binding"]["repository_id"] = str(REPOSITORY_ID)
    view["bindings"][0]["confirmation"]["evidence"]["repository_id"] = str(REPOSITORY_ID)
    with pytest.raises(canary.CanaryFailure, match="brain_transport_or_shape_invalid"):
        canary.verify_view(
            view,
            _scope(_view()),
            "observed",
            github_evidence=_evidence(_view()),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_bounded_transport_counts_chunked_response_bytes(canary: ModuleType) -> None:
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"a" * canary._MAX_RESPONSE_BYTES
            yield b"b"

    class Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=Chunks(), request=request)

    async with httpx.AsyncClient(transport=canary._BoundedTransport(Inner())) as client:
        with pytest.raises(httpx.ReadError, match="response limit"):
            async with client.stream("GET", "https://example.test/value") as response:
                async for _chunk in response.aiter_bytes():
                    pass


@pytest.mark.asyncio
async def test_bounded_transport_closes_a_rejected_encoded_response(canary: ModuleType) -> None:
    class Encoded(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"compressed"

        async def aclose(self) -> None:
            self.closed = True

    encoded = Encoded()

    class Inner(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Encoding": "gzip"},
                stream=encoded,
                request=request,
            )

    async with httpx.AsyncClient(transport=canary._BoundedTransport(Inner())) as client:
        with pytest.raises(httpx.ReadError, match="response limit"):
            await client.get("https://example.test/value")
    assert encoded.closed


def test_actual_9b1_helper_can_be_loaded_with_its_dataclass_context(canary: ModuleType) -> None:
    assert callable(canary._load_preflight_checker())


@pytest.mark.asyncio
async def test_real_fastmcp_http_returns_structured_content_with_exact_auth_and_headers(
    canary: ModuleType, tmp_path: Path
) -> None:
    view = _view()
    calls: list[dict[str, Any]] = []
    server = FastMCP("delivery-canary-fixture")

    @server.tool(name="brain_delivery_get")
    async def delivery_get(
        ticket_id: str,
        actor_project: str,
        history_limit: int,
        history_cursor: str | None,
    ) -> dict[str, Any]:
        calls.append(
            {
                "ticket_id": ticket_id,
                "actor_project": actor_project,
                "history_limit": history_limit,
                "history_cursor": history_cursor,
                "headers": dict(get_http_headers(include={"authorization"})),
            }
        )
        return view

    app = server.http_app(
        transport="http",
        stateless_http=True,
        json_response=True,
        middleware=[Middleware(BearerTokenGuard, token="mcp-fixture-token")],
    )
    token = _private(tmp_path / "mcp.token", "mcp-fixture-token")
    async with _serve_loopback(app) as base_url:
        config = _canary_config(canary, token, f"{base_url}/mcp", tmp_path)
        raw = await canary._read_view(config)

    assert raw == view
    assert len(calls) == 1
    assert calls[0]["ticket_id"] == str(TICKET)
    assert calls[0]["actor_project"] == "brain-v42"
    assert calls[0]["history_limit"] == 20
    assert calls[0]["history_cursor"] is None
    assert calls[0]["headers"]["authorization"] == "Bearer mcp-fixture-token"
    assert calls[0]["headers"]["x-brain-tool-profile"] == "native"
    assert calls[0]["headers"]["x-brain-agent"] == "delivery-canary-verifier"
    assert "cookie" not in calls[0]["headers"]


@pytest.mark.asyncio
async def test_mcp_redirect_is_refused_without_following(
    canary: ModuleType, tmp_path: Path
) -> None:
    requests: list[str] = []

    async def redirect(request: Any) -> RedirectResponse:
        requests.append(request.url.path)
        return RedirectResponse("https://example.test/credential-target", status_code=307)

    app = Starlette(routes=[Route("/mcp", redirect, methods=["POST", "GET", "DELETE"])])
    token = _private(tmp_path / "mcp.token", "mcp-fixture-token")
    async with _serve_loopback(app) as base_url:
        config = _canary_config(canary, token, f"{base_url}/mcp", tmp_path)
        with pytest.raises(canary.CanaryFailure, match="brain_transport_or_shape_invalid"):
            await canary._read_view(config)
    assert requests == ["/mcp"]


@pytest.mark.asyncio
async def test_mcp_overall_timeout_is_a_safe_failure(
    canary: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = FastMCP("delivery-canary-timeout")

    @server.tool(name="brain_delivery_get")
    async def delivery_get(
        ticket_id: str,
        actor_project: str,
        history_limit: int,
        history_cursor: str | None,
    ) -> dict[str, Any]:
        del ticket_id, actor_project, history_limit, history_cursor
        await asyncio.sleep(1)
        return _view()

    app = server.http_app(
        transport="http",
        stateless_http=True,
        json_response=True,
        middleware=[Middleware(BearerTokenGuard, token="mcp-fixture-token")],
    )
    token = _private(tmp_path / "mcp.token", "mcp-fixture-token")
    async with _serve_loopback(app) as base_url:
        config = _canary_config(canary, token, f"{base_url}/mcp", tmp_path)
        monkeypatch.setattr(canary, "_OVERALL_TIMEOUT_SECONDS", 0.01)
        with pytest.raises(canary.CanaryFailure, match="brain_transport_or_shape_invalid"):
            await canary._read_view(config)


@pytest.mark.asyncio
async def test_real_github_client_reuses_retained_synthetic_proof_and_emits_safe_records(
    canary: ModuleType, tmp_path: Path
) -> None:
    view = DeliveryView.model_validate(_view(), strict=False)
    seen: list[httpx.Request] = []
    observer_env = _observer_env(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer github-fixture-token"
        assert "cookie" not in request.headers
        root = "/repos/hawkixs/brain-v42"
        if request.url.path == f"{root}/pulls/{PR_NUMBER}":
            return httpx.Response(200, json=_github_pr())
        if request.url.path == f"{root}/git/commits/{SYNTHETIC}":
            return httpx.Response(
                200,
                json={
                    "sha": SYNTHETIC,
                    "parents": [{"sha": BASE}, {"sha": HEAD}],
                    "tree": {"sha": "8" * 40},
                },
            )
        if request.url.path == f"{root}/commits/{HEAD}/check-runs":
            return httpx.Response(200, json={"total_count": 0, "check_runs": []})
        if request.url.path == f"{root}/commits/{SYNTHETIC}/check-runs":
            records = [_github_check(index, name) for index, name in enumerate(CHECK_NAMES, 1)]
            return httpx.Response(200, json={"total_count": len(records), "check_runs": records})
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    live = await canary._collect_github(
        observer_env,
        view.bindings[0].binding,
        view.contract,
        view.bindings[0].confirmation.evidence,
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
    )
    assert live.synthetic_merges[0].synthetic_sha == SYNTHETIC
    assert [check.name for check in live.checks] == list(CHECK_NAMES)
    assert all(check.provider_id == APP_ID for check in live.checks)
    assert all(
        check.record_url and check.record_url.startswith("https://api.github.test/")
        for check in live.checks
    )
    assert sum(request.url.path.endswith(f"/git/commits/{SYNTHETIC}") for request in seen) == 1


@pytest.mark.asyncio
async def test_each_phase_reruns_preflight_and_positive_phases_collect_github(
    canary: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    positive = _view(stage="integrated", accepted=True, receipts=True)
    missing = _view(missing=True)
    token = _private(tmp_path / "mcp.token", "fixture")
    config = _canary_config(canary, token, "http://127.0.0.1:1/mcp", tmp_path)
    calls = {"preflight": 0, "read": 0, "github": 0}
    selected_phase = "missing-proof"

    def preflight(_path: Path) -> Any:
        calls["preflight"] += 1
        return canary.PreflightReceipt(
            source_sha="7" * 40,
            schema_revision="053",
            observer_env=tmp_path / "observer.env",
        )

    async def read(_config: Any) -> dict[str, Any]:
        calls["read"] += 1
        return missing if selected_phase == "missing-proof" else positive

    async def github(*_args: Any, **_kwargs: Any) -> PullRequestEvidence:
        calls["github"] += 1
        return _evidence(positive)

    monkeypatch.setattr(canary, "_deployment_canary", preflight)
    monkeypatch.setattr(canary, "_read_view", read)
    monkeypatch.setattr(canary, "_collect_github", github)
    for phase in ("missing-proof", "observed", "verified", "integrated", "accepted"):
        selected_phase = phase
        receipt = await canary._run(config, phase, clock=lambda: NOW)
        assert receipt["source_sha"] == "7" * 40
        assert receipt["schema_revision"] == "053"
        assert receipt["source"] == "deployment_preflight_rerun"
    assert calls == {"preflight": 5, "read": 5, "github": 4}


@pytest.mark.asyncio
async def test_run_executes_sync_preflight_checker_outside_its_event_loop(
    canary: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    deployment = _private(
        tmp_path / "deployment.json",
        json.dumps(
            {
                "mode": "canary",
                "observer_env_file": str(tmp_path / "observer.env"),
                "writers": {"brain-v42-delivery-observer.service": {"must_be_active": True}},
            }
        ),
    )
    token = _private(tmp_path / "mcp.token", "fixture")
    config = _canary_config(canary, token, "http://127.0.0.1:1/mcp", tmp_path)
    calls = {"preflight": 0, "read": 0}

    def check(_path: Path) -> dict[str, str]:
        async def schema_probe() -> str:
            return "053"

        calls["preflight"] += 1
        assert asyncio.run(schema_probe()) == "053"
        return {"source_sha": "7" * 40, "schema_revision": "053"}

    async def read(_config: Any) -> dict[str, Any]:
        calls["read"] += 1
        return _view(missing=True)

    monkeypatch.setattr(canary, "_load_preflight_checker", lambda: check)
    monkeypatch.setattr(canary, "_read_view", read)
    result = await canary._run(config, "missing-proof", clock=lambda: NOW)

    assert deployment == config.deployment_config
    assert result["outcome"] == "expected_missing_proof"
    assert result["source"] == "deployment_preflight_rerun"
    assert calls == {"preflight": 1, "read": 1}


@pytest.mark.asyncio
async def test_run_stops_before_mcp_or_github_when_real_preflight_fails(
    canary: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _private(
        tmp_path / "deployment.json",
        json.dumps(
            {
                "mode": "canary",
                "observer_env_file": str(tmp_path / "observer.env"),
                "writers": {"brain-v42-delivery-observer.service": {"must_be_active": True}},
            }
        ),
    )
    token = _private(tmp_path / "mcp.token", "fixture")
    config = _canary_config(canary, token, "http://127.0.0.1:1/mcp", tmp_path)
    calls = {"read": 0, "github": 0}

    def check(_path: Path) -> dict[str, str]:
        raise RuntimeError("preflight fixture failure")

    async def read(_config: Any) -> dict[str, Any]:
        calls["read"] += 1
        return _view()

    async def github(*_args: Any, **_kwargs: Any) -> PullRequestEvidence:
        calls["github"] += 1
        return _evidence(_view())

    monkeypatch.setattr(canary, "_load_preflight_checker", lambda: check)
    monkeypatch.setattr(canary, "_read_view", read)
    monkeypatch.setattr(canary, "_collect_github", github)
    with pytest.raises(canary.CanaryFailure, match="running_artifact_unverified"):
        await canary._run(config, "observed", clock=lambda: NOW)
    assert calls == {"read": 0, "github": 0}


@pytest.mark.asyncio
async def test_run_samples_time_after_brain_and_github_acquisitions(
    canary: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    view = _view()
    confirmation = view["bindings"][0]["confirmation"]
    confirmation["collection_started_at"] = (NOW + timedelta(seconds=6)).isoformat()
    confirmation["collection_finished_at"] = (NOW + timedelta(seconds=8)).isoformat()
    confirmation["evidence"]["collected_at"] = (NOW + timedelta(seconds=8)).isoformat()
    view["bindings"][0]["last_attempt_at"] = (NOW + timedelta(seconds=8)).isoformat()
    view["bindings"][0]["last_success_at"] = (NOW + timedelta(seconds=8)).isoformat()
    view["assessment"]["observed_at"] = (NOW + timedelta(seconds=8)).isoformat()
    view["assessment"]["assessed_at"] = (NOW + timedelta(seconds=9)).isoformat()
    view["assessment"]["fresh_until"] = (NOW + timedelta(minutes=5)).isoformat()
    token = _private(tmp_path / "mcp.token", "fixture")
    config = _canary_config(canary, token, "http://127.0.0.1:1/mcp", tmp_path)
    moments = iter(
        (NOW + timedelta(seconds=10), NOW + timedelta(seconds=20), NOW + timedelta(seconds=21))
    )
    sampled: list[datetime] = []

    def clock() -> datetime:
        value = next(moments)
        sampled.append(value)
        return value

    monkeypatch.setattr(
        canary,
        "_deployment_canary",
        lambda _path: canary.PreflightReceipt(
            source_sha="7" * 40,
            schema_revision="053",
            observer_env=tmp_path / "observer.env",
        ),
    )

    async def read(_config: Any) -> dict[str, Any]:
        return view

    async def github(*_args: Any, **kwargs: Any) -> PullRequestEvidence:
        collected_at = kwargs["now"]()
        return _evidence(view).model_copy(update={"collected_at": collected_at})

    monkeypatch.setattr(canary, "_read_view", read)
    monkeypatch.setattr(canary, "_collect_github", github)
    result = await canary._run(config, "observed", clock=clock)
    assert result["assessment"]["assessed_at"] == (NOW + timedelta(seconds=9)).isoformat()
    assert result["github"]["collected_at"] == (NOW + timedelta(seconds=20)).isoformat()
    assert sampled == [
        NOW + timedelta(seconds=10),
        NOW + timedelta(seconds=20),
        NOW + timedelta(seconds=21),
    ]


def test_dormant_observer_is_refused_before_the_9b1_checker(
    canary: ModuleType, tmp_path: Path
) -> None:
    deployment = _private(
        tmp_path / "deployment.json",
        json.dumps(
            {
                "mode": "dormant",
                "observer_env_file": str(tmp_path / "observer.env"),
                "writers": {"brain-v42-delivery-observer.service": {"must_be_active": False}},
            }
        ),
    )
    with pytest.raises(canary.CanaryFailure, match="running_artifact_unverified"):
        canary._deployment_canary(deployment)


def test_private_config_requires_absolute_paths_aware_time_and_no_extra_fields(
    canary: ModuleType, tmp_path: Path
) -> None:
    raw = {
        "deployment_config": str(tmp_path / "deployment.json"),
        "mcp_url": "http://127.0.0.1:18743/mcp",
        "mcp_token_file": str(tmp_path / "mcp.token"),
        **_scope(_view()),
    }
    path = _private(tmp_path / "canary.json", json.dumps(raw))
    assert canary._load_config(path).not_before is None
    for mutation in (
        {"deployment_config": "relative.json"},
        {"not_before": "2026-09-08T12:00:00"},
        {"unexpected": "secret"},
    ):
        name = next(iter(mutation))
        invalid = _private(tmp_path / f"invalid-{name}.json", json.dumps(raw | mutation))
        with pytest.raises(canary.CanaryFailure, match="brain_transport_or_shape_invalid"):
            canary._load_config(invalid)


def test_cli_emits_one_utc_json_without_private_paths_or_tokens(
    canary: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = _private(tmp_path / "private-token-name", "never-render-this-token")
    config = _canary_config(canary, token, "http://127.0.0.1:1/mcp", tmp_path)

    async def run(_config: Any, phase: str) -> dict[str, object]:
        assert phase == "observed"
        return {
            "outcome": "observed",
            "source": "deployment_preflight_rerun",
            "source_sha": "7" * 40,
            "schema_revision": "053",
        }

    monkeypatch.setattr(canary, "_load_config", lambda _path: config)
    monkeypatch.setattr(canary, "_run", run)
    assert canary.main(["--config", str(tmp_path / "canary.json"), "--phase", "observed"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["status"] == "ok" and payload["phase"] == "observed"
    assert datetime.fromisoformat(payload["timestamp"]).utcoffset() is not None
    assert "never-render-this-token" not in output
    assert str(token) not in output


def _verify(canary: ModuleType, view: dict[str, Any], phase: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        canary.verify_view(
            view,
            _scope(view),
            phase,
            github_evidence=_evidence(view),
            now=NOW,
        ),
    )


def _scope(view: dict[str, Any], *, positive: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ticket_id": str(TICKET),
        "actor_project": "brain-v42",
        "repository_id": REPOSITORY_ID,
        "pr_number": PR_NUMBER,
        "deliverable_key": "canary",
        "contract_revision": 1,
        "attempt": 1,
        "contract_digest": view["contract"]["content_digest"],
    }
    if positive:
        result.update(
            expected_head=HEAD,
            expected_delivery_digest=view["assessment"]["delivery_digest"],
        )
    return result


def _view(
    *,
    health: str = "fresh",
    requirements: bool = True,
    stage: str = "verified",
    accepted: bool = False,
    receipts: bool = False,
    missing: bool = False,
) -> dict[str, Any]:
    contract = ContractRevision.model_validate(
        {
            "ticket_id": TICKET,
            "contract_revision": 1,
            "content_digest": None,
            "author_project": "brain-v42",
            "objective": "verify one delivery canary",
            "priority": 1,
            "acceptance_mode": "explicit",
            "deliverables": [
                {
                    "key": "canary",
                    "repository": "hawkixs/brain-v42",
                    "repository_id": REPOSITORY_ID,
                    "target_branch": "main",
                    "required_checks": [
                        {
                            "kind": "check_run",
                            "name": name,
                            "app_slug": "github-actions",
                            "provider_id": APP_ID,
                        }
                        for name in CHECK_NAMES
                    ],
                    "no_checks_reason": None,
                    "review": {"required_approvals": 0, "allowed_reviewers": []},
                }
            ],
            "dependencies": [],
            "context_refs": [],
            "created_at": NOW - timedelta(minutes=10),
        }
    )
    checks = tuple(
        {
            "record_id": 8000 + index,
            "provider_id": APP_ID,
            "kind": "check_run",
            "name": name,
            "app_slug": "github-actions",
            "head_sha": SYNTHETIC,
            "conclusion": "success",
            "started_at": NOW - timedelta(minutes=2),
            "completed_at": NOW - timedelta(minutes=1),
            "check_suite_id": 9000 + index,
            "record_url": (
                f"https://api.github.test/repos/hawkixs/brain-v42/check-runs/{8000 + index}"
            ),
        }
        for index, name in enumerate(CHECK_NAMES, 1)
    )
    evidence = PullRequestEvidence.model_validate(
        {
            "provider_id": 7001,
            "repository_id": REPOSITORY_ID,
            "pr_number": PR_NUMBER,
            "author_id": "executor",
            "head_repository_id": REPOSITORY_ID,
            "head_sha": HEAD,
            "base_sha": BASE,
            "base_ref": "main",
            "integration_sha": MERGE,
            "integration_revision": None,
            "state": "merged",
            "draft": False,
            "mergeable": True,
            "complete": True,
            "checks": checks,
            "reviews": [],
            "synthetic_merges": [{"synthetic_sha": SYNTHETIC, "head_sha": HEAD, "base_sha": BASE}],
            "collected_at": NOW - timedelta(seconds=2),
        }
    )
    binding = ArtifactBinding(
        id=BINDING,
        ticket_id=TICKET,
        contract_revision=1,
        attempt=1,
        deliverable_key="canary",
        repository_id=REPOSITORY_ID,
        pr_number=PR_NUMBER,
        state="observed",
        head_sha=HEAD,
        base_sha=BASE,
        integration_sha=MERGE,
        binding_version=3,
    )
    confirmation = ObservationConfirmation(
        id=CURRENT_CONFIRMATION,
        evidence=evidence,
        collection_started_at=NOW - timedelta(seconds=4),
        collection_finished_at=NOW - timedelta(seconds=2),
        outcome="success",
    )
    binding_evidence = BindingEvidence(
        binding=binding,
        confirmation=confirmation,
        snapshot_id=CURRENT_SNAPSHOT,
        success_confirmation_id=CURRENT_CONFIRMATION,
        latest_attempt_confirmation_id=CURRENT_CONFIRMATION,
        last_attempt_at=NOW - timedelta(seconds=2),
        last_success_at=NOW - timedelta(seconds=2),
        last_attempt_outcome="success",
    )
    active = () if missing else (binding_evidence,)
    digest = delivery_digest(
        contract_digest=contract.content_digest or "",
        attempt=1,
        active_bindings=active,
    )
    assessment = DeliveryAssessment(
        assessment_id=ASSESSMENT,
        assessment_version=3,
        assessed_at=NOW - timedelta(seconds=1),
        observed_at=None if missing else NOW - timedelta(seconds=2),
        fresh_until=None if missing else NOW + timedelta(minutes=5),
        coordination_status="open",
        delivery_stage="awaiting_artifact" if missing else stage,
        observation_health="never_observed" if missing else health,
        acceptance_state="accepted" if accepted else "pending",
        requirements_satisfied=False if missing else requirements,
        integration_receipt_eligible=False if missing else requirements,
        completion_eligible_now=accepted and requirements,
        contract_fulfilled=accepted and requirements,
        delivery_digest=digest,
        blockers=(),
        deliverables=(),
        eligible_work=(),
    )
    integration = _receipt(contract, digest, "integration") if receipts else None
    fulfillment = _receipt(contract, digest, "fulfilled") if receipts and accepted else None
    view = DeliveryView(
        contract=contract,
        assessment=assessment,
        bindings=active,
        contexts=(),
        integration_receipt=integration,
        fulfillment_receipt=fulfillment,
        history={"items": [], "omitted_count": 0},
    )
    return cast(dict[str, Any], view.model_dump(mode="json"))


def _evaluated_context_view(
    *,
    required: bool = True,
    context_age_seconds: int = 300,
    merged: bool = True,
    context_missing: bool = False,
) -> dict[str, Any]:
    """Produce a public view from the same evaluator used by the real Brain."""
    raw = _view()
    reference = RepositoryDocumentReference(
        kind="repository_document",
        repository_id=REPOSITORY_ID,
        sha=HEAD,
        path="docs/runbooks/delivery.md",
        required=required,
    )
    raw["contract"]["content_digest"] = None
    raw["contract"]["context_refs"] = [reference.model_dump(mode="json")]
    contract = ContractRevision.model_validate(raw["contract"], strict=False)
    if not merged:
        raw["bindings"][0]["binding"]["integration_sha"] = None
        raw["bindings"][0]["confirmation"]["evidence"].update(state="open", integration_sha=None)
    binding = BindingEvidence.model_validate(raw["bindings"][0], strict=False)
    finished_at = NOW - timedelta(seconds=context_age_seconds)
    context = ContextPredicate(
        reference_identity=context_reference_identity(reference),
        current_digest=context_reference_digest(reference),
        status="available",
        snapshot_id=UUID("77777777-7777-7777-7777-777777777777"),
        success_confirmation_id=UUID("88888888-8888-8888-8888-888888888888"),
        latest_attempt_confirmation_id=UUID("88888888-8888-8888-8888-888888888888"),
        collection_started_at=finished_at - timedelta(seconds=1),
        collection_finished_at=finished_at,
        last_attempt_at=finished_at,
        last_success_at=finished_at,
        evidence=RepositoryContextEvidence(
            complete=True,
            facts=(
                RepositoryDocumentFact(
                    repository_id=REPOSITORY_ID,
                    commit_sha=HEAD,
                    path=reference.path,
                    tree_sha="7" * 40,
                    blob_sha="8" * 40,
                    mode="100644",
                    status="available",
                ),
            ),
        ),
    )
    contexts = () if context_missing else (context,)
    assessment = evaluate_delivery(
        EvaluationInput(
            contract=contract,
            attempt=1,
            workflow_version=3,
            coordination_status="open",
            coordination_disposition="active",
            is_self_ticket=True,
            active_bindings=(binding,),
            contexts=contexts,
            feature_enabled=True,
            freshness_seconds=600,
        ),
        now=NOW,
    )
    return cast(
        dict[str, Any],
        DeliveryView(
            contract=contract,
            assessment=assessment,
            bindings=(binding,),
            contexts=contexts,
        ).model_dump(mode="json"),
    )


def _receipt(contract: ContractRevision, digest: str, milestone: str) -> MilestoneReceipt:
    explicit = (
        {"requester_project": "brain-v42", "rationale": "accepted"}
        if milestone == "fulfilled"
        else None
    )
    proof = {
        "ticket_id": TICKET,
        "contract_revision": 1,
        "contract_digest": contract.content_digest,
        "attempt": 1,
        "workflow_version": 1,
        "delivery_digest": digest,
        "assessment_id": FROZEN_ASSESSMENT,
        "decision_time": NOW - timedelta(seconds=5),
        "artifact_proofs": [
            {
                "binding_id": BINDING,
                "binding_version": 1,
                "deliverable_key": "canary",
                "repository_id": REPOSITORY_ID,
                "pr_number": PR_NUMBER,
                "head_sha": HEAD,
                "base_sha": BASE,
                "integration_sha": MERGE,
                "integration_revision": None,
                "snapshot_id": FROZEN_SNAPSHOT,
                "snapshot_digest": "6" * 64,
                "success_confirmation_id": FROZEN_CONFIRMATION,
                "latest_attempt_confirmation_id": FROZEN_CONFIRMATION,
                "collection_started_at": NOW - timedelta(seconds=8),
                "collection_finished_at": NOW - timedelta(seconds=6),
            }
        ],
        "brain_context_proofs": [],
        "repository_context_proofs": [],
        "upstream_receipt_ids": [],
        "issuer": {
            "issuer_project": "brain-v42",
            "issuer_identity": "requester" if milestone == "fulfilled" else "observer",
            "issuer_kind": "requester" if milestone == "fulfilled" else "observer",
        },
        "acceptance_basis": "explicit" if milestone == "fulfilled" else None,
        "explicit_acceptance": explicit,
    }
    return MilestoneReceipt.model_validate(
        {
            "id": UUID("55555555-5555-5555-5555-555555555555")
            if milestone == "integration"
            else UUID("66666666-6666-6666-6666-666666666666"),
            "ticket_id": TICKET,
            "milestone": milestone,
            "contract_revision": 1,
            "attempt": 1,
            "contract_digest": contract.content_digest,
            "delivery_digest": digest,
            "issued_at": NOW - timedelta(seconds=5),
            "acceptance_basis": proof["acceptance_basis"],
            "explicit_acceptance": explicit,
            "proof": proof,
        }
    )


def _evidence(view: dict[str, Any]) -> PullRequestEvidence:
    return PullRequestEvidence.model_validate(
        view["bindings"][0]["confirmation"]["evidence"], strict=False
    )


def _set(container: object, path: tuple[str | int, ...], value: object) -> None:
    cursor: Any = container
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value


def _private(path: Path, contents: str) -> Path:
    path.write_text(contents, encoding="utf-8")
    path.chmod(0o600)
    return path


def _canary_config(canary: ModuleType, token: Path, url: str, tmp_path: Path) -> Any:
    view = _view()
    return canary.CanaryConfig.model_validate(
        {
            "deployment_config": tmp_path / "deployment.json",
            "mcp_url": url,
            "mcp_token_file": token,
            **_scope(view),
        }
    )


def _observer_env(tmp_path: Path) -> Path:
    return _private(
        tmp_path / "observer.env",
        "BRAIN_DELIVERY_ENABLED=true\n"
        "BRAIN_DELIVERY_GITHUB_TOKEN=github-fixture-token\n"
        "BRAIN_DELIVERY_POSTGRES_URL=postgresql://unused:unused@127.0.0.1:1/unused\n"
        'BRAIN_DELIVERY_REPOSITORY_REGISTRY={"brain-v42":{"1337360966":"hawkixs/brain-v42"}}\n'
        "BRAIN_DELIVERY_GITHUB_API_ORIGIN=https://api.github.test\n",
    )


def _github_pr() -> dict[str, Any]:
    return {
        "id": 7001,
        "number": PR_NUMBER,
        "state": "closed",
        "merged": True,
        "draft": False,
        "mergeable": True,
        "merge_commit_sha": MERGE,
        "user": {"id": 800, "login": "executor"},
        "head": {
            "sha": HEAD,
            "repo": {"id": REPOSITORY_ID, "full_name": "hawkixs/brain-v42"},
            "ref": "canary",
        },
        "base": {
            "sha": BASE,
            "ref": "main",
            "repo": {"id": REPOSITORY_ID, "full_name": "hawkixs/brain-v42"},
        },
    }


def _github_check(index: int, name: str) -> dict[str, Any]:
    record_id = 8000 + index
    return {
        "id": record_id,
        "name": name,
        "head_sha": SYNTHETIC,
        "status": "completed",
        "conclusion": "success",
        "started_at": "2026-09-08T11:58:00Z",
        "completed_at": "2026-09-08T11:59:00Z",
        "app": {"id": APP_ID, "slug": "github-actions"},
        "check_suite": {"id": 9000 + index},
        "url": (f"https://api.github.test/repos/hawkixs/brain-v42/check-runs/{record_id}"),
        "pull_requests": [],
    }


@asynccontextmanager
async def _serve_loopback(app: Any) -> AsyncIterator[str]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="on", log_level="error")
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _attempt in range(200):
            if server.started:
                break
            if task.done():
                await task
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("Uvicorn loopback server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        listener.close()
