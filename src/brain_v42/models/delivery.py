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


def _lists_to_tuples(value: object) -> object:
    """Accept JSON arrays while retaining immutable tuple values after validation."""
    return tuple(value) if isinstance(value, list) else value


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
    model_config = ConfigDict(
        extra="forbid", strict=True, populate_by_name=True, frozen=True, validate_default=True
    )


class _StoredModel(_StrictModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, populate_by_name=True, frozen=True, validate_default=True
    )


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
    allowed_reviewers: Annotated[tuple[str, ...], BeforeValidator(_lists_to_tuples)] = Field(
        max_length=200
    )

    @field_validator("allowed_reviewers")
    @classmethod
    def _reviewers_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_reviewers contains duplicate identities")
        return tuple(_reject_surrogates(reviewer) for reviewer in value)


class Deliverable(_StrictModel):
    key: str = Field(min_length=1, max_length=64)
    repository: str = Field(min_length=3, max_length=201)
    repository_id: StrictInt | None = Field(default=None, gt=0)
    target_branch: str = Field(min_length=1, max_length=255)
    required_checks: Annotated[tuple[RequiredCheck, ...], BeforeValidator(_lists_to_tuples)] = (
        Field(max_length=100)
    )
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
    entity_type: Literal["decision", "learning", "snippet", "runbook", "adr", "plan"]
    entity_id: UUIDValue
    required: StrictBool = True


class PinnedBrainEntityReference(BrainEntityReference):
    """Server-resolved Brain context stored with immutable content provenance."""

    content_snapshot: str = Field(min_length=1, max_length=65536)
    content_digest: str = Field(min_length=64, max_length=64)

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


class RepositoryDocumentFact(_StrictModel):
    """A provider observation for one exact repository-document pin, without its body."""

    repository_id: StrictInt = Field(gt=0)
    commit_sha: str = Field(min_length=40, max_length=64)
    path: str = Field(min_length=1, max_length=4096)
    tree_sha: str | None = Field(default=None, min_length=40, max_length=64)
    blob_sha: str | None = Field(default=None, min_length=40, max_length=64)
    mode: Literal["100644", "100755"] | None = None
    status: Literal["available", "missing", "error"]

    _valid_shas = field_validator("commit_sha", "tree_sha", "blob_sha")(
        lambda value: _validate_sha(value) if value is not None else None
    )
    _valid_path = field_validator("path")(
        lambda value: _validate_relative_path(value, field_name="path")
    )

    @model_validator(mode="after")
    def _complete_available_proof(self) -> RepositoryDocumentFact:
        if self.status == "available" and (
            self.tree_sha is None or self.blob_sha is None or self.mode is None
        ):
            raise ValueError("available repository fact requires complete regular-file proof")
        return self

    def identity(self) -> tuple[int, str, str]:
        return (self.repository_id, self.commit_sha, self.path)


class RepositoryContextEvidence(_StrictModel):
    """A bounded immutable set of repository-document facts for one collection."""

    facts: Annotated[tuple[RepositoryDocumentFact, ...], BeforeValidator(_lists_to_tuples)] = Field(
        default_factory=tuple, max_length=32
    )
    complete: StrictBool

    @model_validator(mode="after")
    def _consistent_facts(self) -> RepositoryContextEvidence:
        identities = [fact.identity() for fact in self.facts]
        if len(identities) != len(set(identities)):
            raise ValueError("repository context evidence has duplicate pins")
        if self.complete and (
            not self.facts or any(fact.status != "available" for fact in self.facts)
        ):
            raise ValueError("complete repository context evidence requires available facts")
        return self


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
PinnedContextReference = Annotated[
    PinnedBrainEntityReference | RepositoryDocumentReference | UrlReference,
    Field(discriminator="kind"),
]


def context_reference_identity(reference: PinnedContextReference) -> str:
    """Return the stable identity used to reconcile a stored context predicate."""
    if isinstance(reference, PinnedBrainEntityReference):
        return f"brain_entity:{reference.entity_type}:{reference.entity_id}"
    if isinstance(reference, RepositoryDocumentReference):
        return f"repository_document:{reference.repository_id}:{reference.sha}:{reference.path}"
    return f"url:{reference.url}"


def context_reference_digest(reference: PinnedContextReference) -> str | None:
    """Return the stored proof digest for a required context reference."""
    if isinstance(reference, PinnedBrainEntityReference):
        return reference.content_digest
    if isinstance(reference, RepositoryDocumentReference):
        from brain_v42.models.delivery_hashes import canonical_digest

        return canonical_digest(
            {
                "repository_id": reference.repository_id,
                "sha": reference.sha,
                "path": reference.path,
            },
            domain="result",
        )
    return None


class DeliveryDependency(_StrictModel):
    ticket_id: UUIDValue
    contract_revision: StrictInt = Field(gt=0)
    attempt: StrictInt = Field(gt=0)
    milestone: Literal["integrated", "accepted"]

    def identity(self) -> tuple[UUID, int, int, str]:
        return (self.ticket_id, self.contract_revision, self.attempt, self.milestone)


class ContractInput(_StrictModel):
    """Caller-owned, validated contract content before repository normalization."""

    schema_version: StrictInt = 1
    objective: str = Field(min_length=1, max_length=8000)
    constraints: Annotated[tuple[str, ...], BeforeValidator(_lists_to_tuples)] = Field(
        default_factory=tuple, max_length=200
    )
    acceptance_criteria: Annotated[tuple[str, ...], BeforeValidator(_lists_to_tuples)] = Field(
        default_factory=tuple, max_length=200
    )
    priority: StrictInt = Field(ge=0, le=10000)
    context_refs: Annotated[tuple[ContextReference, ...], BeforeValidator(_lists_to_tuples)] = (
        Field(default_factory=tuple, max_length=32)
    )
    dependencies: Annotated[tuple[DeliveryDependency, ...], BeforeValidator(_lists_to_tuples)] = (
        Field(default_factory=tuple, max_length=32)
    )
    deliverables: Annotated[tuple[Deliverable, ...], BeforeValidator(_lists_to_tuples)] = Field(
        min_length=1, max_length=20
    )
    acceptance_mode: Literal["automatic", "explicit"]

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version_is_exact_integer(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @field_validator("objective", "constraints", "acceptance_criteria")
    @classmethod
    def _strings_have_no_surrogates(cls, value: str | tuple[str, ...]) -> str | tuple[str, ...]:
        if isinstance(value, str):
            return _reject_surrogates(value)
        return tuple(_reject_surrogates(item) for item in value)

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
    content_digest: str | None = Field(default=None, min_length=64, max_length=64)
    author_project: str = Field(min_length=1, max_length=50)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    amendment_reason: str | None = Field(default=None, min_length=1, max_length=4000)
    context_refs: Annotated[
        tuple[PinnedContextReference, ...], BeforeValidator(_lists_to_tuples)
    ] = Field(default_factory=tuple, max_length=32)

    @field_validator("content_digest")
    @classmethod
    def _valid_content_digest(cls, value: str | None) -> str | None:
        return _validate_digest(value) if value is not None else None

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
        from brain_v42.models.delivery_hashes import contract_digest

        expected_digest = contract_digest(self)
        if self.content_digest is None:
            object.__setattr__(self, "content_digest", expected_digest)
        elif self.content_digest != expected_digest:
            raise ValueError("content_digest does not match canonical contract content")
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
    state: Literal["proposed", "observed"] = "proposed"
    head_sha: str | None = Field(default=None, min_length=40, max_length=64)
    base_sha: str | None = Field(default=None, min_length=40, max_length=64)
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

    @model_validator(mode="after")
    def _validated_revision_state(self) -> ArtifactBinding:
        if self.state == "proposed":
            if (
                self.head_sha is not None
                or self.base_sha is not None
                or self.integration_sha is not None
            ):
                raise ValueError("proposed bindings cannot contain observed revision identities")
        elif self.head_sha is None or self.base_sha is None:
            raise ValueError("observed bindings require head_sha and base_sha")
        return self

    @property
    def is_observed(self) -> bool:
        """Whether a provider observation has established a head/base revision pair."""
        return self.state == "observed"


class CheckAttempt(_StrictModel):
    """One provider check result associated with an evaluated pull-request head."""

    record_id: StrictInt = Field(gt=0)
    provider_id: StrictInt = Field(gt=0)
    kind: Literal["check_run", "commit_status"]
    name: str = Field(min_length=1, max_length=200)
    app_slug: str | None = Field(default=None, min_length=1, max_length=200)
    head_sha: str = Field(min_length=40, max_length=64)
    conclusion: Literal["success", "failure", "pending", "skipped", "neutral", "cancelled"]
    started_at: datetime | None = None
    completed_at: datetime | None = None

    _valid_head = field_validator("head_sha")(_validate_sha)


class ReviewEvidence(_StrictModel):
    """One immutable provider review decision on a pull-request revision."""

    record_id: StrictInt = Field(gt=0)
    provider_id: StrictInt = Field(gt=0)
    reviewer: str = Field(min_length=1, max_length=200)
    head_sha: str = Field(min_length=40, max_length=64)
    decision: Literal["approved", "changes_requested", "dismissed", "commented"]
    submitted_at: datetime

    _valid_head = field_validator("head_sha")(_validate_sha)


class PullRequestEvidence(_StrictModel):
    """Provider facts collected for an observed pull request; never caller-authored success."""

    provider_id: StrictInt = Field(gt=0)
    repository_id: StrictInt = Field(gt=0)
    pr_number: StrictInt = Field(gt=0)
    author_id: str = Field(min_length=1, max_length=200)
    head_repository_id: StrictInt = Field(gt=0)
    head_sha: str = Field(min_length=40, max_length=64)
    base_sha: str = Field(min_length=40, max_length=64)
    base_ref: str = Field(min_length=1, max_length=255)
    integration_sha: str | None = Field(default=None, min_length=40, max_length=64)
    state: Literal["open", "merged", "closed"]
    draft: StrictBool
    mergeable: StrictBool | None = None
    complete: StrictBool
    checks: Annotated[tuple[CheckAttempt, ...], BeforeValidator(_lists_to_tuples)] = Field(
        default_factory=tuple, max_length=2000
    )
    reviews: Annotated[tuple[ReviewEvidence, ...], BeforeValidator(_lists_to_tuples)] = Field(
        default_factory=tuple, max_length=2000
    )
    synthetic_merges: Annotated[
        tuple[SyntheticMergeAssociation, ...], BeforeValidator(_lists_to_tuples)
    ] = Field(default_factory=tuple, max_length=2000)
    integration_revision: str | None = Field(default=None, min_length=40, max_length=64)
    collected_at: datetime

    _valid_head = field_validator(
        "head_sha", "base_sha", "integration_sha", "integration_revision"
    )(lambda value: _validate_sha(value) if value is not None else None)
    _valid_base_ref = field_validator("base_ref")(_reject_surrogates)


class SyntheticMergeAssociation(_StrictModel):
    """Proven parents for a provider-created synthetic merge revision."""

    synthetic_sha: str = Field(min_length=40, max_length=64)
    head_sha: str = Field(min_length=40, max_length=64)
    base_sha: str = Field(min_length=40, max_length=64)

    _valid_shas = field_validator("synthetic_sha", "head_sha", "base_sha")(_validate_sha)


class ObservationConfirmation(_StoredModel):
    """Append-only successful or failed collection confirmation for one binding."""

    id: UUIDValue = Field(default_factory=uuid4)
    evidence: PullRequestEvidence | None = None
    collection_started_at: datetime
    collection_finished_at: datetime
    outcome: Literal["success", "error"] = "success"
    error_code: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _has_evidence_only_for_success(self) -> ObservationConfirmation:
        if self.outcome == "success" and self.evidence is None:
            raise ValueError("successful confirmation requires evidence")
        if self.outcome == "error" and self.error_code is None:
            raise ValueError("error confirmation requires error_code")
        if self.collection_finished_at < self.collection_started_at:
            raise ValueError("collection_finished_at precedes collection_started_at")
        return self


class RepositoryContextObservationConfirmation(_StoredModel):
    """Append-only successful or failed collection confirmation for repository context."""

    id: UUIDValue = Field(default_factory=uuid4)
    snapshot_id: UUIDValue | None = None
    evidence: RepositoryContextEvidence | None = None
    collection_started_at: datetime
    collection_finished_at: datetime
    outcome: Literal["success", "error"] = "success"
    error_code: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _has_evidence_only_for_success(self) -> RepositoryContextObservationConfirmation:
        if self.collection_started_at.tzinfo is None or self.collection_finished_at.tzinfo is None:
            raise ValueError("repository context confirmation interval must be timezone-aware")
        if self.outcome == "success":
            if (
                self.evidence is None
                or self.snapshot_id is None
                or self.error_code is not None
                or not self.evidence.complete
            ):
                raise ValueError(
                    "successful repository context confirmation requires immutable evidence"
                )
        elif self.evidence is not None or self.snapshot_id is not None or self.error_code is None:
            raise ValueError(
                "repository context error confirmation requires only a safe error code"
            )
        if self.collection_finished_at < self.collection_started_at:
            raise ValueError("collection_finished_at precedes collection_started_at")
        return self


class BindingEvidence(_StrictModel):
    """Active binding plus its latest retained provider confirmation and health facts."""

    binding: ArtifactBinding
    confirmation: ObservationConfirmation | None = None
    snapshot_id: UUIDValue | None = None
    success_confirmation_id: UUIDValue | None = None
    latest_attempt_confirmation_id: UUIDValue | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_attempt_outcome: Literal["success", "error", "never"] = "never"

    @model_validator(mode="after")
    def _error_attempt_is_identified(self) -> BindingEvidence:
        if self.last_attempt_outcome == "error" and self.latest_attempt_confirmation_id is None:
            raise ValueError("error binding evidence requires latest_attempt_confirmation_id")
        return self


class ContextPredicate(_StrictModel):
    """Current comparison of one pinned context fact with its contract digest."""

    reference_identity: str = Field(min_length=1, max_length=5000)
    current_digest: str | None = Field(default=None, min_length=64, max_length=64)
    status: Literal["available", "changed", "missing", "error"]
    snapshot_id: UUIDValue | None = None
    success_confirmation_id: UUIDValue | None = None
    latest_attempt_confirmation_id: UUIDValue | None = None
    collection_started_at: datetime | None = None
    collection_finished_at: datetime | None = None
    evidence: RepositoryContextEvidence | None = None

    _valid_current = field_validator("current_digest")(
        lambda value: _validate_digest(value) if value is not None else None
    )

    @model_validator(mode="after")
    def _repository_proof_is_explicit(self) -> ContextPredicate:
        if not self.reference_identity.startswith("repository_document:"):
            return self
        if self.status == "error" and self.latest_attempt_confirmation_id is None:
            raise ValueError("repository context error requires latest_attempt_confirmation_id")
        if self.status != "available":
            return self
        if (
            self.current_digest is None
            or self.snapshot_id is None
            or self.success_confirmation_id is None
            or self.latest_attempt_confirmation_id is None
            or self.collection_started_at is None
            or self.collection_finished_at is None
            or self.evidence is None
            or not self.evidence.complete
        ):
            raise ValueError("available repository context requires immutable confirmation proof")
        if self.collection_started_at.tzinfo is None or self.collection_finished_at.tzinfo is None:
            raise ValueError("repository context confirmation interval must be timezone-aware")
        if self.collection_finished_at < self.collection_started_at:
            raise ValueError("repository context confirmation interval is reversed")
        return self


class MilestoneReceipt(_StoredModel):
    """Immutable historical integration or fulfillment evidence for one delivery identity."""

    id: UUIDValue = Field(default_factory=uuid4)
    ticket_id: UUIDValue
    milestone: Literal["integration", "fulfilled"]
    contract_revision: StrictInt = Field(gt=0)
    attempt: StrictInt = Field(gt=0)
    contract_digest: str = Field(min_length=64, max_length=64)
    delivery_digest: str = Field(min_length=64, max_length=64)
    issued_at: datetime
    acceptance_basis: Literal["automatic", "explicit"] | None = None

    _valid_digests = field_validator("contract_digest", "delivery_digest")(_validate_digest)

    @model_validator(mode="after")
    def _valid_basis(self) -> MilestoneReceipt:
        if self.milestone == "integration" and self.acceptance_basis is not None:
            raise ValueError("integration receipts cannot have an acceptance basis")
        if self.milestone == "fulfilled" and self.acceptance_basis is None:
            raise ValueError("fulfillment receipts require an acceptance basis")
        return self


class DependencyPredicate(_StrictModel):
    """Current upstream generation and immutable receipt available to this contract."""

    ticket_id: UUIDValue
    contract_revision: StrictInt = Field(gt=0)
    attempt: StrictInt = Field(gt=0)
    milestone: Literal["integrated", "accepted"]
    current_contract_revision: StrictInt = Field(gt=0)
    current_attempt: StrictInt = Field(gt=0)
    current_contract_digest: str = Field(min_length=64, max_length=64)
    current_delivery_digest: str = Field(min_length=64, max_length=64)
    current_disposition: Literal["active", "fulfilled", "cancelled", "wontfix"]
    receipt: MilestoneReceipt | None = None

    _valid_current_digests = field_validator("current_contract_digest", "current_delivery_digest")(
        _validate_digest
    )


class ClaimState(_StrictModel):
    """Non-secret lease facts that can influence claim availability and assessment identity."""

    epoch: StrictInt = Field(ge=0)
    owner: str | None = Field(default=None, min_length=1, max_length=200)
    expires_at: datetime | None = None


class EvaluationInput(_StrictModel):
    """Read-only facts consumed by the deterministic delivery evaluator."""

    contract: ContractRevision
    attempt: StrictInt = Field(gt=0)
    workflow_version: StrictInt = Field(gt=0)
    coordination_status: str = Field(min_length=1, max_length=50)
    coordination_disposition: Literal["active", "fulfilled", "cancelled", "wontfix"]
    is_self_ticket: StrictBool
    active_bindings: Annotated[tuple[BindingEvidence, ...], BeforeValidator(_lists_to_tuples)] = (
        Field(default_factory=tuple, max_length=20)
    )
    contexts: Annotated[tuple[ContextPredicate, ...], BeforeValidator(_lists_to_tuples)] = Field(
        default_factory=tuple, max_length=32
    )
    dependencies: Annotated[tuple[DependencyPredicate, ...], BeforeValidator(_lists_to_tuples)] = (
        Field(default_factory=tuple, max_length=32)
    )
    integration_receipt: MilestoneReceipt | None = None
    fulfillment_receipt: MilestoneReceipt | None = None
    feature_enabled: StrictBool
    freshness_seconds: StrictInt = Field(ge=1, le=86400)
    claim: ClaimState | None = None
    executor_identity: str = Field(default="executor-project", min_length=1, max_length=200)
    requested_completion_action: (
        Literal[
            "cross_resolve", "cross_confirm", "self_resolve_pending", "self_resolve", "self_confirm"
        ]
        | None
    ) = None


class DeliveryFinding(_StrictModel):
    """A deterministic, stable explanation of an observed delivery predicate."""

    code: str = Field(min_length=1, max_length=100)
    deliverable_key: str | None = Field(default=None, min_length=1, max_length=64)
    detail: str = Field(min_length=1, max_length=1000)


class EligibleWork(_StrictModel):
    """A claimable external work category and its required participating role."""

    kind: Literal["implement", "repair", "review", "integrate", "accept"]
    role: Literal["executor", "requester"]


class DeliveryAssessment(_StrictModel):
    """Pure, replayable assessment returned by the delivery evaluator."""

    assessment_id: str = Field(min_length=64, max_length=64)
    assessment_version: StrictInt = Field(gt=0)
    assessed_at: datetime
    observed_at: datetime | None = None
    fresh_until: datetime | None = None
    coordination_status: str
    delivery_stage: Literal["awaiting_artifact", "proposed", "verified", "integrated"]
    observation_health: Literal["never_observed", "fresh", "stale", "error", "disabled"]
    acceptance_state: Literal["not_required", "pending", "accepted", "superseded"]
    requirements_satisfied: StrictBool
    integration_receipt_eligible: StrictBool
    completion_eligible_now: StrictBool
    contract_fulfilled: StrictBool
    delivery_digest: str = Field(min_length=64, max_length=64)
    blockers: Annotated[tuple[DeliveryFinding, ...], BeforeValidator(_lists_to_tuples)]
    deliverables: Annotated[tuple[DeliveryFinding, ...], BeforeValidator(_lists_to_tuples)]
    eligible_work: Annotated[tuple[EligibleWork, ...], BeforeValidator(_lists_to_tuples)]

    _valid_ids = field_validator("assessment_id", "delivery_digest")(_validate_digest)


class DeliveryView(_StrictModel):
    """Concrete API read shape for one workflow, its evidence assessment and receipts."""

    contract: ContractRevision
    assessment: DeliveryAssessment
    bindings: Annotated[tuple[BindingEvidence, ...], BeforeValidator(_lists_to_tuples)] = Field(
        default_factory=tuple, max_length=20
    )
    contexts: Annotated[tuple[ContextPredicate, ...], BeforeValidator(_lists_to_tuples)] = Field(
        default_factory=tuple, max_length=32
    )
    integration_receipt: MilestoneReceipt | None = None
    fulfillment_receipt: MilestoneReceipt | None = None


class DeliveryPage(_StrictModel):
    """Concrete stable page of delivery views."""

    items: Annotated[tuple[DeliveryView, ...], BeforeValidator(_lists_to_tuples)] = Field(
        default_factory=tuple, max_length=100
    )
    next_cursor: str | None = Field(default=None, min_length=1, max_length=1000)
    omitted_count: StrictInt = Field(default=0, ge=0)


class ClaimResult(_StrictModel):
    """Result returned by a future atomic claim operation; token remains secret at the boundary."""

    ticket_id: UUIDValue
    assessment_id: str = Field(min_length=64, max_length=64)
    work: EligibleWork
    epoch: StrictInt = Field(ge=0)
    expires_at: datetime
    claim_token: str = Field(min_length=1, max_length=1000)

    _valid_assessment = field_validator("assessment_id")(_validate_digest)


def contract_content_payload(contract: ContractRevision) -> Mapping[str, Any]:
    """Project a normalized stored contract onto its canonical content identity."""
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
    normalized_deliverables: list[dict[str, Any]] = []
    for deliverable in payload["deliverables"]:
        repository_id = deliverable.get("repository_id")
        if type(repository_id) is not int or repository_id <= 0:
            raise ValueError("contract digest requires normalized repository_id values")
        normalized_deliverable = dict(deliverable)
        normalized_deliverable.pop("repository", None)
        normalized_deliverables.append(normalized_deliverable)
    payload["deliverables"] = normalized_deliverables
    return payload
