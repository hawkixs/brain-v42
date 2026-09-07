"""Bounded GitHub facts for an exact, stable pull-request revision pair."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from pydantic import ValidationError

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.transport import GitHubPage, GitHubTransport, ProviderError
from brain_v42.models.delivery import (
    ArtifactBinding,
    CheckAttempt,
    ContractRevision,
    Deliverable,
    PullRequestEvidence,
    ReviewEvidence,
    SyntheticMergeAssociation,
)

_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")


class GitHubAuthorization(Protocol):
    transport: GitHubTransport

    async def authorization_headers(self) -> dict[str, str]: ...


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderError("provider_invalid_response")
    return value


def _positive(value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise ProviderError("provider_invalid_response")
    return value


def _sha(value: Any) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise ProviderError("provider_invalid_response")
    return value


def _text(value: Any, limit: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= limit
        or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise ProviderError("provider_invalid_response")
    return value


def _time(value: Any, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    try:
        result = datetime.fromisoformat(_text(value, 64).replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result.astimezone(UTC)
    except ValueError:
        raise ProviderError("provider_invalid_response") from None


@dataclass(slots=True)
class _PageBudget:
    pages: int = 0
    records: int = 0

    def take_page(self) -> None:
        if self.pages >= 20:
            raise ProviderError("provider_invalid_response")
        self.pages += 1

    def take_records(self, count: int) -> None:
        self.records += count
        if count > 100 or self.records > 2000:
            raise ProviderError("provider_invalid_response")


class GitHubClient:
    """Only GETs; the injected machine auth and every job share one transport."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        settings: DeliverySettings,
        auth: GitHubAuthorization,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if auth.transport.http is not http:
            raise ValueError("GitHub client must share its authentication transport")
        self.settings = DeliverySettings.model_validate(settings.model_dump())
        self.transport = auth.transport
        self.auth = auth
        self.now = now

    async def _get(self, path: str) -> GitHubPage:
        # Installation-token refresh happens before HTTP admission, avoiding a
        # recursive acquisition while an outer request holds a concurrency slot.
        headers = await self.auth.authorization_headers()
        return await self.transport.request_page("GET", path, headers=headers)

    def _deliverable(self, binding: ArtifactBinding, contract: ContractRevision) -> Deliverable:
        candidates = [item for item in contract.deliverables if item.key == binding.deliverable_key]
        registered = {
            repository_id
            for repositories in self.settings.repository_registry.values()
            for repository_id in repositories
        }
        if (
            len(candidates) != 1
            or binding.ticket_id != contract.ticket_id
            or binding.contract_revision != contract.contract_revision
            or binding.repository_id not in registered
            or candidates[0].repository_id != binding.repository_id
        ):
            raise ProviderError("provider_invalid_response")
        return candidates[0]

    def _address(self, repository_id: int) -> str:
        names = {
            name
            for repositories in self.settings.repository_registry.values()
            for identity, name in repositories.items()
            if identity == repository_id
        }
        if len(names) != 1 or not _REPOSITORY.fullmatch(next(iter(names))):
            raise ProviderError("provider_invalid_response")
        return "/repos/" + next(iter(names))

    def _pr(self, raw: Any) -> tuple[dict[str, Any], str, str | None]:
        pr = _object(raw)
        head, base, author = (_object(pr[key]) for key in ("head", "base", "user"))
        head_repo, base_repo = _object(head["repo"]), _object(base["repo"])
        merged, draft, state = pr["merged"], pr["draft"], pr["state"]
        mergeable = pr.get("mergeable")
        if (
            type(merged) is not bool
            or type(draft) is not bool
            or state not in {"open", "closed"}
            or (merged and state != "closed")
            or (mergeable is not None and type(mergeable) is not bool)
        ):
            raise ProviderError("provider_invalid_response")
        name = _text(base_repo["full_name"], 201)
        if not _REPOSITORY.fullmatch(name):
            raise ProviderError("provider_invalid_response")
        merge_sha = pr.get("merge_commit_sha")
        if merge_sha is not None:
            merge_sha = _sha(merge_sha)
        if merged and merge_sha is None:
            raise ProviderError("provider_revision_unverifiable")
        facts = {
            "provider_id": _positive(pr["id"]),
            "repository_id": _positive(base_repo["id"]),
            "pr_number": _positive(pr["number"]),
            "author_id": _text(author["login"]),
            "head_repository_id": _positive(head_repo["id"]),
            "head_sha": _sha(head["sha"]),
            "base_sha": _sha(base["sha"]),
            "base_ref": _text(base["ref"], 255),
            "state": "merged" if merged else state,
            "draft": draft,
            "integration_sha": merge_sha if merged else None,
            "mergeable": mergeable,
        }
        # Author's numeric identity also participates in the before/after fence.
        _positive(author["id"])
        return facts, name, merge_sha if state == "open" else None

    def _record_url(self, value: Any, path: str) -> str:
        value = _text(value, 2048)
        actual = self.transport.validate_url(value)
        expected = self.transport.validate_url(path)
        if actual != expected:
            raise ProviderError("provider_invalid_response")
        return actual

    async def _records(
        self,
        path: str,
        budget: _PageBudget,
        *,
        check_runs: bool = False,
    ) -> list[dict[str, Any]]:
        query = {"per_page": "100", **({"filter": "all"} if check_runs else {})}
        page_number = 1
        records: list[dict[str, Any]] = []
        seen: set[int] = set()
        total: int | None = None
        while True:
            budget.take_page()
            query["page"] = str(page_number)
            page = await self._get(path + "?" + urlencode(query))
            payload = page.data
            if check_runs:
                envelope = _object(payload)
                count = envelope["total_count"]
                if type(count) is not int or count < 0 or count > 2000:
                    raise ProviderError("provider_invalid_response")
                if total is not None and total != count:
                    raise ProviderError("provider_invalid_response")
                total, payload = count, envelope["check_runs"]
            if not isinstance(payload, list):
                raise ProviderError("provider_invalid_response")
            budget.take_records(len(payload))
            for value in payload:
                record = _object(value)
                identity = _positive(record["id"])
                if identity in seen:
                    raise ProviderError("provider_invalid_response")
                seen.add(identity)
                records.append(record)
            if page.next_url is None:
                if total is not None and len(records) != total:
                    raise ProviderError("provider_invalid_response")
                return records
            page_number += 1
            expected_query = {**query, "page": str(page_number)}
            next_parts = urlsplit(self.transport.validate_url(page.next_url))
            pairs = parse_qsl(next_parts.query, keep_blank_values=True)
            if (
                next_parts.path != path
                or len(pairs) != len(expected_query)
                or dict(pairs) != expected_query
            ):
                raise ProviderError("provider_invalid_response")

    async def _association(
        self, root: str, synthetic: str, head: str, base: str
    ) -> SyntheticMergeAssociation:
        raw = _object((await self._get(f"{root}/git/commits/{synthetic}")).data)
        parents = raw.get("parents")
        if (
            raw.get("sha") != synthetic
            or synthetic in {head, base}
            or not isinstance(parents, list)
            or len(parents) != 2
            or any(not isinstance(parent, dict) for parent in parents)
            or [parent.get("sha") for parent in parents] != [base, head]
        ):
            raise ProviderError("provider_revision_unverifiable")
        return SyntheticMergeAssociation(synthetic_sha=synthetic, head_sha=head, base_sha=base)

    def _check(self, raw: Mapping[str, Any], root: str, sha: str) -> CheckAttempt:
        identity = _positive(raw["id"])
        app, suite = _object(raw["app"]), _object(raw["check_suite"])
        state, conclusion = raw["status"], raw.get("conclusion")
        if state in {"queued", "in_progress", "requested", "waiting", "pending"}:
            conclusion = "pending"
        elif state == "completed":
            if conclusion in {"timed_out", "action_required", "stale", "startup_failure"}:
                conclusion = "failure"
            elif conclusion not in {"success", "failure", "skipped", "neutral", "cancelled"}:
                raise ProviderError("provider_invalid_response")
        else:
            raise ProviderError("provider_invalid_response")
        if raw["head_sha"] != sha:
            raise ProviderError("provider_invalid_response")
        return CheckAttempt.model_validate(
            {
                "record_id": identity,
                "provider_id": _positive(app["id"]),
                "kind": "check_run",
                "name": _text(raw["name"]),
                "app_slug": _text(app["slug"]),
                "head_sha": sha,
                "conclusion": conclusion,
                "started_at": _time(raw.get("started_at"), optional=True),
                "completed_at": _time(raw.get("completed_at"), optional=state != "completed"),
                "check_suite_id": _positive(suite["id"]),
                "record_url": self._record_url(raw["url"], f"{root}/check-runs/{identity}"),
            }
        )

    def _status(self, raw: Mapping[str, Any], root: str, sha: str) -> CheckAttempt:
        creator = _object(raw["creator"])
        state = raw["state"]
        if state not in {"success", "pending", "failure", "error"}:
            raise ProviderError("provider_invalid_response")
        return CheckAttempt.model_validate(
            {
                "record_id": _positive(raw["id"]),
                "provider_id": _positive(creator["id"]),
                "kind": "commit_status",
                "name": _text(raw["context"]),
                "app_slug": _text(creator["login"]),
                "head_sha": sha,
                "conclusion": "failure" if state == "error" else state,
                "started_at": _time(raw["created_at"]),
                "completed_at": _time(raw["updated_at"]),
                "record_url": self._record_url(raw["url"], f"{root}/statuses/{sha}"),
            }
        )

    def _review(self, raw: Mapping[str, Any], root: str, pr_number: int) -> ReviewEvidence | None:
        if raw["state"] == "PENDING":
            return None  # An unpublished draft does not change an effective decision.
        states: dict[str, Literal["approved", "changes_requested", "dismissed", "commented"]] = {
            "APPROVED": "approved",
            "CHANGES_REQUESTED": "changes_requested",
            "DISMISSED": "dismissed",
            "COMMENTED": "commented",
        }
        user = _object(raw["user"])
        identity = _positive(raw["id"])
        return ReviewEvidence.model_validate(
            {
                "record_id": identity,
                "provider_id": _positive(user["id"]),
                "reviewer": _text(user["login"]),
                "head_sha": _sha(raw["commit_id"]),
                "decision": states[raw["state"]],
                "submitted_at": _time(raw["submitted_at"]),
                "record_url": self._record_url(
                    raw["url"], f"{root}/pulls/{pr_number}/reviews/{identity}"
                ),
            }
        )

    async def collect(
        self,
        binding: ArtifactBinding,
        contract: ContractRevision,
        *,
        previous: PullRequestEvidence | None = None,
    ) -> PullRequestEvidence:
        """Return complete facts only after the closing PR identity fence passes."""
        try:
            return await self._collect(binding, contract, previous=previous)
        except (KeyError, TypeError, ValueError, ValidationError, OverflowError):
            # Arbitrary provider bodies or validation input never escape the adapter.
            raise ProviderError("provider_invalid_response") from None

    async def _collect(
        self,
        binding: ArtifactBinding,
        contract: ContractRevision,
        *,
        previous: PullRequestEvidence | None,
    ) -> PullRequestEvidence:
        deliverable = self._deliverable(binding, contract)
        root = self._address(binding.repository_id)
        opening_raw = _object((await self._get(f"{root}/pulls/{binding.pr_number}")).data)
        opening, name, synthetic = self._pr(opening_raw)
        if (
            opening["repository_id"] != binding.repository_id
            or opening["pr_number"] != binding.pr_number
            or opening["base_ref"] != deliverable.target_branch
        ):
            raise ProviderError("provider_invalid_response")
        # A rename may change the address, never the registered numeric identity.
        root = "/repos/" + name
        head, base = opening["head_sha"], opening["base_sha"]
        associations: list[SyntheticMergeAssociation] = []
        if deliverable.required_checks:
            if synthetic is not None:
                associations.append(await self._association(root, synthetic, head, base))
            elif opening["state"] == "merged" and previous is not None and previous.complete:
                paired = all(
                    getattr(previous, key) == opening[key]
                    for key in (
                        "provider_id",
                        "repository_id",
                        "pr_number",
                        "head_sha",
                        "base_sha",
                    )
                )
                if paired:
                    candidates = {
                        item.synthetic_sha
                        for item in previous.synthetic_merges
                        if item.head_sha == head and item.base_sha == base
                    }
                    if len(candidates) > 1:
                        raise ProviderError("provider_revision_unverifiable")
                    for candidate in candidates:
                        associations.append(await self._association(root, candidate, head, base))
        kinds = {check.kind for check in deliverable.required_checks}
        checks: list[CheckAttempt] = []
        # H and S share each family's page budget and the domain's record cap.
        check_budget, status_budget = _PageBudget(), _PageBudget()
        for sha in [head, *(association.synthetic_sha for association in associations)]:
            if "check_run" in kinds:
                checks.extend(
                    self._check(raw, root, sha)
                    for raw in await self._records(
                        f"{root}/commits/{sha}/check-runs",
                        check_budget,
                        check_runs=True,
                    )
                )
            if "commit_status" in kinds:
                checks.extend(
                    self._status(raw, root, sha)
                    for raw in await self._records(
                        f"{root}/commits/{sha}/statuses",
                        status_budget,
                    )
                )
            if len(checks) > 2000:
                raise ProviderError("provider_invalid_response")
        record_ids = [(item.kind, item.record_id) for item in checks]
        if len(record_ids) != len(set(record_ids)):
            raise ProviderError("provider_invalid_response")
        reviews: list[ReviewEvidence] = []
        if deliverable.review.required_approvals:
            for raw in await self._records(
                f"{root}/pulls/{binding.pr_number}/reviews", _PageBudget()
            ):
                review = self._review(raw, root, binding.pr_number)
                if review is not None:
                    reviews.append(review)
        closing_raw = _object((await self._get(f"{root}/pulls/{binding.pr_number}")).data)
        closing, closing_name, closing_synthetic = self._pr(closing_raw)
        # mergeable may transition from null while GitHub computes it; reject
        # that race too rather than attaching a closing fact to an opening pair.
        if (
            opening != closing
            or name != closing_name
            or synthetic != closing_synthetic
            or opening_raw["user"]["id"] != closing_raw["user"]["id"]
        ):
            raise ProviderError("provider_revision_changed")
        if opening["state"] == "merged" and kinds and not checks and not associations:
            raise ProviderError("provider_revision_unverifiable")
        collected_at = self.now()
        if collected_at.utcoffset() is None:
            raise ProviderError("provider_invalid_response")
        return PullRequestEvidence.model_validate(
            {
                **opening,
                "checks": checks,
                "reviews": reviews,
                "synthetic_merges": associations,
                "complete": True,
                "collected_at": collected_at,
            }
        )
