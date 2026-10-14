"""Identity refusals cannot accept a declaration measured from the target."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from brain_v42.config import Settings
from brain_v42.facts.model import SourceIdentity
from brain_v42.knowledge import extraction
from brain_v42.lifecycle_cli.adapters import IdentityGates, SDKExtraction


@pytest.mark.parametrize(
    ("declared", "observed", "reason"),
    [
        (None, None, "target_undeclared"),
        (
            {"system_identifier": "123", "database": "declared", "server_port": 5432},
            SourceIdentity("123", "other", "127.0.0.1", 5432),
            "target_mismatch",
        ),
        (
            {"system_identifier": "123", "database": "declared", "server_port": 5432},
            SourceIdentity("123", "declared", "127.0.0.1", 5432),
            None,
        ),
    ],
)
async def test_lifecycle_identity_gate(
    declared: object, observed: object, reason: str | None
) -> None:
    import json

    settings = Settings(
        postgres_url="postgresql+asyncpg://unused/unused",
        _env_file=None,
        brain_knowledge_lifecycle_enabled=True,
        facts_production_identity_json=json.dumps(declared) if declared else None,
    )
    measure = AsyncMock(return_value=observed)
    gates = IdentityGates(settings, measure)
    assert await gates.check("dry_run") == reason
    if declared is None:
        measure.assert_not_awaited()


async def test_lifecycle_unreadable_identity_refuses_without_exception_text() -> None:
    settings = Settings(
        postgres_url="postgresql+asyncpg://unused/unused",
        _env_file=None,
        brain_knowledge_lifecycle_enabled=True,
        facts_production_identity_json='{"system_identifier":"123","database":"declared","server_port":5432}',
    )
    assert (
        await IdentityGates(settings, AsyncMock(side_effect=RuntimeError("private details"))).check(
            "apply"
        )
        == "lifecycle_dry_run_only"
    )
    settings.brain_knowledge_lifecycle_dry_run = False
    assert (
        await IdentityGates(settings, AsyncMock(side_effect=RuntimeError("private details"))).check(
            "apply"
        )
        == "identity_unreadable"
    )


async def test_lifecycle_empty_manifest_never_initializes_sdk() -> None:
    factory = Mock(side_effect=AssertionError("SDK must remain lazy"))
    adapter = SDKExtraction(factory)
    manifest = extraction.Manifest("r", (), ())
    counted = await adapter.count(manifest)
    assert counted.worst_case_usd == 0
    assert counted.manifest_digest == extraction.manifest_digest(manifest)
    await adapter.aclose()
    factory.assert_not_called()


async def test_lifecycle_sdk_adapter_uses_injected_client() -> None:
    request = extraction.BatchRequest(
        "r-00000",
        (),
        {"model": "fake", "system": [], "messages": [], "output_config": {}, "max_tokens": 1},
        {},
    )
    manifest = extraction.Manifest("r", (request,), ())
    sdk = SimpleNamespace(
        messages=SimpleNamespace(
            count_tokens=Mock(return_value=SimpleNamespace(input_tokens=100)),
            batches=SimpleNamespace(create=Mock(return_value=SimpleNamespace(id="batch"))),
        ),
        close=Mock(),
    )
    factory = Mock(return_value=sdk)
    adapter = SDKExtraction(factory)
    assert (await adapter.count(manifest)).input_tokens == {"r-00000": 100}
    assert await adapter.submit(manifest) == "batch"
    await adapter.aclose()
    factory.assert_called_once_with()
    sdk.close.assert_called_once_with()


async def test_lifecycle_sdk_collect_waits_until_batch_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import brain_v42.lifecycle_cli.adapters as module

    request = extraction.BatchRequest("r-00000", (), {}, {})
    manifest = extraction.Manifest("r", (request,), ())
    batches = SimpleNamespace(
        retrieve=Mock(
            side_effect=[
                SimpleNamespace(processing_status="in_progress"),
                SimpleNamespace(processing_status="ended"),
                SimpleNamespace(processing_status="ended"),
            ]
        ),
        results=Mock(return_value=[]),
    )
    sdk = SimpleNamespace(messages=SimpleNamespace(batches=batches), close=Mock())
    pause = AsyncMock()
    monkeypatch.setattr(module.asyncio, "sleep", pause)
    adapter = SDKExtraction(Mock(return_value=sdk))
    results = await adapter.collect("batch", manifest)
    assert results.results["r-00000"].status == extraction.ResultStatus.MISSING
    pause.assert_awaited_once_with(30)


def test_lifecycle_github_credential_is_sent_only_to_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from brain_v42.lifecycle_cli.__main__ import GitHubAuthentication

    monkeypatch.setenv("LIFECYCLE_TEST_GITHUB_CREDENTIAL", "fake-test-value")
    auth = GitHubAuthentication("LIFECYCLE_TEST_GITHUB_CREDENTIAL")
    rest = next(
        auth.auth_flow(httpx.Request("GET", "https://api.github.com/repos/hawkixs/brain-v42/pulls"))
    )
    archive = next(
        auth.auth_flow(
            httpx.Request("GET", "https://codeload.github.com/hawkixs/brain-v42/tar.gz/main")
        )
    )
    assert rest.headers["Authorization"] == "Bearer fake-test-value"
    assert "Authorization" not in archive.headers


@pytest.mark.parametrize(
    "variable", ["", "1TOKEN", "TOKEN-NAME", "TOKEN VALUE", "TOKEN\n", "TOKEN=value", "TOKEN.NAME"]
)
def test_lifecycle_github_authentication_rejects_invalid_variable_names(variable: str) -> None:
    from brain_v42.lifecycle_cli.__main__ import GitHubAuthentication

    with pytest.raises(ValueError, match="^invalid_github_token_variable_name$"):
        GitHubAuthentication(variable)


@pytest.mark.parametrize("variable", [None, "TOKEN", "_TOKEN", "token9"])
def test_lifecycle_github_authentication_accepts_variable_names(variable: str | None) -> None:
    from brain_v42.lifecycle_cli.__main__ import GitHubAuthentication

    assert GitHubAuthentication(variable).variable == variable
