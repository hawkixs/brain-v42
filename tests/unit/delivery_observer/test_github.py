"""HTTP-boundary acceptance tests for complete GitHub revision evidence."""

from copy import deepcopy

import pytest

from brain_v42.delivery_observer.transport import ProviderError
from brain_v42.models.delivery import CheckAttempt, ContractRevision, ReviewEvidence

from .github_cases import APP, NOW, PR, RID, ROOT, B, GitHubCase, H, M, S, X, check, review, status


def codes(case, evidence):
    assessment = case.assess(evidence)
    return {item.code for item in (*assessment.deliverables, *assessment.blockers)}


@pytest.mark.asyncio
async def test_merged_fork_preserves_exact_identity_and_provider_records():
    case = GitHubCase(approvals=1)
    result = await case.collect()
    assert result.complete and result.collected_at == NOW
    assert (result.repository_id, result.pr_number, result.provider_id) == (RID, PR, 7001)
    assert (result.head_sha, result.base_sha, result.integration_sha) == (H, B, M)
    assert result.head_repository_id == 43
    assert result.author_id == "author" and result.base_ref == "main"
    assert result.state == "merged" and not result.synthetic_merges
    run = result.checks[0]
    assert (run.record_id, run.provider_id, run.app_slug) == (8001, APP, "github-actions")
    assert run.check_suite_id == 9001 and run.record_url.endswith("/check-runs/8001")
    assert result.reviews[0].record_url.endswith(f"/pulls/{PR}/reviews/9101")
    assert case.assess(result).integration_receipt_eligible
    assert case.pr_reads == 2


@pytest.mark.asyncio
async def test_synthetic_revision_requires_ordered_parent_proof_and_survives_merge():
    case = GitHubCase(merged=False, synthetic=True)
    case.checks[H] = [[]]
    case.checks[S] = [[check(8002, sha=S)]]
    before = await case.collect()
    assert before.synthetic_merges[0].model_dump() == {
        "synthetic_sha": S,
        "head_sha": H,
        "base_sha": B,
    }
    assert not before.integration_sha
    assert case.assess(before).delivery_stage == "verified"
    case.pr.update(merged=True, state="closed", merge_commit_sha=M)
    after = await case.collect(previous=before)
    assert after.integration_sha == M and after.synthetic_merges == before.synthetic_merges
    assert case.assess(after).integration_receipt_eligible
    assert sum(path.endswith(f"/git/commits/{S}") for _, path, _ in case.requests) == 2
    assert not any(path.endswith(f"/commits/{M}/check-runs") for _, path, _ in case.requests)


@pytest.mark.parametrize(
    "mutation", ["returned_sha", "reversed", "extra", "wrong_head", "wrong_base"]
)
@pytest.mark.asyncio
async def test_synthetic_parent_proof_rejects_each_mismatch(mutation):
    case = GitHubCase(merged=False, synthetic=True)
    if mutation == "returned_sha":
        case.parent["sha"] = X
    elif mutation == "reversed":
        case.parent["parents"].reverse()
    elif mutation == "extra":
        case.parent["parents"].append({"sha": X})
    elif mutation == "wrong_head":
        case.parent["parents"][1]["sha"] = X
    else:
        case.parent["parents"][0]["sha"] = X
    with pytest.raises(ProviderError) as failure:
        await case.collect()
    assert failure.value.code == "provider_revision_unverifiable"


@pytest.mark.asyncio
async def test_merged_without_direct_checks_or_previous_parent_proof_is_unverifiable():
    case = GitHubCase()
    case.checks[H] = [[]]
    with pytest.raises(ProviderError) as failure:
        await case.collect()
    assert failure.value.code == "provider_revision_unverifiable"
    assert not any(M in path for _, path, _ in case.requests)


@pytest.mark.parametrize(
    "field",
    [
        "head",
        "base",
        "repository",
        "pr",
        "provider",
        "state",
        "synthetic",
        "draft",
        "branch",
        "head_repo",
        "author",
    ],
)
@pytest.mark.asyncio
async def test_before_after_revision_and_identity_changes_reject_snapshot(field):
    case = GitHubCase(merged=False, synthetic=True)
    case.after = deepcopy(case.pr)
    if field in {"head", "base"}:
        case.after[field]["sha"] = X
    elif field == "repository":
        case.after["base"]["repo"]["id"] = 999
    elif field == "head_repo":
        case.after["head"]["repo"]["id"] = 999
    elif field == "pr":
        case.after["number"] += 1
    elif field == "provider":
        case.after["id"] += 1
    elif field == "state":
        case.after["state"] = "closed"
    elif field == "synthetic":
        case.after["merge_commit_sha"] = X
    elif field == "draft":
        case.after["draft"] = True
    elif field == "branch":
        case.after["base"]["ref"] = "different"
    else:
        case.after["user"]["login"] = "different"
    with pytest.raises(ProviderError) as failure:
        await case.collect()
    assert failure.value.code == "provider_revision_changed"


@pytest.mark.parametrize("field", ["repo", "pr", "base", "malformed_head"])
@pytest.mark.asyncio
async def test_initial_identity_mismatch_never_collects_positive_checks(field):
    case = GitHubCase()
    if field == "repo":
        case.pr["base"]["repo"]["id"] += 1
    elif field == "pr":
        case.pr["number"] += 1
    elif field == "base":
        case.pr["base"]["ref"] = "other"
    else:
        case.pr["head"]["sha"] = "not-a-revision"
    with pytest.raises(ProviderError):
        await case.collect()
    assert len(case.requests) == 1


@pytest.mark.parametrize(
    "field", ["provider_id", "head_sha", "base_sha", "repository_id", "pr_number", "complete"]
)
@pytest.mark.asyncio
async def test_previous_synthetic_proof_cannot_be_borrowed_from_other_revision(field):
    case = GitHubCase(merged=False, synthetic=True)
    case.checks[H] = [[]]
    case.checks[S] = [[check(8002, sha=S)]]
    previous = await case.collect()
    wrong = False if field == "complete" else X if field.endswith("sha") else 999
    previous = previous.model_copy(update={field: wrong})
    case.pr.update(merged=True, state="closed", merge_commit_sha=M)
    with pytest.raises(ProviderError) as failure:
        await case.collect(previous=previous)
    assert failure.value.code == "provider_revision_unverifiable"


@pytest.mark.asyncio
async def test_same_check_name_from_wrong_app_is_not_trusted():
    case = GitHubCase()
    case.checks[H] = [[check(app=123)]]
    result = await case.collect()
    assert "check_missing" in codes(case, result)
    assert not case.assess(result).integration_receipt_eligible


@pytest.mark.asyncio
async def test_all_check_pages_include_newer_pending_rerun():
    case = GitHubCase()
    case.checks[H] = [[check()], [check(8002, state="queued", conclusion=None)]]
    result = await case.collect()
    assert [run.record_id for run in result.checks] == [8001, 8002]
    assert result.checks[1].conclusion == "pending"
    assert "check_pending" in codes(case, result)
    assert not case.assess(result).integration_receipt_eligible


@pytest.mark.asyncio
async def test_newer_pending_synthetic_attempt_cannot_be_hidden_by_direct_success():
    case = GitHubCase(merged=False, synthetic=True)
    case.checks[S] = [[check(8002, sha=S, state="in_progress", conclusion=None)]]
    result = await case.collect()
    assert "check_pending" in codes(case, result)
    assert {run.head_sha for run in result.checks} == {H, S}


@pytest.mark.parametrize(
    "decision,expected",
    [("DISMISSED", "review_approval_missing"), ("CHANGES_REQUESTED", "review_changes_requested")],
)
@pytest.mark.asyncio
async def test_later_review_page_invalidates_approval(decision, expected):
    case = GitHubCase(approvals=1)
    case.reviews = [[review()], [review(9102, state=decision)]]
    result = await case.collect()
    assert len(result.reviews) == 2
    assert expected in codes(case, result)
    assert not case.assess(result).integration_receipt_eligible


@pytest.mark.asyncio
async def test_approval_on_old_revision_and_author_approval_do_not_count():
    case = GitHubCase(approvals=1)
    author_review = review(9102)
    author_review["user"] = {"id": 800, "login": "author"}
    case.reviews = [[review(sha=X), author_review]]
    result = await case.collect()
    assert "review_approval_missing" in codes(case, result)


@pytest.mark.asyncio
async def test_commit_statuses_retain_distinct_record_creator_identity_and_latest_page():
    case = GitHubCase(statuses=True)
    case.statuses[H] = [[status()], [status(8102, state="pending")]]
    result = await case.collect()
    statuses = [item for item in result.checks if item.kind == "commit_status"]
    assert [(item.record_id, item.provider_id) for item in statuses] == [(8101, 900), (8102, 900)]
    assert statuses[0].record_url.endswith(f"/statuses/{H}")
    assert "check_pending" in codes(case, result)


@pytest.mark.asyncio
async def test_required_status_success_is_eligible():
    case = GitHubCase(statuses=True)
    assert case.assess(await case.collect()).integration_receipt_eligible


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "total",
        "duplicate",
        "oversize",
        "wrong_sha",
        "wrong_url",
        "naive_time",
        "unknown_state",
    ],
)
@pytest.mark.asyncio
async def test_incomplete_or_malformed_check_records_are_not_success(mutation):
    case = GitHubCase()
    if mutation == "missing":
        case.missing_records = True
    elif mutation == "total":
        case.total_override = 2
    elif mutation == "duplicate":
        case.checks[H] = [[check()], [check()]]
    elif mutation == "oversize":
        case.checks[H] = [[check(8000 + i) for i in range(101)]]
    elif mutation == "wrong_sha":
        case.checks[H][0][0]["head_sha"] = X
    elif mutation == "wrong_url":
        case.checks[H][0][0]["url"] = "https://api.github.com/repos/other/repo/check-runs/8001"
    elif mutation == "naive_time":
        case.checks[H][0][0]["started_at"] = "2026-09-07T11:00:00"
    else:
        case.checks[H][0][0]["status"] = "invented"
    with pytest.raises(ProviderError) as failure:
        await case.collect()
    assert failure.value.code == "provider_invalid_response"


@pytest.mark.parametrize("kind", ["check", "status", "review"])
@pytest.mark.asyncio
async def test_more_than_twenty_pages_fails_closed(kind):
    case = GitHubCase(statuses=kind == "status", approvals=int(kind == "review"))
    if kind == "check":
        case.checks[H] = [[check(8000 + i)] for i in range(21)]
    elif kind == "status":
        case.statuses[H] = [[status(8100 + i)] for i in range(21)]
    else:
        case.reviews = [[review(9100 + i)] for i in range(21)]
    with pytest.raises(ProviderError) as failure:
        await case.collect()
    assert failure.value.code == "provider_invalid_response"
    assert all(int(query.get("page", 1)) <= 20 for _, _, query in case.requests)


@pytest.mark.asyncio
async def test_head_and_synthetic_share_twenty_page_budget():
    case = GitHubCase(merged=False, synthetic=True)
    case.checks[H] = [[check(8000 + i)] for i in range(11)]
    case.checks[S] = [[check(9000 + i, sha=S)] for i in range(10)]
    with pytest.raises(ProviderError):
        await case.collect()
    assert sum(path.endswith("/check-runs") for _, path, _ in case.requests) == 20


@pytest.mark.parametrize(
    "next_url",
    [
        f"https://api.github.com/repos/other/repo/commits/{H}/check-runs?filter=all&per_page=100&page=2",
        f"https://api.github.com{ROOT}/commits/{H}/check-runs?filter=latest&per_page=100&page=2",
        f"https://api.github.com{ROOT}/commits/{H}/check-runs?filter=all&per_page=100&page=1",
    ],
)
@pytest.mark.asyncio
async def test_same_origin_pagination_cannot_change_endpoint_filter_or_repeat_page(next_url):
    case = GitHubCase()
    case.checks[H] = [[check()], [check(8002)]]
    case.bad_next = next_url
    with pytest.raises(ProviderError) as failure:
        await case.collect()
    assert failure.value.code == "provider_invalid_response"
    assert len(case.requests) == 2


@pytest.mark.parametrize(
    "state,draft,expected",
    [("open", False, "open"), ("open", True, "open"), ("closed", False, "closed")],
)
@pytest.mark.asyncio
async def test_unmerged_states_are_preserved_without_integration_identity(state, draft, expected):
    case = GitHubCase(merged=False)
    case.pr.update(state=state, draft=draft)
    result = await case.collect()
    assert result.state == expected and result.draft == draft
    assert not result.integration_sha
    assert not case.assess(result).integration_receipt_eligible


@pytest.mark.asyncio
async def test_explicit_no_checks_and_no_reviews_skip_unnecessary_provider_calls():
    case = GitHubCase()
    payload = case.contract.model_dump()
    payload.pop("content_digest")
    payload["deliverables"][0].update(required_checks=[], no_checks_reason="Documentation only")
    case.contract = ContractRevision.model_validate(payload)
    result = await case.collect()
    assert not result.checks and not result.reviews
    assert len(case.requests) == 2
    assert case.assess(result).integration_receipt_eligible


def test_provider_record_metadata_survives_model_storage():
    attempt = CheckAttempt.model_validate(
        {
            "record_id": 8001,
            "provider_id": APP,
            "kind": "check_run",
            "name": "test-unit",
            "head_sha": H,
            "conclusion": "success",
            "check_suite_id": 9001,
            "record_url": f"https://api.github.com{ROOT}/check-runs/8001",
        }
    )
    decision = ReviewEvidence.model_validate(
        {
            "record_id": 9101,
            "provider_id": 901,
            "reviewer": "reviewer",
            "head_sha": H,
            "decision": "approved",
            "submitted_at": NOW,
            "record_url": f"https://api.github.com{ROOT}/pulls/{PR}/reviews/9101",
        }
    )
    assert attempt.model_dump()["check_suite_id"] == 9001
    assert decision.model_dump()["record_url"] == decision.record_url


@pytest.mark.parametrize("integration", [H, M, X])
@pytest.mark.asyncio
async def test_integration_identity_is_exactly_the_provider_returned_commit(integration):
    # GitHub does not expose a reliable merge-method discriminator in this
    # response. Preserve the SHA whether a merge, squash or rebase produced it.
    case = GitHubCase()
    case.pr["merge_commit_sha"] = integration
    result = await case.collect()
    assert result.integration_sha == integration
    assert case.assess(result).integration_receipt_eligible
    assert not result.synthetic_merges
    assert {path for _, path, _ in case.requests} == {
        f"{ROOT}/pulls/{PR}",
        f"{ROOT}/commits/{H}/check-runs",
    }


@pytest.mark.parametrize("family", ["check", "status"])
@pytest.mark.parametrize("synthetic_pages", [10, 11])
@pytest.mark.asyncio
async def test_exact_shared_page_and_record_boundary(family, synthetic_pages):
    case = GitHubCase(merged=False, synthetic=True, statuses=family == "status")
    if family == "status":
        payload = case.contract.model_dump()
        payload.pop("content_digest")
        payload["deliverables"][0]["required_checks"] = [
            payload["deliverables"][0]["required_checks"][1]
        ]
        case.contract = ContractRevision.model_validate(payload)
    make_record = check if family == "check" else status
    pages = case.checks if family == "check" else case.statuses
    for sha, count, offset in [(H, 10, 10000), (S, synthetic_pages, 20000)]:
        pages[sha] = [
            [make_record(offset + page * 100 + i, sha=sha) for i in range(100)]
            for page in range(count)
        ]
    if synthetic_pages == 10:
        result = await case.collect()
        assert len(result.checks) == 2000
        assert case.assess(result).delivery_stage == "verified"
    else:
        with pytest.raises(ProviderError) as failure:
            await case.collect()
        assert failure.value.code == "provider_invalid_response"
    suffix = "/check-runs" if family == "check" else "/statuses"
    assert sum(path.endswith(suffix) for _, path, _ in case.requests) == 20


@pytest.mark.asyncio
async def test_checks_and_statuses_together_cannot_overflow_evidence_record_bound():
    case = GitHubCase(statuses=True)
    case.checks[H] = [[check(10000 + page * 100 + i) for i in range(100)] for page in range(11)]
    case.statuses[H] = [[status(20000 + page * 100 + i) for i in range(100)] for page in range(10)]
    with pytest.raises(ProviderError) as failure:
        await case.collect()
    assert failure.value.code == "provider_invalid_response"


@pytest.mark.parametrize("family", ["review", "status"])
@pytest.mark.asyncio
async def test_record_url_cannot_borrow_another_pull_request_or_revision(family):
    case = GitHubCase(approvals=int(family == "review"), statuses=family == "status")
    if family == "review":
        case.reviews[0][0]["url"] = f"https://api.github.com{ROOT}/pulls/{PR + 1}/reviews/9101"
    else:
        case.statuses[H][0][0]["url"] = f"https://api.github.com{ROOT}/statuses/{X}"
    with pytest.raises(ProviderError) as failure:
        await case.collect()
    assert failure.value.code == "provider_invalid_response"
