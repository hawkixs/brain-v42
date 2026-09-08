#!/usr/bin/env python3
"""Verify one persisted delivery generation without mutating Brain or GitHub."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import ssl
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from brain_v42.delivery_observer.auth import GitHubAuthProvider, read_private_file
from brain_v42.delivery_observer.config import load_observer_settings
from brain_v42.delivery_observer.github import GitHubClient
from brain_v42.delivery_observer.transport import GitHubTransport, ProviderError
from brain_v42.models.delivery import (
    BindingEvidence,
    ContractRevision,
    DeliveryView,
    MilestoneReceipt,
    PullRequestEvidence,
    RepositoryDocumentReference,
    context_reference_identity,
)
from brain_v42.models.delivery_evaluator import _select_check
from brain_v42.models.delivery_hashes import delivery_digest

_PHASES = ("missing-proof", "observed", "verified", "integrated", "accepted")
_MAX_RESPONSE_BYTES = 256 * 1024
_CALL_TIMEOUT_SECONDS = 5
_OVERALL_TIMEOUT_SECONDS = 10
_SHA = set("0123456789abcdef")
_MCP_HEADERS = {
    "x-brain-tool-profile": "native",
    "x-brain-agent": "delivery-canary-verifier",
}


def _uuid(value: UUID | str) -> UUID:
    return value if isinstance(value, UUID) else UUID(value)


_UUID = Annotated[UUID, BeforeValidator(_uuid)]


class CanaryFailure(Exception):
    """Only stable non-secret refusal codes cross the command boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CanaryScope(_Strict):
    ticket_id: _UUID
    actor_project: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9._-]+$")
    repository_id: int = Field(gt=0)
    pr_number: int = Field(gt=0)
    deliverable_key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9._-]*$")
    contract_revision: int = Field(gt=0)
    attempt: int = Field(gt=0)
    contract_digest: str = Field(min_length=64, max_length=64)
    expected_head: str | None = Field(default=None, min_length=40, max_length=64)
    expected_delivery_digest: str | None = Field(default=None, min_length=64, max_length=64)
    prior_success_confirmation_id: _UUID | None = None
    not_before: datetime | None = None

    @field_validator("contract_digest", "expected_delivery_digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and (len(value) != 64 or set(value) - _SHA):
            raise ValueError("digest")
        return value

    @field_validator("expected_head")
    @classmethod
    def _head(cls, value: str | None) -> str | None:
        if value is not None and (len(value) not in {40, 64} or set(value) - _SHA):
            raise ValueError("head")
        return value

    @field_validator("not_before")
    @classmethod
    def _aware_not_before(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("not_before must be timezone-aware")
        return value.astimezone(UTC) if value is not None else None


class CanaryConfig(CanaryScope):
    deployment_config: Path
    mcp_url: str = Field(min_length=1, max_length=2048)
    mcp_token_file: Path

    @field_validator("deployment_config", "mcp_token_file")
    @classmethod
    def _absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("absolute path required")
        return value


@dataclass(frozen=True, slots=True)
class PreflightReceipt:
    source_sha: str
    schema_revision: str
    observer_env: Path


class _BoundedStream(httpx.AsyncByteStream):
    def __init__(self, inner: httpx.AsyncByteStream) -> None:
        self._inner = inner
        self._size = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._size += len(chunk)
            if self._size > _MAX_RESPONSE_BYTES:
                await self._inner.aclose()
                raise httpx.ReadError("response limit exceeded")
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _BoundedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport(
            verify=ssl.create_default_context(), retries=0
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        try:
            encoding = response.headers.get("Content-Encoding", "identity").lower()
            length = response.headers.get("Content-Length")
            invalid = encoding != "identity" or (
                length is not None and not 0 <= int(length) <= _MAX_RESPONSE_BYTES
            )
        except (TypeError, ValueError):
            await response.aclose()
            raise httpx.ReadError("response limit exceeded", request=request) from None
        if invalid:
            await response.aclose()
            raise httpx.ReadError("response limit exceeded", request=request)
        if not isinstance(response.stream, httpx.AsyncByteStream):
            await response.aclose()
            raise httpx.ReadError("response limit exceeded", request=request)
        response.stream = _BoundedStream(response.stream)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _fail(code: str) -> NoReturn:
    raise CanaryFailure(code)


def _aware_at_or_before(value: datetime | None, now: datetime) -> bool:
    return value is not None and value.utcoffset() is not None and value <= now


def validate_mcp_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            not isinstance(value, str)
            or value != value.strip()
            or "\\" in value
            or any(ord(character) < 33 or ord(character) == 127 for character in value)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
            or not parsed.path
            or parsed.hostname is None
        ):
            raise ValueError
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
        tls = parsed.scheme == "https"
        if not local_http and not tls:
            raise ValueError
        _ = parsed.port
        return value
    except (TypeError, ValueError):
        _fail("brain_transport_or_shape_invalid")


def strict_http_client_factory() -> Callable[..., httpx.AsyncClient]:
    """Keep only the declared MCP headers/auth and override redirect defaults."""

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        if args:
            raise ValueError("keyword-only MCP HTTP factory")
        auth = kwargs.get("auth")
        if not isinstance(auth, BearerAuth):
            raise ValueError("explicit bearer auth required")
        incoming = kwargs.get("headers")
        if not isinstance(incoming, Mapping):
            raise ValueError("declared MCP headers required")
        headers = httpx.Headers(incoming)
        if any(headers.get(key) != value for key, value in _MCP_HEADERS.items()):
            raise ValueError("declared MCP headers required")
        return httpx.AsyncClient(
            transport=_BoundedTransport(),
            auth=auth,
            headers={**_MCP_HEADERS, "Accept-Encoding": "identity"},
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(_CALL_TIMEOUT_SECONDS),
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    return factory


def _private_token(path: Path) -> str:
    try:
        if not path.is_absolute():
            raise ValueError
        value = cast(bytes, read_private_file(path)).decode("ascii")
        if (
            value != value.strip()
            or not 1 <= len(value) <= 8192
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
        ):
            raise ValueError
        return value
    except (ProviderError, OSError, UnicodeError, ValueError):
        _fail("brain_transport_or_shape_invalid")


def _private_json(path: Path) -> dict[str, object]:
    try:
        if not path.is_absolute():
            raise ValueError
        value = json.loads(read_private_file(path))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (ProviderError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
        _fail("brain_transport_or_shape_invalid")


def _load_config(path: Path) -> CanaryConfig:
    try:
        if not path.is_absolute():
            raise ValueError
        config = CanaryConfig.model_validate_json(read_private_file(path))
        validate_mcp_url(config.mcp_url)
        return config
    except CanaryFailure:
        raise
    except (ProviderError, OSError, ValidationError, ValueError):
        _fail("brain_transport_or_shape_invalid")


def _load_preflight_checker() -> Callable[[Path], dict[str, str]]:
    checker_path = Path(__file__).with_name("check_delivery_deployment.py")
    spec = importlib.util.spec_from_file_location("delivery_preflight_9b1", checker_path)
    if spec is None or spec.loader is None:
        raise ValueError("9B1 preflight helper unavailable")
    checker = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = checker
    try:
        spec.loader.exec_module(checker)
    except Exception:
        if sys.modules.get(spec.name) is checker:
            del sys.modules[spec.name]
        raise
    check = getattr(checker, "check", None)
    if not callable(check):
        raise ValueError("9B1 preflight helper unavailable")
    return cast(Callable[[Path], dict[str, str]], check)


def _deployment_canary(path: Path) -> PreflightReceipt:
    """Rerun the accepted 9B1 checker and retain only its public identity."""
    try:
        deployment = _private_json(path)
        writers = deployment.get("writers")
        observer = (
            writers.get("brain-v42-delivery-observer.service")
            if isinstance(writers, dict)
            else None
        )
        observer_env_value = deployment.get("observer_env_file")
        if (
            deployment.get("mode") != "canary"
            or not isinstance(observer, dict)
            or observer.get("must_be_active") is not True
            or not isinstance(observer_env_value, str)
        ):
            raise ValueError
        observer_env = Path(observer_env_value)
        if not observer_env.is_absolute():
            raise ValueError
        result = _load_preflight_checker()(path)
        if not isinstance(result, dict) or set(result) != {"source_sha", "schema_revision"}:
            raise ValueError
        source_sha, schema_revision = result["source_sha"], result["schema_revision"]
        if (
            not isinstance(source_sha, str)
            or len(source_sha) != 40
            or set(source_sha) - _SHA
            or schema_revision != "053"
        ):
            raise ValueError
        return PreflightReceipt(source_sha, schema_revision, observer_env)
    except CanaryFailure:
        raise
    except Exception:
        _fail("running_artifact_unverified")


async def _read_view(config: CanaryConfig) -> dict[str, object]:
    token = _private_token(config.mcp_token_file)
    try:
        transport = StreamableHttpTransport(
            validate_mcp_url(config.mcp_url),
            auth=token,
            headers=dict(_MCP_HEADERS),
            httpx_client_factory=strict_http_client_factory(),
        )
        async with asyncio.timeout(_OVERALL_TIMEOUT_SECONDS):
            async with Client(
                transport,
                timeout=_CALL_TIMEOUT_SECONDS,
                init_timeout=_CALL_TIMEOUT_SECONDS,
            ) as client:
                result = await client.call_tool(
                    "brain_delivery_get",
                    {
                        "ticket_id": str(config.ticket_id),
                        "actor_project": config.actor_project,
                        "history_limit": 20,
                        "history_cursor": None,
                    },
                    timeout=_CALL_TIMEOUT_SECONDS,
                )
        payload = result.structured_content
        if not isinstance(payload, dict):
            raise ValueError
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
        if len(encoded) > _MAX_RESPONSE_BYTES:
            raise ValueError
        return payload
    except CanaryFailure:
        raise
    except (TimeoutError, ValueError, TypeError, httpx.HTTPError, OSError):
        _fail("brain_transport_or_shape_invalid")
    except Exception:
        _fail("brain_transport_or_shape_invalid")


async def _collect_github(
    observer_env: Path,
    binding: Any,
    contract: ContractRevision,
    previous: PullRequestEvidence,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], datetime] | None = None,
) -> PullRequestEvidence:
    """Collect one current PR fence through the production read-only adapter."""
    try:
        if not observer_env.is_absolute():
            raise ValueError
        settings = load_observer_settings(observer_env)
        deliverables = [
            item for item in contract.deliverables if item.key == binding.deliverable_key
        ]
        registered = settings.repositories_for(contract.author_project)
        if (
            len(deliverables) != 1
            or registered.get(binding.repository_id) != deliverables[0].repository
        ):
            raise ValueError
        selected_transport = transport or httpx.AsyncHTTPTransport(
            verify=ssl.create_default_context(), retries=0
        )
        async with httpx.AsyncClient(
            transport=selected_transport,
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(settings.request_timeout_seconds),
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        ) as http:
            github_transport = GitHubTransport(http, settings)
            auth = GitHubAuthProvider(settings, github_transport)
            github = GitHubClient(http, settings, auth, now=now or (lambda: datetime.now(UTC)))
            return await github.collect(binding, contract, previous=previous)
    except (ProviderError, ValidationError, ValueError, OSError, httpx.HTTPError):
        _fail("current_github_proof_invalid")


def _scope(raw: dict[str, object] | CanaryScope) -> CanaryScope:
    if isinstance(raw, CanaryScope):
        return raw
    try:
        return CanaryScope.model_validate_json(json.dumps(raw, default=str))
    except (ValidationError, ValueError, TypeError):
        _fail("canary_identity_mismatch")


def _strict_wire_identities(raw: dict[str, object]) -> bool:
    """Reject coercible identity primitives while permitting RFC3339 timestamps."""

    def object_value(value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise TypeError
        return value

    def list_value(value: object) -> list[object]:
        if not isinstance(value, list):
            raise TypeError
        return value

    def exact(mapping: dict[str, object], strings: tuple[str, ...], ints: tuple[str, ...]) -> None:
        if any(not isinstance(mapping.get(name), str) for name in strings):
            raise TypeError
        if any(type(mapping.get(name)) is not int for name in ints):
            raise TypeError

    try:
        contract = object_value(raw.get("contract"))
        exact(
            contract,
            ("ticket_id", "content_digest", "author_project"),
            ("contract_revision",),
        )
        for deliverable_value in list_value(contract.get("deliverables")):
            deliverable = object_value(deliverable_value)
            exact(deliverable, ("key", "repository"), ("repository_id",))
        assessment = object_value(raw.get("assessment"))
        exact(
            assessment,
            ("assessment_id", "delivery_digest"),
            ("assessment_version",),
        )
        for item_value in list_value(raw.get("bindings")):
            item = object_value(item_value)
            binding = object_value(item.get("binding"))
            exact(
                binding,
                ("id", "ticket_id", "deliverable_key"),
                (
                    "contract_revision",
                    "attempt",
                    "repository_id",
                    "pr_number",
                    "binding_version",
                ),
            )
            confirmation_value = item.get("confirmation")
            if confirmation_value is None:
                continue
            confirmation = object_value(confirmation_value)
            exact(confirmation, ("id",), ())
            evidence_value = confirmation.get("evidence")
            if evidence_value is None:
                continue
            evidence = object_value(evidence_value)
            exact(
                evidence,
                ("head_sha", "base_sha"),
                ("provider_id", "repository_id", "pr_number", "head_repository_id"),
            )
            for check_value in list_value(evidence.get("checks")):
                check = object_value(check_value)
                exact(check, ("head_sha", "name", "kind"), ("record_id", "provider_id"))
                suite = check.get("check_suite_id")
                if suite is not None and type(suite) is not int:
                    raise TypeError
            for review_value in list_value(evidence.get("reviews")):
                review = object_value(review_value)
                exact(review, ("head_sha", "reviewer"), ("record_id", "provider_id"))
        for receipt_name in ("integration_receipt", "fulfillment_receipt"):
            receipt_value = raw.get(receipt_name)
            if receipt_value is None:
                continue
            receipt = object_value(receipt_value)
            exact(
                receipt,
                ("id", "ticket_id", "contract_digest", "delivery_digest"),
                ("contract_revision", "attempt"),
            )
            proof = object_value(receipt.get("proof"))
            exact(
                proof,
                ("ticket_id", "contract_digest", "delivery_digest", "assessment_id"),
                ("contract_revision", "attempt", "workflow_version"),
            )
            for artifact_value in list_value(proof.get("artifact_proofs")):
                artifact = object_value(artifact_value)
                exact(
                    artifact,
                    ("binding_id", "deliverable_key", "head_sha", "base_sha"),
                    ("binding_version", "repository_id", "pr_number"),
                )
    except (KeyError, TypeError):
        return False
    return True


def _parse_view(raw: dict[str, object], *, receipt_phase: bool = False) -> DeliveryView:
    try:
        if not _strict_wire_identities(raw):
            raise ValueError("non-strict wire identity")
        return DeliveryView.model_validate(raw, strict=False)
    except ValidationError as error:
        if receipt_phase and any(
            detail.get("loc", (None,))[0] in {"integration_receipt", "fulfillment_receipt"}
            for detail in error.errors(include_url=False)
        ):
            _fail("receipt_invalid_or_missing")
        _fail("brain_transport_or_shape_invalid")
    except (ValueError, TypeError):
        _fail("brain_transport_or_shape_invalid")


def _contract_scope(view: DeliveryView, scope: CanaryScope) -> None:
    contract = view.contract
    deliverables = [item for item in contract.deliverables if item.key == scope.deliverable_key]
    if (
        contract.ticket_id != scope.ticket_id
        or contract.contract_revision != scope.contract_revision
        or contract.content_digest != scope.contract_digest
        or contract.author_project != scope.actor_project
        or len(contract.deliverables) != 1
        or len(deliverables) != 1
        or deliverables[0].repository_id != scope.repository_id
    ):
        _fail("canary_identity_mismatch")


def _binding_scope(view: DeliveryView, scope: CanaryScope) -> BindingEvidence:
    _contract_scope(view, scope)
    if len(view.bindings) != 1:
        _fail("canary_identity_mismatch")
    item = view.bindings[0]
    binding = item.binding
    if (
        binding.ticket_id != scope.ticket_id
        or binding.contract_revision != scope.contract_revision
        or binding.attempt != scope.attempt
        or binding.deliverable_key != scope.deliverable_key
        or binding.repository_id != scope.repository_id
        or binding.pr_number != scope.pr_number
    ):
        _fail("canary_identity_mismatch")
    return item


def _has_current_proof(view: DeliveryView, scope: CanaryScope, now: datetime) -> bool:
    if not view.bindings:
        return False
    item = _binding_scope(view, scope)
    confirmation = item.confirmation
    return bool(
        item.binding.state == "observed"
        and confirmation is not None
        and confirmation.outcome == "success"
        and confirmation.evidence is not None
        and confirmation.evidence.complete
        and item.snapshot_id is not None
        and item.success_confirmation_id == confirmation.id
        and item.latest_attempt_confirmation_id == confirmation.id
        and item.last_attempt_outcome == "success"
        and view.assessment.observation_health == "fresh"
        and view.assessment.fresh_until is not None
        and view.assessment.fresh_until.utcoffset() is not None
        and view.assessment.fresh_until > now
    )


def _fresh(view: DeliveryView, scope: CanaryScope, now: datetime) -> BindingEvidence:
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    item = _binding_scope(view, scope)
    binding = item.binding
    confirmation = item.confirmation
    if scope.expected_head is None:
        _fail("canary_identity_mismatch")
    if (
        binding.state != "observed"
        or binding.head_sha != scope.expected_head
        or confirmation is None
        or confirmation.outcome != "success"
        or confirmation.evidence is None
    ):
        _fail("canary_identity_mismatch")
    evidence = confirmation.evidence
    deliverable = view.contract.deliverables[0]
    if (
        evidence.repository_id != binding.repository_id
        or evidence.pr_number != binding.pr_number
        or evidence.head_sha != binding.head_sha
        or evidence.base_sha != binding.base_sha
        or evidence.integration_sha != binding.integration_sha
        or evidence.base_ref != deliverable.target_branch
    ):
        _fail("canary_identity_mismatch")
    assessment = view.assessment
    required_repository_identities = {
        context_reference_identity(reference)
        for reference in view.contract.context_refs
        if reference.required and isinstance(reference, RepositoryDocumentReference)
    }
    observation_times = [confirmation.collection_finished_at] + [
        context.collection_finished_at
        for context in view.contexts
        if context.reference_identity in required_repository_identities
        and context.status == "available"
        and context.collection_finished_at is not None
        and context.collection_finished_at.utcoffset() is not None
    ]
    computed_digest = delivery_digest(
        contract_digest=scope.contract_digest,
        attempt=scope.attempt,
        active_bindings=view.bindings,
    )
    if (
        assessment.observation_health != "fresh"
        or assessment.observed_at is None
        or assessment.fresh_until is None
        or assessment.observed_at.utcoffset() is None
        or assessment.fresh_until.utcoffset() is None
        or assessment.assessed_at.utcoffset() is None
        or assessment.assessed_at > now
        or assessment.observed_at > now
        or assessment.fresh_until <= now
        or assessment.delivery_digest != computed_digest
        or (
            scope.expected_delivery_digest is not None
            and assessment.delivery_digest != scope.expected_delivery_digest
        )
        or not evidence.complete
        or not _aware_at_or_before(evidence.collected_at, now)
        or item.snapshot_id is None
        or item.success_confirmation_id != confirmation.id
        or item.latest_attempt_confirmation_id != confirmation.id
        or item.last_attempt_outcome != "success"
        or item.last_attempt_at != confirmation.collection_finished_at
        or item.last_success_at != confirmation.collection_finished_at
        or not _aware_at_or_before(confirmation.collection_started_at, now)
        or not _aware_at_or_before(confirmation.collection_finished_at, now)
        or confirmation.collection_finished_at < confirmation.collection_started_at
        or not (
            confirmation.collection_started_at
            <= evidence.collected_at
            <= confirmation.collection_finished_at
        )
        or assessment.assessed_at < max(observation_times)
        # The shared evaluator uses the oldest required observation, which may
        # be a pinned repository context collected before the latest PR poll.
        or assessment.observed_at != min(observation_times)
    ):
        _fail("current_generation_unverified")
    if (
        scope.prior_success_confirmation_id is not None
        and confirmation.id == scope.prior_success_confirmation_id
    ):
        _fail("current_generation_unverified")
    if scope.not_before is not None and confirmation.collection_finished_at <= scope.not_before:
        _fail("current_generation_unverified")
    return item


def _live_identity(
    view: DeliveryView,
    item: BindingEvidence,
    live: PullRequestEvidence | None,
    now: datetime,
) -> PullRequestEvidence:
    if live is None or item.confirmation is None or item.confirmation.evidence is None:
        _fail("current_github_proof_invalid")
    retained = item.confirmation.evidence
    binding = item.binding
    fields = (
        "provider_id",
        "repository_id",
        "pr_number",
        "author_id",
        "head_repository_id",
        "head_sha",
        "base_sha",
        "base_ref",
        "integration_sha",
        "integration_revision",
        "state",
        "draft",
    )
    if (
        not live.complete
        or not _aware_at_or_before(live.collected_at, now)
        or any(getattr(live, field) != getattr(retained, field) for field in fields)
        or live.repository_id != binding.repository_id
        or live.pr_number != binding.pr_number
        or live.head_sha != binding.head_sha
        or live.base_sha != binding.base_sha
        or live.integration_sha != binding.integration_sha
        or live.base_ref != view.contract.deliverables[0].target_branch
    ):
        _fail("current_github_proof_invalid")
    return live


def _required_checks(
    contract: ContractRevision, live: PullRequestEvidence
) -> tuple[dict[str, object], ...]:
    selected: list[dict[str, object]] = []
    for required in contract.deliverables[0].required_checks:
        check = _select_check(live, required)
        if check is None or check.conclusion != "success" or check.record_url is None:
            _fail("required_checks_unverified")
        selected.append(
            {
                "record_id": check.record_id,
                "app_id": check.provider_id,
                "kind": check.kind,
                "name": check.name,
                "app_slug": check.app_slug,
                "head_sha": check.head_sha,
                "check_suite_id": check.check_suite_id,
                "record_url": check.record_url,
            }
        )
    return tuple(selected)


def _receipt(
    receipt: MilestoneReceipt | None,
    milestone: str,
    view: DeliveryView,
    scope: CanaryScope,
    item: BindingEvidence,
    live: PullRequestEvidence,
    now: datetime,
) -> tuple[MilestoneReceipt, dict[str, object]]:
    if receipt is None or receipt.milestone != milestone:
        _fail("receipt_invalid_or_missing")
    proof = receipt.proof
    if (
        receipt.ticket_id != scope.ticket_id
        or receipt.contract_revision != scope.contract_revision
        or receipt.attempt != scope.attempt
        or receipt.contract_digest != scope.contract_digest
        or receipt.delivery_digest != view.assessment.delivery_digest
        or proof.workflow_version > view.assessment.assessment_version
        or len(proof.artifact_proofs) != 1
        or not _aware_at_or_before(receipt.issued_at, now)
        or not _aware_at_or_before(proof.decision_time, now)
    ):
        _fail("receipt_invalid_or_missing")
    frozen = proof.artifact_proofs[0]
    binding = item.binding
    retained = item.confirmation.evidence if item.confirmation is not None else None
    if retained is None or (
        frozen.binding_id != binding.id
        or frozen.binding_version > binding.binding_version
        or frozen.deliverable_key != binding.deliverable_key
        or frozen.repository_id != binding.repository_id
        or frozen.pr_number != binding.pr_number
        or frozen.head_sha != binding.head_sha
        or frozen.base_sha != binding.base_sha
        or frozen.integration_sha != binding.integration_sha
        or frozen.integration_revision != retained.integration_revision
        or frozen.head_sha != live.head_sha
        or frozen.base_sha != live.base_sha
        or frozen.integration_sha != live.integration_sha
        or frozen.integration_revision != live.integration_revision
        or not _aware_at_or_before(frozen.collection_started_at, now)
        or not _aware_at_or_before(frozen.collection_finished_at, now)
    ):
        _fail("receipt_invalid_or_missing")
    public = {
        "id": str(receipt.id),
        "milestone": receipt.milestone,
        "workflow_version": proof.workflow_version,
        "assessment_id": proof.assessment_id,
        "decision_time": proof.decision_time.isoformat(),
        "binding_id": str(frozen.binding_id),
        "binding_version": frozen.binding_version,
        "snapshot_id": str(frozen.snapshot_id),
        "snapshot_digest": frozen.snapshot_digest,
        "success_confirmation_id": str(frozen.success_confirmation_id),
        "latest_attempt_confirmation_id": str(frozen.latest_attempt_confirmation_id),
        "head_sha": frozen.head_sha,
        "base_sha": frozen.base_sha,
        "integration_sha": frozen.integration_sha,
        "integration_revision": frozen.integration_revision,
        "collection_started_at": frozen.collection_started_at.isoformat(),
        "collection_finished_at": frozen.collection_finished_at.isoformat(),
    }
    return receipt, public


def verify_view(
    raw: dict[str, object],
    config: dict[str, object] | CanaryScope,
    phase: str,
    *,
    github_evidence: PullRequestEvidence | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Validate one public view and independently collected GitHub evidence."""
    if phase not in _PHASES:
        _fail("brain_transport_or_shape_invalid")
    scope = _scope(config)
    view = _parse_view(raw, receipt_phase=phase in {"integrated", "accepted"})
    observed_now = (now or datetime.now(UTC)).astimezone(UTC)
    _contract_scope(view, scope)
    if phase == "missing-proof":
        if (
            _has_current_proof(view, scope, observed_now)
            or view.assessment.requirements_satisfied
            or view.assessment.integration_receipt_eligible
            or view.assessment.completion_eligible_now
            or view.assessment.contract_fulfilled
            or view.assessment.acceptance_state == "accepted"
            or view.integration_receipt is not None
            or view.fulfillment_receipt is not None
        ):
            _fail("current_generation_unverified")
        return {
            "outcome": "expected_missing_proof",
            "contract": {
                "ticket_id": str(view.contract.ticket_id),
                "contract_revision": view.contract.contract_revision,
                "contract_digest": view.contract.content_digest,
                "deliverable_key": scope.deliverable_key,
                "repository_id": scope.repository_id,
            },
            "assessment": {
                "id": view.assessment.assessment_id,
                "version": view.assessment.assessment_version,
                "health": view.assessment.observation_health,
            },
        }

    item = _fresh(view, scope, observed_now)
    live = _live_identity(view, item, github_evidence, observed_now)
    confirmation = item.confirmation
    if confirmation is None:
        _fail("current_generation_unverified")
    result: dict[str, object] = {
        "outcome": phase,
        "contract": {
            "ticket_id": str(view.contract.ticket_id),
            "contract_revision": view.contract.contract_revision,
            "contract_digest": view.contract.content_digest,
            "deliverable_key": item.binding.deliverable_key,
            "repository_id": item.binding.repository_id,
            "pr_number": item.binding.pr_number,
            "attempt": item.binding.attempt,
        },
        "assessment": {
            "id": view.assessment.assessment_id,
            "version": view.assessment.assessment_version,
            "assessed_at": view.assessment.assessed_at.isoformat(),
            "observed_at": view.assessment.observed_at.isoformat()
            if view.assessment.observed_at
            else None,
            "fresh_until": view.assessment.fresh_until.isoformat()
            if view.assessment.fresh_until
            else None,
            "delivery_digest": view.assessment.delivery_digest,
            "stage": view.assessment.delivery_stage,
            "requirements_satisfied": view.assessment.requirements_satisfied,
        },
        "binding": {
            "id": str(item.binding.id),
            "binding_version": item.binding.binding_version,
            "head_sha": item.binding.head_sha,
            "base_sha": item.binding.base_sha,
            "integration_sha": item.binding.integration_sha,
        },
        "current_confirmation": {
            "id": str(confirmation.id),
            "snapshot_id": str(item.snapshot_id),
            "collection_started_at": confirmation.collection_started_at.isoformat(),
            "collection_finished_at": confirmation.collection_finished_at.isoformat(),
        },
        "github": {
            "provider_id": live.provider_id,
            "repository_id": live.repository_id,
            "pr_number": live.pr_number,
            "head_sha": live.head_sha,
            "base_sha": live.base_sha,
            "integration_sha": live.integration_sha,
            "integration_revision": live.integration_revision,
            "state": live.state,
            "collected_at": live.collected_at.isoformat(),
            "check_records_collected": len(live.checks),
        },
    }
    if phase in {"verified", "integrated", "accepted"}:
        # The global requirements predicate includes integration. Before merge,
        # exactly that outstanding deliverable predicate is expected; any other
        # blocker (context, dependency, review, conflict, ...) still refuses.
        awaiting_merge_only = (
            phase == "verified"
            and live.state == "open"
            and len(view.assessment.blockers) == 1
            and view.assessment.blockers[0].code == "pr_not_merged"
            and view.assessment.blockers[0].deliverable_key == item.binding.deliverable_key
        )
        if not (
            view.assessment.requirements_satisfied or awaiting_merge_only
        ) or view.assessment.delivery_stage not in {"verified", "integrated"}:
            _fail("current_generation_unverified")
        result["required_checks"] = _required_checks(view.contract, live)
    if phase in {"integrated", "accepted"}:
        if (
            view.assessment.delivery_stage != "integrated"
            or not view.assessment.integration_receipt_eligible
        ):
            _fail("current_generation_unverified")
        _, public = _receipt(
            view.integration_receipt,
            "integration",
            view,
            scope,
            item,
            live,
            observed_now,
        )
        result["integration_receipt"] = public
    if phase == "accepted":
        if not (
            view.contract.acceptance_mode == "explicit"
            and view.assessment.acceptance_state == "accepted"
            and view.assessment.contract_fulfilled
            and view.assessment.completion_eligible_now
        ):
            _fail("receipt_invalid_or_missing")
        receipt, public = _receipt(
            view.fulfillment_receipt,
            "fulfilled",
            view,
            scope,
            item,
            live,
            observed_now,
        )
        if (
            receipt.acceptance_basis != "explicit"
            or receipt.explicit_acceptance is None
            or receipt.explicit_acceptance.requester_project != view.contract.author_project
        ):
            _fail("receipt_invalid_or_missing")
        result["fulfillment_receipt"] = public
    return result


def _scope_from_config(config: CanaryConfig) -> CanaryScope:
    return CanaryScope.model_validate(
        {name: getattr(config, name) for name in CanaryScope.model_fields}
    )


async def _run(
    config: CanaryConfig,
    phase: str,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, object]:
    time_source = clock or (lambda: datetime.now(UTC))
    preflight = _deployment_canary(config.deployment_config)
    raw = await _read_view(config)
    brain_observed_at = time_source()
    if brain_observed_at.utcoffset() is None:
        _fail("current_generation_unverified")
    brain_observed_at = brain_observed_at.astimezone(UTC)
    scope = _scope_from_config(config)
    live: PullRequestEvidence | None = None
    if phase != "missing-proof":
        view = _parse_view(raw, receipt_phase=phase in {"integrated", "accepted"})
        item = _fresh(view, scope, brain_observed_at)
        if item.confirmation is None or item.confirmation.evidence is None:
            _fail("current_generation_unverified")
        live = await _collect_github(
            preflight.observer_env,
            item.binding,
            view.contract,
            item.confirmation.evidence,
            now=time_source,
        )
    validation_time = time_source()
    if validation_time.utcoffset() is None:
        _fail("current_generation_unverified")
    receipt = verify_view(
        raw,
        scope,
        phase,
        github_evidence=live,
        now=validation_time.astimezone(UTC),
    )
    return receipt | {
        "source": "deployment_preflight_rerun",
        "source_sha": preflight.source_sha,
        "schema_revision": preflight.schema_revision,
    }


def _emit(status: str, phase: str, **values: object) -> None:
    payload = {
        "status": status,
        "phase": phase,
        "timestamp": datetime.now(UTC).isoformat(),
        **values,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))


def main(argv: list[str] | None = None) -> int:
    try:
        parser = argparse.ArgumentParser(
            description="Verify one immutable delivery canary generation"
        )
        parser.add_argument("--config", required=True, type=Path, metavar="ABSOLUTE_PRIVATE_JSON")
        parser.add_argument("--phase", required=True, choices=_PHASES)
        args = parser.parse_args(argv)
        config = _load_config(args.config)
        receipt = asyncio.run(_run(config, args.phase))
    except CanaryFailure as failure:
        _emit(
            "failed",
            getattr(locals().get("args", None), "phase", "unknown"),
            failure=failure.code,
        )
        return 2
    except Exception:
        _emit(
            "failed",
            getattr(locals().get("args", None), "phase", "unknown"),
            failure="brain_transport_or_shape_invalid",
        )
        return 2
    _emit("ok", args.phase, **receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
