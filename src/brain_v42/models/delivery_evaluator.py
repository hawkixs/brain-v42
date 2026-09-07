"""Pure predicates for observable delivery evidence and receipt eligibility."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from brain_v42.models.delivery import (
    BindingEvidence,
    CheckAttempt,
    DeliveryAssessment,
    DeliveryFinding,
    EligibleWork,
    EvaluationInput,
    MilestoneReceipt,
    PullRequestEvidence,
    RequiredCheck,
    ReviewEvidence,
)
from brain_v42.models.delivery_hashes import canonical_digest

_TECHNICAL_CHECK_CODES = frozenset(
    {
        "binding_missing",
        "binding_unobserved",
        "observation_incomplete",
        "head_mismatch",
        "base_mismatch",
        "check_missing",
        "check_pending",
        "check_failed",
        "check_skipped",
        "check_neutral",
        "check_cancelled",
        "review_changes_requested",
        "review_approval_missing",
        "pr_draft",
        "pr_not_merged",
    }
)


def _timestamp(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _finding(code: str, detail: str, key: str | None = None) -> DeliveryFinding:
    return DeliveryFinding(code=code, detail=detail, deliverable_key=key)


def _current_health(
    inputs: EvaluationInput, now: datetime
) -> Literal["never_observed", "fresh", "stale", "error", "disabled"]:
    if not inputs.feature_enabled:
        return "disabled"
    confirmations = [item.confirmation for item in inputs.active_bindings]
    if not confirmations or any(item is None for item in confirmations):
        return "never_observed"
    if any(item.last_attempt_outcome == "error" for item in inputs.active_bindings):
        return "error"
    successes = [
        item.evidence for item in confirmations if item is not None and item.evidence is not None
    ]
    if not successes:
        return "never_observed"
    if any(
        (now - evidence.collected_at).total_seconds() > inputs.freshness_seconds
        for evidence in successes
    ):
        return "stale"
    return "fresh"


def _matching_binding(bindings: tuple[BindingEvidence, ...], key: str) -> BindingEvidence | None:
    candidates = [item for item in bindings if item.binding.deliverable_key == key]
    return candidates[0] if len(candidates) == 1 else None


def _select_check(evidence: PullRequestEvidence, required: RequiredCheck) -> CheckAttempt | None:
    checks = [
        check
        for check in evidence.checks
        if check.head_sha == evidence.head_sha
        and check.kind == required.kind
        and check.name == required.name
        and (required.app_slug is None or check.app_slug == required.app_slug)
        and (required.provider_id is None or check.provider_id == required.provider_id)
    ]
    if not checks:
        return None
    return max(
        checks,
        key=lambda check: (
            check.run_attempt,
            _timestamp(check.completed_at) or "",
            check.provider_id,
        ),
    )


def _review_findings(
    inputs: EvaluationInput, evidence: PullRequestEvidence, key: str
) -> list[DeliveryFinding]:
    policy = next(item.review for item in inputs.contract.deliverables if item.key == key)
    if policy.required_approvals == 0:
        return []
    reviews = [
        review
        for review in evidence.reviews
        if review.head_sha == evidence.head_sha
        and review.reviewer != inputs.executor_identity
        and (not policy.allowed_reviewers or review.reviewer in policy.allowed_reviewers)
    ]
    latest: dict[str, ReviewEvidence] = {}
    for review in sorted(
        reviews, key=lambda item: (_timestamp(item.submitted_at) or "", item.provider_id)
    ):
        latest[review.reviewer] = review
    effective: tuple[ReviewEvidence, ...] = tuple(latest.values())
    findings: list[DeliveryFinding] = []
    if any(review.decision == "changes_requested" for review in effective):
        findings.append(
            _finding("review_changes_requested", "an allowed reviewer requested changes", key)
        )
    approvals = sum(1 for review in effective if review.decision == "approved")
    if approvals < policy.required_approvals:
        findings.append(
            _finding("review_approval_missing", "required effective approvals are missing", key)
        )
    return findings


def _deliverable_findings(inputs: EvaluationInput) -> tuple[DeliveryFinding, ...]:
    findings: list[DeliveryFinding] = []
    for deliverable in sorted(inputs.contract.deliverables, key=lambda item: item.key):
        current = _matching_binding(inputs.active_bindings, deliverable.key)
        if current is None:
            findings.append(
                _finding("binding_missing", "no active pull-request binding", deliverable.key)
            )
            continue
        if current.binding.state != "observed" or current.confirmation is None:
            findings.append(
                _finding(
                    "binding_unobserved",
                    "binding lacks a successful provider observation",
                    deliverable.key,
                )
            )
            continue
        if current.confirmation.outcome != "success" or current.confirmation.evidence is None:
            findings.append(
                _finding("observation_error", "latest provider collection failed", deliverable.key)
            )
            continue
        evidence = current.confirmation.evidence
        if not evidence.complete:
            findings.append(
                _finding(
                    "observation_incomplete", "provider observation is incomplete", deliverable.key
                )
            )
        if (
            evidence.repository_id != current.binding.repository_id
            or evidence.pr_number != current.binding.pr_number
        ):
            findings.append(
                _finding(
                    "binding_identity_mismatch",
                    "observed provider identity differs from binding",
                    deliverable.key,
                )
            )
        if evidence.head_sha != current.binding.head_sha:
            findings.append(
                _finding("head_mismatch", "observed head differs from bound head", deliverable.key)
            )
        if evidence.base_sha != current.binding.base_sha:
            findings.append(
                _finding("base_mismatch", "observed base differs from bound base", deliverable.key)
            )
        if evidence.draft:
            findings.append(_finding("pr_draft", "pull request remains a draft", deliverable.key))
        for required in deliverable.required_checks:
            check = _select_check(evidence, required)
            if check is None:
                findings.append(
                    _finding(
                        "check_missing",
                        f"required check {required.name} is absent",
                        deliverable.key,
                    )
                )
            elif check.conclusion != "success":
                code = (
                    "check_failed" if check.conclusion == "failure" else f"check_{check.conclusion}"
                )
                findings.append(
                    _finding(
                        code,
                        f"required check {required.name} is {check.conclusion}",
                        deliverable.key,
                    )
                )
        findings.extend(_review_findings(inputs, evidence, deliverable.key))
        if evidence.state != "merged":
            findings.append(
                _finding("pr_not_merged", "pull request is not merged", deliverable.key)
            )
            if evidence.mergeable is None:
                findings.append(
                    _finding(
                        "mergeability_unknown",
                        "provider has not determined mergeability",
                        deliverable.key,
                    )
                )
            elif not evidence.mergeable:
                findings.append(
                    _finding(
                        "merge_conflict",
                        "provider reports the pull request cannot merge",
                        deliverable.key,
                    )
                )
        elif evidence.integration_sha is None and evidence.integration_revision is None:
            findings.append(
                _finding(
                    "integration_identity_missing",
                    "merged evidence lacks integration revision",
                    deliverable.key,
                )
            )
    return tuple(
        sorted(findings, key=lambda item: (item.deliverable_key or "", item.code, item.detail))
    )


def _context_findings(inputs: EvaluationInput) -> tuple[DeliveryFinding, ...]:
    findings: list[DeliveryFinding] = []
    for context in sorted(inputs.contexts, key=lambda item: item.key):
        if not context.required:
            continue
        if context.status == "missing":
            findings.append(
                _finding("context_missing", f"required context {context.key} is missing")
            )
        elif context.status == "error":
            findings.append(
                _finding("context_error", f"required context {context.key} could not be read")
            )
        elif context.status != "available" or context.current_digest != context.expected_digest:
            findings.append(
                _finding("context_changed", f"required context {context.key} differs from its pin")
            )
    return tuple(findings)


def _dependency_findings(inputs: EvaluationInput) -> tuple[DeliveryFinding, ...]:
    findings: list[DeliveryFinding] = []
    for dependency in sorted(inputs.dependencies, key=lambda item: str(item.ticket_id)):
        if dependency.current_disposition in {"cancelled", "wontfix"}:
            findings.append(
                _finding(
                    "dependency_unsuccessful", "upstream dependency has an unsuccessful disposition"
                )
            )
        elif (
            dependency.contract_revision != dependency.current_contract_revision
            or dependency.attempt != dependency.current_attempt
        ):
            findings.append(
                _finding("dependency_generation_mismatch", "upstream dependency generation changed")
            )
        elif dependency.receipt is None:
            findings.append(
                _finding(
                    "dependency_receipt_missing", "upstream dependency has no matching receipt"
                )
            )
        else:
            expected_milestone = (
                "integration" if dependency.milestone == "integrated" else "fulfilled"
            )
            receipt = dependency.receipt
            if (
                receipt.milestone != expected_milestone
                or receipt.ticket_id != dependency.ticket_id
                or receipt.contract_revision != dependency.contract_revision
                or receipt.attempt != dependency.attempt
                or receipt.contract_digest != dependency.current_contract_digest
                or receipt.delivery_digest != dependency.current_delivery_digest
            ):
                findings.append(
                    _finding(
                        "dependency_receipt_mismatch", "upstream receipt does not match its pin"
                    )
                )
    return tuple(findings)


def _stage(
    inputs: EvaluationInput, findings: tuple[DeliveryFinding, ...]
) -> Literal["awaiting_artifact", "proposed", "verified", "integrated"]:
    if any(item.code == "binding_missing" for item in findings):
        return "awaiting_artifact"
    evidence = [
        item.confirmation.evidence
        for item in inputs.active_bindings
        if item.confirmation is not None and item.confirmation.evidence is not None
    ]
    if (
        evidence
        and all(item.state == "merged" for item in evidence)
        and len(evidence) == len(inputs.contract.deliverables)
    ):
        return "integrated"
    if any(item.code == "pr_draft" for item in findings):
        return "proposed"
    technical_codes = _TECHNICAL_CHECK_CODES - {"pr_not_merged", "pr_draft"}
    if evidence and not any(item.code in technical_codes for item in findings):
        return "verified"
    return "proposed"


def _delivery_digest(inputs: EvaluationInput) -> str:
    bindings = []
    for item in sorted(inputs.active_bindings, key=lambda value: value.binding.deliverable_key):
        evidence = item.confirmation.evidence if item.confirmation is not None else None
        bindings.append(
            {
                "key": item.binding.deliverable_key,
                "binding_id": str(item.binding.id),
                "binding_version": item.binding.binding_version,
                "repository_id": item.binding.repository_id,
                "pr_number": item.binding.pr_number,
                "head_sha": evidence.head_sha if evidence is not None else item.binding.head_sha,
                "base_sha": evidence.base_sha if evidence is not None else item.binding.base_sha,
                "integration_revision": evidence.integration_revision
                if evidence is not None
                else None,
            }
        )
    return canonical_digest(
        {
            "contract_digest": inputs.contract.content_digest,
            "attempt": inputs.attempt,
            "bindings": bindings,
        },
        domain="result",
    )


def _matches(
    receipt: MilestoneReceipt | None, milestone: str, inputs: EvaluationInput, digest: str
) -> bool:
    return bool(
        receipt
        and receipt.milestone == milestone
        and receipt.ticket_id == inputs.contract.ticket_id
        and receipt.contract_revision == inputs.contract.contract_revision
        and receipt.attempt == inputs.attempt
        and receipt.contract_digest == inputs.contract.content_digest
        and receipt.delivery_digest == digest
    )


def _eligible_work(
    inputs: EvaluationInput,
    findings: tuple[DeliveryFinding, ...],
    stage: str,
    requirements_satisfied: bool,
    has_integration: bool,
    has_fulfillment: bool,
) -> tuple[EligibleWork, ...]:
    if (
        inputs.coordination_disposition in {"cancelled", "wontfix", "fulfilled"}
        or not inputs.feature_enabled
    ):
        return ()
    codes = {item.code for item in findings}
    if "binding_missing" in codes:
        return (EligibleWork(kind="implement", role="executor"),)
    if codes & {"pr_draft", "binding_unobserved"}:
        return (EligibleWork(kind="implement", role="executor"),)
    if codes & {"review_changes_requested", "review_approval_missing"}:
        return (EligibleWork(kind="review", role="executor"),)
    if codes & (_TECHNICAL_CHECK_CODES - {"pr_not_merged"}):
        return (EligibleWork(kind="repair", role="executor"),)
    progress_only = {"pr_not_merged"}
    if stage != "integrated" and not ({item.code for item in findings} - progress_only):
        return (EligibleWork(kind="integrate", role="executor"),)
    if has_integration and inputs.contract.acceptance_mode == "explicit" and not has_fulfillment:
        return (EligibleWork(kind="accept", role="requester"),)
    return ()


def _assessment_id(
    inputs: EvaluationInput,
    health: str,
    findings: tuple[DeliveryFinding, ...],
    work: tuple[EligibleWork, ...],
    digest: str,
    now: datetime,
) -> str:
    bindings = []
    for item in sorted(inputs.active_bindings, key=lambda value: value.binding.deliverable_key):
        evidence = item.confirmation.evidence if item.confirmation is not None else None
        bindings.append(
            {
                "key": item.binding.deliverable_key,
                "id": str(item.binding.id),
                "version": item.binding.binding_version,
                "head": evidence.head_sha if evidence is not None else item.binding.head_sha,
                "base": evidence.base_sha if evidence is not None else item.binding.base_sha,
                "confirmation": str(item.confirmation.id)
                if item.confirmation is not None
                else None,
                "outcome": item.confirmation.outcome
                if item.confirmation is not None
                else item.last_attempt_outcome,
            }
        )
    return canonical_digest(
        {
            "contract_revision": inputs.contract.contract_revision,
            "contract_digest": inputs.contract.content_digest,
            "attempt": inputs.attempt,
            "workflow_version": inputs.workflow_version,
            "coordination_status": inputs.coordination_status,
            "disposition": inputs.coordination_disposition,
            "action": inputs.requested_completion_action,
            "bindings": bindings,
            "contexts": [
                {"key": item.key, "status": item.status, "current_digest": item.current_digest}
                for item in sorted(inputs.contexts, key=lambda value: value.key)
            ],
            "dependencies": [
                {
                    "ticket_id": str(item.ticket_id),
                    "revision": item.current_contract_revision,
                    "attempt": item.current_attempt,
                    "contract_digest": item.current_contract_digest,
                    "delivery_digest": item.current_delivery_digest,
                    "disposition": item.current_disposition,
                    "receipt_id": str(item.receipt.id) if item.receipt is not None else None,
                }
                for item in sorted(inputs.dependencies, key=lambda value: str(value.ticket_id))
            ],
            "integration_receipt": str(inputs.integration_receipt.id)
            if inputs.integration_receipt
            else None,
            "fulfillment_receipt": str(inputs.fulfillment_receipt.id)
            if inputs.fulfillment_receipt
            else None,
            "claim": {
                "epoch": inputs.claim.epoch if inputs.claim else None,
                "owner": inputs.claim.owner if inputs.claim else None,
                "active": bool(
                    inputs.claim and inputs.claim.expires_at and inputs.claim.expires_at > now
                ),
            },
            "health": health,
            "blockers": [item.code for item in findings],
            "work": [item.kind for item in work],
            "freshness_seconds": inputs.freshness_seconds,
            "feature_enabled": inputs.feature_enabled,
            "delivery_digest": digest,
        },
        domain="assessment",
    )


def _observation_times(inputs: EvaluationInput) -> tuple[datetime | None, datetime | None]:
    observed = [
        item.confirmation.evidence.collected_at
        for item in inputs.active_bindings
        if item.confirmation is not None and item.confirmation.evidence is not None
    ]
    if not observed:
        return (None, None)
    oldest = min(observed)
    return (oldest, oldest + timedelta(seconds=inputs.freshness_seconds))


def evaluate_delivery(inputs: EvaluationInput, *, now: datetime) -> DeliveryAssessment:
    """Evaluate immutable delivery facts at ``now`` without I/O or ticket mutation."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    health = _current_health(inputs, now)
    deliverable_findings = _deliverable_findings(inputs)
    blockers = (
        list(deliverable_findings)
        + list(_context_findings(inputs))
        + list(_dependency_findings(inputs))
    )
    if not inputs.feature_enabled:
        blockers.append(_finding("delivery_disabled", "delivery workflow feature is disabled"))
    elif health == "stale":
        blockers.append(
            _finding("observation_stale", "provider observation exceeded freshness limit")
        )
    elif health == "error":
        blockers.append(_finding("observation_error", "latest provider observation failed"))
    elif health == "never_observed" and inputs.active_bindings:
        blockers.append(
            _finding("observation_missing", "active binding has no successful observation")
        )
    if inputs.coordination_disposition in {"cancelled", "wontfix", "fulfilled"}:
        blockers.append(
            _finding(
                "delivery_disposition_terminal",
                "coordination disposition permits no further delivery work",
            )
        )
    blockers_tuple = tuple(
        sorted(blockers, key=lambda item: (item.deliverable_key or "", item.code, item.detail))
    )
    stage = _stage(inputs, deliverable_findings)
    requirements_satisfied = (
        not blockers_tuple and health == "fresh" and inputs.coordination_disposition == "active"
    )
    digest = _delivery_digest(inputs)
    has_integration = _matches(inputs.integration_receipt, "integration", inputs, digest)
    has_fulfillment = _matches(inputs.fulfillment_receipt, "fulfilled", inputs, digest)
    integration_receipt_eligible = stage == "integrated" and requirements_satisfied
    if has_fulfillment:
        acceptance_state: Literal["not_required", "pending", "accepted", "superseded"] = "accepted"
    elif inputs.fulfillment_receipt is not None or inputs.integration_receipt is not None:
        acceptance_state = "superseded"
    elif inputs.contract.acceptance_mode == "automatic":
        acceptance_state = "not_required"
    else:
        acceptance_state = "pending"
    if inputs.requested_completion_action in {"cross_resolve", "self_resolve_pending"}:
        completion_eligible = requirements_satisfied and has_integration
    else:
        completion_eligible = requirements_satisfied and has_fulfillment
    work = _eligible_work(
        inputs, blockers_tuple, stage, requirements_satisfied, has_integration, has_fulfillment
    )
    assessment_id = _assessment_id(inputs, health, blockers_tuple, work, digest, now)
    observed_at, fresh_until = _observation_times(inputs)
    return DeliveryAssessment(
        assessment_id=assessment_id,
        assessment_version=inputs.workflow_version,
        assessed_at=now,
        observed_at=observed_at,
        fresh_until=fresh_until,
        coordination_status=inputs.coordination_status,
        delivery_stage=stage,
        observation_health=health,
        acceptance_state=acceptance_state,
        requirements_satisfied=requirements_satisfied,
        integration_receipt_eligible=integration_receipt_eligible,
        completion_eligible_now=completion_eligible,
        contract_fulfilled=has_fulfillment,
        delivery_digest=digest,
        blockers=blockers_tuple,
        deliverables=deliverable_findings,
        eligible_work=work,
    )
