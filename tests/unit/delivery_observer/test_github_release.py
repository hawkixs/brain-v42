from datetime import UTC, datetime

import pytest

from brain_v42.delivery_observer.transport import ProviderError
from tests.unit.delivery_observer.github_cases import RID
from tests.unit.delivery_observer.release_cases import ReleaseCase, tag

A = "a" * 40
B2 = "b" * 40
C2 = "c" * 40
D2 = "d" * 40
M = "e" * 40


@pytest.mark.asyncio
async def test_release_tags_keep_only_bare_v_versions_across_pages():
    case = ReleaseCase(
        tags=[[tag("v0.6.2", A), tag("v0.6.3-rc1", B2)], [tag("latest", C2), tag("v0.6.3", D2)]]
    )
    tags = await case.call("release_tags", RID)
    assert [(t.name, t.version, t.sha) for t in tags] == [
        ("v0.6.2", "0.6.2", A),
        ("v0.6.3", "0.6.3", D2),
    ]
    assert sum(path.endswith("/tags") for _, path, _ in case.requests) == 2
    assert [params["page"] for _, path, params in case.requests if path.endswith("/tags")] == [
        "1",
        "2",
    ]


@pytest.mark.asyncio
async def test_release_tags_follow_repository_id_pagination_path():
    case = ReleaseCase(
        tags=[[tag("v0.6.2", A)], [tag("v0.6.3", D2)]],
        tag_next_paths=[f"/repositories/{RID}/tags"],
    )
    assert [item.name for item in await case.call("release_tags", RID)] == ["v0.6.2", "v0.6.3"]
    assert [(path, params["page"]) for _, path, params in case.requests] == [
        ("/repos/hawkixs/brain-v42/tags", "1"),
        (f"/repositories/{RID}/tags", "2"),
    ]


@pytest.mark.asyncio
async def test_release_tags_reject_other_repository_id_pagination_path():
    case = ReleaseCase(
        tags=[[tag("v0.6.2", A)], [tag("v0.6.3", D2)]],
        tag_next_paths=["/repositories/123/tags"],
    )
    with pytest.raises(ProviderError, match="provider_invalid_response"):
        await case.call("release_tags", RID)


@pytest.mark.asyncio
async def test_release_tags_reject_duplicate_tag_names_across_pages():
    case = ReleaseCase(tags=[[tag("v0.6.2", A)], [tag("v0.6.2", D2)]])
    with pytest.raises(ProviderError, match="provider_invalid_response"):
        await case.call("release_tags", RID)


@pytest.mark.asyncio
async def test_tag_with_bad_sha_is_invalid_response():
    case = ReleaseCase(tags=[[{"name": "v0.6.3", "commit": {"sha": "nope"}}]])
    with pytest.raises(ProviderError, match="provider_invalid_response"):
        await case.call("release_tags", RID)


@pytest.mark.parametrize(
    "status, expected",
    [("behind", True), ("identical", True), ("ahead", False), ("diverged", False)],
)
@pytest.mark.asyncio
async def test_contains_reads_compare_tag_first(status, expected):
    case = ReleaseCase(compare={(D2, M): {"status": status, "behind_by": 0}})
    assert await case.call("contains", RID, M, D2) is expected
    assert case.requests[-1][1].endswith(f"/compare/{D2}...{M}")


@pytest.mark.asyncio
async def test_unknown_compare_status_is_invalid_response():
    case = ReleaseCase(compare={(D2, M): {"status": "weird"}})
    with pytest.raises(ProviderError, match="provider_invalid_response"):
        await case.call("contains", RID, M, D2)


@pytest.mark.asyncio
async def test_commit_date_is_the_committer_date():
    case = ReleaseCase(dates={D2: "2026-10-04T08:00:00Z"})
    assert await case.call("commit_date", RID, D2) == datetime(2026, 10, 4, 8, tzinfo=UTC)


@pytest.mark.asyncio
async def test_commit_date_rejects_timezone_conversion_overflow():
    case = ReleaseCase(dates={D2: "0001-01-01T00:00:00+14:00"})
    with pytest.raises(ProviderError, match="provider_invalid_response"):
        await case.call("commit_date", RID, D2)
