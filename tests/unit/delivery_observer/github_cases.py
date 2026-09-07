"""Full HTTP fixtures for one registered pull request and its immutable records."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlencode

import httpx

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.transport import GitHubTransport
from brain_v42.models.delivery import (
    ArtifactBinding,
    BindingEvidence,
    ContractRevision,
    EvaluationInput,
    ObservationConfirmation,
)
from brain_v42.models.delivery_evaluator import evaluate_delivery
from tests.delivery_helpers import stored_contract_payload

RID = 42
PR = 73
APP = 15368
H = "a" * 40
B = "b" * 40
M = "c" * 40
S = "d" * 40
X = "e" * 40
NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)
ROOT = "/repos/hawkixs/brain-v42"


def check(record_id=8001, *, sha=H, state="completed", conclusion="success", app=APP):
    return {
        "id": record_id,
        "name": "test-unit",
        "head_sha": sha,
        "status": state,
        "conclusion": conclusion,
        "started_at": "2026-09-07T11:59:00Z",
        "completed_at": "2026-09-07T11:59:30Z" if state == "completed" else None,
        "app": {"id": app, "slug": "github-actions"},
        "check_suite": {"id": 9001},
        "url": f"https://api.github.com{ROOT}/check-runs/{record_id}",
        "pull_requests": [],
    }


def status(record_id=8101, *, sha=H, state="success"):
    return {
        "id": record_id,
        "context": "legacy-ci",
        "state": state,
        "description": "fixture",
        "creator": {"id": 900, "login": "trusted-ci"},
        "created_at": "2026-09-07T11:59:00Z",
        "updated_at": "2026-09-07T11:59:30Z",
        "url": f"https://api.github.com{ROOT}/statuses/{sha}",
    }


def review(record_id=9101, *, state="APPROVED", sha=H):
    return {
        "id": record_id,
        "user": {"id": 901, "login": "reviewer"},
        "state": state,
        "commit_id": sha,
        "submitted_at": f"2026-09-07T11:59:{record_id % 60:02d}Z",
        "url": f"https://api.github.com{ROOT}/pulls/{PR}/reviews/{record_id}",
    }


class GitHubCase:
    def __init__(self, *, merged=True, synthetic=False, approvals=0, statuses=False):
        payload = stored_contract_payload()
        payload["deliverables"][0]["required_checks"][0]["provider_id"] = APP
        payload["deliverables"][0]["review"] = {
            "required_approvals": approvals,
            "allowed_reviewers": ["reviewer"],
        }
        if statuses:
            payload["deliverables"][0]["required_checks"].append(
                {
                    "kind": "commit_status",
                    "name": "legacy-ci",
                    "provider_id": 900,
                    "app_slug": "trusted-ci",
                }
            )
        self.contract = ContractRevision.model_validate(payload)
        self.binding = ArtifactBinding(
            ticket_id=self.contract.ticket_id,
            contract_revision=1,
            attempt=1,
            deliverable_key="implementation",
            repository_id=RID,
            pr_number=PR,
        )
        self.pr = {
            "id": 7001,
            "number": PR,
            "state": "closed" if merged else "open",
            "merged": merged,
            "draft": False,
            "mergeable": True,
            "merge_commit_sha": M if merged else S if synthetic else None,
            "user": {"id": 800, "login": "author"},
            "head": {
                "sha": H,
                "repo": {"id": 43, "full_name": "contributor/fork"},
                "ref": "feature",
            },
            "base": {
                "sha": B,
                "ref": "main",
                "repo": {"id": RID, "full_name": "hawkixs/brain-v42"},
            },
        }
        self.after = None
        self.pr_reads = 0
        self.checks = {H: [[check()]], S: [[]]}
        self.statuses = {H: [[status()]], S: [[]]}
        self.reviews = [[review()]]
        self.parent = {"sha": S, "parents": [{"sha": B}, {"sha": H}], "tree": {"sha": X}}
        self.requests = []
        self.bad_next = None
        self.missing_records = False
        self.total_override = None
        self.elapsed = 0.0

    def page(self, request, pages, *, checks=False):
        page = int(request.url.params.get("page", "1"))
        records = deepcopy(pages[page - 1]) if page <= len(pages) else []
        headers = {}
        if page < len(pages):
            query = dict(request.url.params)
            query["page"] = str(page + 1)
            next_url = (
                self.bad_next or f"https://api.github.com{request.url.path}?{urlencode(query)}"
            )
            headers["Link"] = f'<{next_url}>; rel="next"'
        data = records
        if checks:
            data = {
                "total_count": self.total_override
                if self.total_override is not None
                else sum(map(len, pages)),
                "check_runs": records,
            }
            if self.missing_records:
                data.pop("check_runs")
        return httpx.Response(200, json=data, headers=headers)

    def handle(self, request):
        self.requests.append((request.method, request.url.path, dict(request.url.params)))
        assert request.method == "GET"
        path = request.url.path
        if path == f"{ROOT}/pulls/{PR}":
            self.pr_reads += 1
            return httpx.Response(
                200,
                json=deepcopy(
                    self.after if self.pr_reads % 2 == 0 and self.after is not None else self.pr
                ),
            )
        if path == f"{ROOT}/git/commits/{S}":
            return httpx.Response(200, json=deepcopy(self.parent))
        if path == f"{ROOT}/pulls/{PR}/reviews":
            return self.page(request, self.reviews)
        for sha in (H, S):
            if path == f"{ROOT}/commits/{sha}/check-runs":
                assert request.url.params.get("filter") == "all"
                assert request.url.params.get("per_page") == "100"
                return self.page(request, self.checks[sha], checks=True)
            if path == f"{ROOT}/commits/{sha}/statuses":
                return self.page(request, self.statuses[sha])
        raise AssertionError(f"unexpected fixture endpoint: {path}")

    async def collect(self, *, previous=None):
        from brain_v42.delivery_observer.github import GitHubClient

        async def sleep(delay):
            self.elapsed += delay
            await asyncio.sleep(0)

        settings = DeliverySettings(repository_registry={"executor": {RID: "hawkixs/brain-v42"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as http:
            transport = GitHubTransport(http, settings, monotonic=lambda: self.elapsed, sleep=sleep)

            async def headers():
                return {"Authorization": "Bearer fixture"}

            auth = SimpleNamespace(transport=transport, authorization_headers=headers)
            client = GitHubClient(http, settings, auth, now=lambda: NOW)
            return await client.collect(self.binding, self.contract, previous=previous)

    def assess(self, evidence):
        binding = self.binding.model_copy(
            update={
                "state": "observed",
                "head_sha": evidence.head_sha,
                "base_sha": evidence.base_sha,
                "integration_sha": evidence.integration_sha,
            }
        )
        confirmation = ObservationConfirmation(
            evidence=evidence, collection_started_at=NOW, collection_finished_at=NOW
        )
        inputs = EvaluationInput(
            contract=self.contract,
            attempt=1,
            workflow_version=1,
            coordination_status="open",
            coordination_disposition="active",
            is_self_ticket=False,
            active_bindings=(BindingEvidence(binding=binding, confirmation=confirmation),),
            feature_enabled=True,
            freshness_seconds=600,
            executor_identity="executor",
        )
        return evaluate_delivery(inputs, now=NOW)
