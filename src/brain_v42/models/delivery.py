"""Validated, transport-safe contracts for observable delivery workflows."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

_MAX_CONTRACT_BYTES = 256 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class DeliveryError(Exception):
    """A domain error with a stable, safe-to-render code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _parse_uuid(value: UUID | str) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ValueError("must be a UUID")
    try:
        return UUID(value)
    except ValueError as error:
        raise ValueError("must be a UUID") from error


UUIDValue = Annotated[UUID, BeforeValidator(_parse_uuid)]


def _reject_surrogates(value: str) -> str:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("Unicode surrogate code points are not supported")
    return value


def _validate_sha(value: str) -> str:
    _reject_surrogates(value)
    if not _SHA_RE.fullmatch(value):
        raise ValueError("sha must be 40 or 64 lowercase hexadecimal characters")
    return value


def _validate_digest(value: str) -> str:
    _reject_surrogates(value)
    if not _DIGEST_RE.fullmatch(value):
        raise ValueError("digest must be 64 lowercase hexadecimal characters")
    return value


def _validate_relative_path(value: str, *, field_name: str) -> str:
    _reject_surrogates(value)
    if not value or value.startswith("/") or "\\" in value:
        raise ValueError(f"{field_name} must be a relative repository path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"{field_name} must not traverse the repository")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)


class _StoredModel(_StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True, frozen=True)


class RequiredCheck(_StrictModel):
    """A unique trusted check selector required for one deliverable."""

    kind: Literal["check_run", "commit_status"]
    name: str = Field(min_length=1, max_length=200)
    app_slug: str | None = Field(default=None, min_length=1, max_length=200)
    provider_id: StrictInt | None = Field(default=None, gt=0)

    @field_validator("name", "app_slug")
    @classmethod
    def _no_surrogates(cls, value: str | None) -> str | None:
        return _reject_surrogates(value) if value is not None else None

    @model_validator(mode="after")
    def _requires_provider_identity(self) -> RequiredCheck:
        if self.app_slug is None and self.provider_id is None:
            raise ValueError("a check requires app_slug or provider_id")
        return self

    def selector(self) -> tuple[str, str, str | None, int | None]:
        return (self.kind, self.name, self.app_slug, self.provider_id)


class ReviewPolicy(_StrictModel):
    required_approvals: StrictInt = Field(ge=0, le=100)
    allowed_reviewers: list[str] = Field(max_length=200)

    @field_validator("allowed_reviewers")
    @classmethod
    def _reviewers_are_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_reviewers contains duplicate identities")
        return [_reject_surrogates(reviewer) for reviewer in value]


class Deliverable(_StrictModel):
    key: str = Field(min_length=1, max_length=64)
    repository: str = Field(min_length=3, max_length=201)
    repository_id: StrictInt | None = Field(default=None, gt=0)
    target_branch: str = Field(min_length=1, max_length=255)
    required_checks: list[RequiredCheck] = Field(max_length=100)
    no_checks_reason: str | None = Field(default=None, min_length=1, max_length=2000)
    review: ReviewPolicy

    @field_validator("key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        _reject_surrogates(value)
        if not _KEY_RE.fullmatch(value):
            raise ValueError("key must be a stable lowercase delivery key")
        return value

    @field_validator("repository")
    @classmethod
    def _valid_repository(cls, value: str) -> str:
        _reject_surrogates(value)
        if not _REPOSITORY_RE.fullmatch(value):
            raise ValueError("repository must be an owner/name identity")
        return value

    @field_validator("target_branch")
    @classmethod
    def _valid_target_branch(cls, value: str) -> str:
        return _validate_relative_path(value, field_name="target_branch")

    @field_validator("no_checks_reason")
    @classmethod
    def _no_surrogates(cls, value: str | None) -> str | None:
        return _reject_surrogates(value) if value is not None else None

    @model_validator(mode="after")
    def _explicit_check_policy(self) -> Deliverable:
        if not self.required_checks and self.no_checks_reason is None:
            raise ValueError("no_checks_reason is required when required_checks is empty")
        selectors = [check.selector() for check in self.required_checks]
        if len(selectors) != len(set(selectors)):
            raise ValueError("duplicate required check selector")
        return self


class BrainEntityReference(_StrictModel):
    kind: Literal["brain_entity"]
    entity_type: Literal["knowledge", "decision", "runbook", "plan", "project_context"]
    entity_id: UUIDValue
    content_snapshot: str = Field(min_length=1, max_length=65536)
    content_digest: str = Field(min_length=64, max_length=64)
    required: StrictBool = True

    @field_validator("content_snapshot")
    @classmethod
    def _no_surrogates(cls, value: str) -> str:
        return _reject_surrogates(value)

    @field_validator("content_digest")
    @classmethod
    def _valid_digest(cls, value: str) -> str:
        return _validate_digest(value)


class RepositoryDocumentReference(_StrictModel):
    kind: Literal["repository_document"]
    repository_id: StrictInt = Field(gt=0)
    sha: str = Field(min_length=40, max_length=64)
    path: str = Field(min_length=1, max_length=4096)
    required: StrictBool = True

    @field_validator("sha")
    @classmethod
    def _valid_sha(cls, value: str) -> str:
        return _validate_sha(value)

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        return _validate_relative_path(value, field_name="path")


class UrlReference(_StrictModel):
    kind: Literal["url"]
    url: str = Field(min_length=1, max_length=4096)
    required: StrictBool = False

    @field_validator("url")
    @classmethod
    def _valid_url(cls, value: str) -> str:
        _reject_surrogates(value)
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute HTTP(S) URL")
        return value

    @model_validator(mode="after")
    def _reference_only(self) -> UrlReference:
        if self.required:
            raise ValueError("URL context is reference-only and cannot satisfy a required proof")
        return self


ContextReference = Annotated[
    BrainEntityReference | RepositoryDocumentReference | UrlReference,
    Field(discriminator="kind"),
]


class DeliveryDependency(_StrictModel):
    ticket_id: UUIDValue
    contract_revision: StrictInt = Field(gt=0)
    attempt: StrictInt = Field(gt=0)
    milestone: Literal["integrated", "accepted"]

    def identity(self) -> tuple[UUID, int, int, str]:
        return (self.ticket_id, self.contract_revision, self.attempt, self.milestone)


class ContractInput(_StrictModel):
    """Caller-owned, validated contract content before repository normalization."""

    schema_version: Literal[1] = 1
    objective: str = Field(min_length=1, max_length=8000)
    constraints: list[str] = Field(default_factory=list, max_length=200)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=200)
    priority: StrictInt = Field(ge=0, le=10000)
    context_refs: list[ContextReference] = Field(default_factory=list, max_length=32)
    dependencies: list[DeliveryDependency] = Field(default_factory=list, max_length=32)
    deliverables: list[Deliverable] = Field(min_length=1, max_length=20)
    acceptance_mode: Literal["automatic", "explicit"]

    @field_validator("objective", "constraints", "acceptance_criteria")
    @classmethod
    def _strings_have_no_surrogates(cls, value: str | list[str]) -> str | list[str]:
        if isinstance(value, str):
            return _reject_surrogates(value)
        return [_reject_surrogates(item) for item in value]

    @model_validator(mode="after")
    def _validate_contract_content(self) -> ContractInput:
        keys = [deliverable.key for deliverable in self.deliverables]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate deliverable key")
        identities = [dependency.identity() for dependency in self.dependencies]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate dependency")
        try:
            encoded = json.dumps(
                self.model_dump(mode="json", by_alias=True),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("Unicode surrogate code points are not supported") from error
        if len(encoded) > _MAX_CONTRACT_BYTES:
            raise ValueError("contract payload exceeds 256 KiB")
        return self


class ContractRevision(ContractInput, _StoredModel):
    """An immutable, normalized stored revision of a delivery contract."""

    ticket_id: UUIDValue
    contract_revision: StrictInt = Field(gt=0)
    content_digest: str = Field(min_length=64, max_length=64)
    author_project: str = Field(min_length=1, max_length=50)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    amendment_reason: str | None = Field(default=None, min_length=1, max_length=4000)

    @field_validator("content_digest")
    @classmethod
    def _valid_content_digest(cls, value: str) -> str:
        return _validate_digest(value)

    @field_validator("author_project", "amendment_reason")
    @classmethod
    def _no_surrogates(cls, value: str | None) -> str | None:
        return _reject_surrogates(value) if value is not None else None

    @model_validator(mode="after")
    def _normalized_and_not_self_dependent(self) -> ContractRevision:
        if any(deliverable.repository_id is None for deliverable in self.deliverables):
            raise ValueError("repository_id must be normalized before storing a contract revision")
        if any(dependency.ticket_id == self.ticket_id for dependency in self.dependencies):
            raise ValueError("self_dependency is not permitted")
        if self.contract_revision > 1 and self.amendment_reason is None:
            raise ValueError("amendment_reason is required after the first revision")
        return self


class ArtifactBinding(_StoredModel):
    """One immutable identity binding a contract deliverable to a GitHub pull request."""

    id: UUIDValue = Field(default_factory=uuid4)
    ticket_id: UUIDValue
    contract_revision: StrictInt = Field(gt=0)
    attempt: StrictInt = Field(gt=0)
    deliverable_key: str = Field(min_length=1, max_length=64)
    repository_id: StrictInt = Field(gt=0)
    pr_number: StrictInt = Field(gt=0)
    head_sha: str = Field(min_length=40, max_length=64)
    base_sha: str = Field(min_length=40, max_length=64)
    integration_sha: str | None = Field(default=None, min_length=40, max_length=64)
    binding_version: StrictInt = Field(default=1, gt=0)

    @field_validator("deliverable_key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        _reject_surrogates(value)
        if not _KEY_RE.fullmatch(value):
            raise ValueError("deliverable_key must be a stable lowercase delivery key")
        return value

    @field_validator("head_sha", "base_sha", "integration_sha")
    @classmethod
    def _valid_shas(cls, value: str | None) -> str | None:
        return _validate_sha(value) if value is not None else None


def contract_content_payload(contract: ContractInput | ContractRevision) -> Mapping[str, Any]:
    """Return the JSON-ready content fields that define a contract digest."""
    payload = contract.model_dump(mode="json", by_alias=True)
    for field_name in {
        "ticket_id",
        "contract_revision",
        "content_digest",
        "author_project",
        "created_at",
        "amendment_reason",
    }:
        payload.pop(field_name, None)
    return payload
