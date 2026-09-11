"""Pinned repository context resolves through immutable Git objects, without bodies."""

from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from brain_v42.delivery_config import DeliverySettings
from brain_v42.delivery_observer.github import GitHubClient
from brain_v42.delivery_observer.transport import GitHubTransport, ProviderError
from brain_v42.models.delivery import ContractRevision
from tests.delivery_helpers import stored_contract_payload

from .github_cases import RID, ROOT, B, H, S, X


class ContextCase:
    def __init__(self):
        self.payload = stored_contract_payload()
        self.payload["context_refs"] = [
            {
                "kind": "repository_document",
                "repository_id": RID,
                "sha": H,
                "path": "docs/spec.md",
                "required": True,
            }
        ]
        self.repo = {"id": RID, "full_name": "hawkixs/brain-v42"}
        self.commit = {"sha": H, "tree": {"sha": B}, "parents": []}
        self.tree = {
            "sha": B,
            "truncated": False,
            "tree": [
                {"path": "docs", "mode": "040000", "type": "tree", "sha": S},
                {"path": "docs/spec.md", "mode": "100644", "type": "blob", "sha": X, "size": 42},
                {"path": "README.md", "mode": "100644", "type": "blob", "sha": S, "size": 19},
            ],
        }
        self.requests = []
        self.failure_status = None

    def handle(self, request):
        assert request.method == "GET"
        self.requests.append((request.url.path, dict(request.url.params)))
        if self.failure_status:
            return httpx.Response(self.failure_status, json={"message": "private provider detail"})
        if request.url.path == ROOT:
            return httpx.Response(200, json=deepcopy(self.repo))
        if request.url.path == f"{ROOT}/git/commits/{H}":
            return httpx.Response(200, json=deepcopy(self.commit))
        if request.url.path == f"{ROOT}/git/trees/{B}":
            assert dict(request.url.params) == {"recursive": "1"}
            return httpx.Response(200, json=deepcopy(self.tree))
        raise AssertionError(f"unexpected endpoint {request.url.path}")

    async def collect(self):
        async def headers():
            return {"Authorization": "Bearer fixture"}

        async def invalidate(_headers):
            return None

        settings = DeliverySettings(repository_registry={"executor": {RID: "hawkixs/brain-v42"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handle)) as http:
            auth = SimpleNamespace(
                transport=GitHubTransport(http, settings),
                authorization_headers=headers,
                invalidate=invalidate,
            )
            client = GitHubClient(http, settings, auth)
            return await client.collect_repository_context(
                ContractRevision.model_validate(self.payload)
            )


@pytest.mark.parametrize("mode", ["100644", "100755"])
@pytest.mark.asyncio
async def test_exact_regular_file_pin_resolves_without_reading_document_body(mode):
    case = ContextCase()
    case.tree["tree"][1]["mode"] = mode
    result = await case.collect()
    assert result.complete and len(result.facts) == 1
    assert result.facts[0].model_dump() == {
        "repository_id": RID,
        "commit_sha": H,
        "path": "docs/spec.md",
        "tree_sha": B,
        "blob_sha": X,
        "mode": mode,
        "status": "available",
    }
    assert len(case.requests) == 3
    assert not any("/contents/" in path or "/blobs/" in path for path, _ in case.requests)


@pytest.mark.asyncio
async def test_required_set_is_complete_and_shared_objects_are_fetched_once():
    case = ContextCase()
    case.payload["context_refs"].append(
        {
            "kind": "repository_document",
            "repository_id": RID,
            "sha": H,
            "path": "README.md",
            "required": True,
        }
    )
    result = await case.collect()
    assert result.complete and {fact.path for fact in result.facts} == {"docs/spec.md", "README.md"}
    assert len(case.requests) == 3


@pytest.mark.parametrize("kind", ["absent", "symlink", "submodule", "directory", "symlink_parent"])
@pytest.mark.asyncio
async def test_non_regular_or_missing_path_is_not_available(kind):
    case = ContextCase()
    entry = case.tree["tree"][1]
    if kind == "absent":
        case.tree["tree"].pop(1)
    elif kind == "symlink":
        entry.update(mode="120000", type="blob")
    elif kind == "submodule":
        entry.update(mode="160000", type="commit")
    elif kind == "directory":
        entry.update(mode="040000", type="tree")
    else:
        case.tree["tree"][0].update(mode="120000", type="blob")
    result = await case.collect()
    assert not result.complete and result.facts[0].status == "missing"


@pytest.mark.parametrize(
    "kind",
    [
        "repo",
        "commit",
        "tree",
        "truncated",
        "no_truncation_flag",
        "duplicate_path",
        "no_records",
        "malformed_sha",
        "unsafe_path",
    ],
)
@pytest.mark.asyncio
async def test_ambiguous_or_wrong_object_proof_fails_closed(kind):
    case = ContextCase()
    if kind == "repo":
        case.repo["id"] += 1
    elif kind == "commit":
        case.commit["sha"] = X
    elif kind == "tree":
        case.tree["sha"] = X
    elif kind == "truncated":
        case.tree["truncated"] = True
    elif kind == "no_truncation_flag":
        case.tree.pop("truncated")
    elif kind == "duplicate_path":
        case.tree["tree"].append(deepcopy(case.tree["tree"][1]))
    elif kind == "no_records":
        case.tree.pop("tree")
    elif kind == "malformed_sha":
        case.tree["tree"][1]["sha"] = "bad"
    else:
        case.tree["tree"][1]["path"] = "../escape"
    with pytest.raises(ProviderError) as failure:
        await case.collect()
    assert failure.value.code == "provider_invalid_response"


@pytest.mark.parametrize(
    "status,code",
    [(403, "provider_forbidden"), (404, "provider_not_found"), (429, "provider_rate_limited")],
)
@pytest.mark.asyncio
async def test_failed_context_request_is_safe_typed_failure(status, code):
    case = ContextCase()
    case.failure_status = status
    with pytest.raises(ProviderError) as failure:
        await case.collect()
    assert failure.value.code == code
    assert "private provider detail" not in str(failure.value)


@pytest.mark.asyncio
async def test_optional_documents_do_not_create_provider_jobs():
    case = ContextCase()
    case.payload["context_refs"][0]["required"] = False
    result = await case.collect()
    assert not result.facts and not case.requests


@pytest.mark.asyncio
async def test_missing_one_required_document_never_confirms_partial_set():
    case = ContextCase()
    case.payload["context_refs"].append(
        {
            "kind": "repository_document",
            "repository_id": RID,
            "sha": H,
            "path": "missing.md",
            "required": True,
        }
    )
    result = await case.collect()
    assert not result.complete
    assert {fact.status for fact in result.facts} == {"available", "missing"}


@pytest.mark.asyncio
async def test_unregistered_repository_is_rejected_before_network():
    case = ContextCase()
    case.payload["context_refs"][0]["repository_id"] = 999
    with pytest.raises(ProviderError) as failure:
        await case.collect()
    assert failure.value.code == "provider_invalid_response"
    assert not case.requests
