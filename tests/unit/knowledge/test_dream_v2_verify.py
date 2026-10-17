"""The verifier never turns uncheckable output into a decision."""

from datetime import UTC, datetime, timedelta

import pytest

from brain_v42.knowledge.checks import Sources
from brain_v42.knowledge.dream_v2_verify import (
    Candidate,
    Evidence,
    Verdict,
    check_permission,
    child_environment,
    confined_input,
    eligible_candidates,
    load_api_key,
    recheck_evidence,
    stage_snapshot,
    verify_entry,
)


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    (root / "module.py").write_text("def example():\n    return 42\n")
    (root / "escape").symlink_to(tmp_path)
    (root / ".env").write_text("fake credential")
    (root / "anthropic.env").write_text("fake credential")
    (root / ".secrets").mkdir()
    (root / ".secrets/key").write_text("fake credential")
    return root


@pytest.mark.parametrize(
    "tool,params",
    [
        ("Bash", {"command": "cat /proc/self/environ"}),
        ("Edit", {"file_path": "module.py"}),
        ("Read", {"file_path": "../outside"}),
        ("Read", {"file_path": "/proc/self/environ"}),
        ("Read", {"file_path": "escape/module.py"}),
        ("Read", {"file_path": ".env"}),
        ("Read", {"file_path": "anthropic.env"}),
        ("Read", {"file_path": ".secrets/key"}),
        ("Read", {"file_path": "CLAUDE.md"}),
        ("Grep", {"pattern": "key", "path": ".."}),
        ("Glob", {"pattern": "../../**/*"}),
        ("Glob", {"pattern": "/**/*"}),
        ("Grep", {"pattern": "x", "path": ".", "paths": ["/proc"]}),
        ("WebFetch", {"url": "https://example.invalid"}),
        ("Agent", {"prompt": "read secrets"}),
    ],
)
def test_tool_boundary_refuses_escapes_and_injection(snapshot, tool, params):
    assert not check_permission(snapshot, tool, params)


@pytest.mark.parametrize(
    "tool,params",
    [
        ("Read", {"file_path": "module.py"}),
        ("Grep", {"pattern": "example", "path": ".", "glob": "*.py"}),
        ("Glob", {"pattern": "**/*.py", "path": "."}),
    ],
)
def test_read_tools_inside_snapshot(snapshot, tool, params):
    assert check_permission(snapshot, tool, params)


@pytest.mark.parametrize(
    "tool,params,key",
    [
        ("Read", {"file_path": "module.py"}, "file_path"),
        ("Grep", {"pattern": "example"}, "path"),
        ("Glob", {"pattern": "**/*.py", "path": "."}, "path"),
    ],
)
def test_relative_paths_are_rewritten_before_runtime_execution(snapshot, tool, params, key):
    rewritten = confined_input(snapshot, tool, params)
    assert rewritten[key] == str(snapshot / "module.py" if tool == "Read" else snapshot)
    assert params.get(key) != rewritten[key]


def test_environment_is_an_allowlist(snapshot, tmp_path, monkeypatch):
    for key in [
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "BRAIN_TOKEN",
        "POSTGRES_URL",
        "HTTPS_PROXY",
        "GITHUB_TOKEN",
        "NODE_OPTIONS",
        "ANTHROPIC_BASE_URL",
    ]:
        monkeypatch.setenv(key, "must-not-survive")
    env = child_environment(tmp_path, "http://127.0.0.1:1234", "fake-only")
    assert env["ANTHROPIC_API_KEY"] == "fake-only"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1234"
    assert not any(value == "must-not-survive" for value in env.values())
    assert env["HOME"] == str(tmp_path)


def test_key_loader_parses_only_assignment_without_shell(tmp_path):
    path = tmp_path / "fake.env"
    path.write_text("ANTHROPIC_API_KEY='fake-key'\n")
    path.chmod(0o600)
    assert load_api_key(path) == "fake-key"
    path.write_text("ANTHROPIC_API_KEY=$(touch should-not-exist)\n")
    with pytest.raises(ValueError, match="api_key_unavailable"):
        load_api_key(path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="api_key_unavailable"):
        load_api_key(path)


def sources():
    return Sources(
        frozenset({"module.py"}),
        frozenset({"example"}),
        {7: ("merged", None)},
        {"deadbeef-1111": ("resolved", None)},
        {},
        {},
        "a" * 40,
    )


@pytest.mark.parametrize(
    "evidence",
    [
        Evidence(assertion_key="a", kind="file", path="module.py", quote="return   42"),
        Evidence(assertion_key="a", kind="symbol", identifier="example"),
        Evidence(assertion_key="a", kind="pr", number=7, state="merged"),
        Evidence(assertion_key="a", kind="ticket", prefix="deadbeef", state="resolved"),
    ],
)
def test_each_evidence_type_is_rechecked(snapshot, evidence):
    checked = recheck_evidence(
        Verdict(verdict="valid", evidence=[evidence]), {"a"}, snapshot, sources()
    )
    assert checked.normalized_verdict == "valid"
    assert all(item.passed for item in checked.checks)


@pytest.mark.parametrize(
    "evidence",
    [
        [],
        [Evidence(assertion_key="missing", kind="symbol", identifier="example")],
        [Evidence(assertion_key="a", kind="file", path="module.py", quote="not there")],
        [Evidence(assertion_key="a", kind="symbol", identifier="missing")],
        [Evidence(assertion_key="a", kind="pr", number=7, state="open")],
        [Evidence(assertion_key="a", kind="ticket", prefix="deadbeef", state="open")],
        [Evidence(assertion_key="a", kind="file", path="escape/x", quote="x")],
    ],
)
def test_invalid_evidence_keeps_raw_and_normalizes(snapshot, evidence):
    checked = recheck_evidence(
        Verdict(verdict="refuted", evidence=evidence), {"a"}, snapshot, sources()
    )
    assert checked.raw_verdict == "refuted"
    assert checked.normalized_verdict == "undecided"
    assert checked.failures


def test_ambiguous_ticket_prefix_is_not_evidence(snapshot):
    source = sources()
    source.tickets["deadbeef-2222"] = ("resolved", None)
    raw = Verdict(
        verdict="obsolete",
        evidence=[Evidence(assertion_key="a", kind="ticket", prefix="deadbeef", state="resolved")],
    )
    assert recheck_evidence(raw, {"a"}, snapshot, source).normalized_verdict == "undecided"


def test_candidates_use_latest_complete_check_and_recent_digest_history():
    now = datetime(2026, 10, 17, tzinfo=UTC)
    candidates = [
        Candidate(
            entity_id=str(i),
            entry_digest="d",
            evaluated_at=now,
            complete=True,
            state="contested",
            reason=reason,
            reads=i,
        )
        for i, reason in enumerate(
            [
                "contested_by_code",
                "contested_by_config",
                "tied",
                "contested_by_decision",
                "propagated_from_x",
            ]
        )
    ]
    assert [x.entity_id for x in eligible_candidates(candidates, [], now)] == ["2", "1", "0"]
    assert [
        x.entity_id
        for x in eligible_candidates(candidates, [("2", "d", now - timedelta(days=2))], now)
    ] == ["1", "0"]
    newer = Candidate(
        entity_id="0",
        entry_digest="d",
        evaluated_at=now + timedelta(seconds=1),
        complete=True,
        state="valid",
        reason="core_checked",
    )
    assert "0" not in [x.entity_id for x in eligible_candidates([*candidates, newer], [], now)]


def test_candidates_maximum_and_thirty_day_boundary():
    now = datetime(2026, 10, 17, tzinfo=UTC)
    candidates = [
        Candidate(
            entity_id=str(i),
            entry_digest="d",
            evaluated_at=now,
            complete=True,
            state="contested",
            reason="tied",
            reads=i,
        )
        for i in range(20)
    ]
    assert len(eligible_candidates(candidates, [], now)) == 10
    assert eligible_candidates(candidates, [], now)[0].entity_id == "19"
    assert "19" not in [
        c.entity_id
        for c in eligible_candidates(candidates, [("19", "d", now - timedelta(days=30))], now)
    ]


def test_recursive_tools_only_receive_sanitized_pinned_files(snapshot, tmp_path):
    destination = tmp_path / "staged"
    stage_snapshot(
        snapshot, destination, frozenset({"module.py", ".env", "escape/.env", "CLAUDE.md"})
    )
    assert sorted(p.name for p in destination.iterdir()) == ["module.py"]
    assert (destination / "module.py").read_text() == (snapshot / "module.py").read_text()
    assert not (destination / "module.py").stat().st_mode & 0o222


@pytest.mark.parametrize(
    "entry,policy,reason",
    [
        (
            "x" * 7000 + " api_key=not-a-real-secret",
            {"BRAIN_DREAM_V2_VERIFY_ENABLED": "true", "BRAIN_DREAM_V2_VERIFY_DRY_RUN": "false"},
            "screened",
        ),
        ("safe", {}, "disarmed"),
        (
            "safe",
            {"BRAIN_DREAM_V2_VERIFY_ENABLED": "true", "BRAIN_DREAM_V2_VERIFY_DRY_RUN": "true"},
            "disarmed",
        ),
    ],
)
async def test_screen_and_dry_switches_make_zero_sdk_calls(snapshot, entry, policy, reason):
    from unittest.mock import AsyncMock, Mock

    gate = Mock(
        _started=False,
        stop_reason=None,
        cost=0,
        messages=0,
        tokens=0,
        largest_message=0,
        records=[],
    )
    gate.caps.run_usd = 1
    gate.policy_reader = lambda: policy
    gate.start = AsyncMock()
    result = await verify_entry(
        snapshot_root=snapshot,
        entry=entry,
        assertions=[],
        observations=[],
        sources=sources(),
        credential_path=snapshot / "never-opened",
        gate=gate,
    )
    assert result.stop_reason == reason
    gate.start.assert_not_awaited()


async def test_unproven_sdk_acceptance_blocks_before_loading_credential(snapshot):
    from unittest.mock import Mock

    gate = Mock(
        _started=False,
        stop_reason=None,
        cost=0,
        messages=0,
        tokens=0,
        largest_message=0,
        records=[],
    )
    gate.caps.run_usd = 1
    gate.policy_reader = lambda: {
        "BRAIN_DREAM_V2_VERIFY_ENABLED": "true",
        "BRAIN_DREAM_V2_VERIFY_DRY_RUN": "false",
    }
    result = await verify_entry(
        snapshot_root=snapshot,
        entry="safe",
        assertions=[],
        observations=[],
        sources=sources(),
        credential_path=snapshot / "never-opened",
        gate=gate,
        prices_verified=True,
    )
    assert result.stop_reason == "sdk_acceptance_unverified"


@pytest.mark.parametrize(
    "envelope,reason,raw_verdict",
    [
        ({"stop_reason": "sdk_runtime_blocker"}, "sdk_runtime_blocker", None),
        ({"raw_json": "{invalid"}, "malformed_output", None),
        ({"raw_json": '{"verdict":"refuted","evidence":[]}'}, None, "refuted"),
    ],
)
async def test_worker_failures_and_raw_verdicts_survive_parent_normalization(
    snapshot, tmp_path, monkeypatch, envelope, reason, raw_verdict
):
    import asyncio
    import json
    from decimal import Decimal
    from unittest.mock import AsyncMock, Mock

    import httpx

    from brain_v42.knowledge.dream_v2_verify_transport import Gate

    class SocketlessGate(Gate):
        async def __aenter__(self):
            await self.start()
            self.endpoint = "http://127.0.0.1:1234"
            return self

        async def __aexit__(self, *args):
            await self.finish()

    policy = {"BRAIN_DREAM_V2_VERIFY_ENABLED": "true", "BRAIN_DREAM_V2_VERIFY_DRY_RUN": "false"}
    gate = SocketlessGate(
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: None)),
        open_attempt=AsyncMock(),
        reserve=AsyncMock(),
        settle=AsyncMock(),
        totals=AsyncMock(
            return_value=dict.fromkeys(
                ("p3", "night", "d1", "credit", "month", "unresolved"), Decimal(0)
            )
        ),
        policy_reader=lambda: policy,
        night_policy=policy,
    )
    credential = tmp_path / "fake.env"
    credential.write_text("ANTHROPIC_API_KEY=fake-test-credential\n")
    credential.chmod(0o600)
    process = Mock(
        returncode=0, communicate=AsyncMock(return_value=(json.dumps(envelope).encode(), b""))
    )
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await verify_entry(
        snapshot_root=snapshot,
        entry="safe",
        assertions=[{"assertion_key": "a"}],
        observations=[],
        sources=sources(),
        credential_path=credential,
        gate=gate,
        prices_verified=True,
        sdk_acceptance_verified=True,
    )
    assert result.stop_reason == reason
    assert result.raw_verdict == raw_verdict
    assert result.normalized_verdict == "undecided"
    assert "fake-test-credential" not in str(spawn.call_args.args)
    assert spawn.call_args.kwargs["env"]["ANTHROPIC_API_KEY"] == gate.secret
