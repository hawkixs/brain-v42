"""Credential verification uses repository doubles and two virtual clocks."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from brain_v42.credentials import verifier as verifier_module
from brain_v42.credentials.verifier import CredentialRefused, CredentialVerifier
from brain_v42.repositories.pg_client_credentials import CredentialRow, Disposition


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)
        self.elapsed = 0.0

    def utc(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)
        self.elapsed += seconds


def row(token: str, *, expires_at: datetime | None = None) -> CredentialRow:
    return CredentialRow(
        id=uuid4(),
        client_id="client",
        token_sha256=sha256(token.encode()).digest(),
        families=["read", "write"],
        issuers=["issuer"],
        transition=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_by="operator",
        expires_at=expires_at,
        revoked_at=None,
        revoked_reason=None,
        last_used_at=None,
    )


class Repo:
    def __init__(self) -> None:
        self.rows: list[CredentialRow] = []
        self.loads = 0
        self.lookups = 0
        self.fail = False

    async def active_rows(self, now: datetime) -> list[CredentialRow]:
        self.loads += 1
        if self.fail:
            raise RuntimeError("repository unavailable")
        return [
            item
            for item in self.rows
            if item.revoked_at is None and (item.expires_at is None or item.expires_at > now)
        ]

    async def disposition_by_digest(
        self,
        token_sha256: bytes,
        now: datetime,
    ) -> tuple[str | None, Disposition]:
        self.lookups += 1
        if self.fail:
            raise RuntimeError("repository unavailable")
        for item in self.rows:
            if item.token_sha256 == token_sha256:
                if item.revoked_at is not None:
                    return item.client_id, "revoked"
                if item.expires_at is not None and item.expires_at <= now:
                    return item.client_id, "expired"
                return item.client_id, "active"
        return None, "unknown"


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def repo() -> Repo:
    return Repo()


def verifier(repo: Repo, clock: Clock) -> CredentialVerifier:
    return CredentialVerifier(repo, clock=clock.utc, monotonic=clock.monotonic)


@pytest.fixture(autouse=True)
def isolate_process_lookup_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verifier_module, "_last_lookup_at", None, raising=False)


async def test_valid_credential_returns_immutable_principal(repo: Repo, clock: Clock) -> None:
    token = "tok-" + "v" * 40
    credential = row(token)
    repo.rows.append(credential)
    core = verifier(repo, clock)
    await core.refresh()
    principal = await core.verify(token)
    assert principal.client_id == credential.client_id
    assert principal.credential_id == credential.id
    assert principal.families == frozenset(credential.families)
    assert principal.issuers == frozenset(credential.issuers)
    assert principal.expires_at == credential.expires_at
    assert repo.lookups == 0


async def test_before_first_success_registry_is_unavailable(repo: Repo, clock: Clock) -> None:
    core = verifier(repo, clock)
    with pytest.raises(CredentialRefused) as refused:
        await core.verify("tok-" + "v" * 40)
    assert refused.value.reason == "registry_unavailable"


async def test_missing_token_is_refused(repo: Repo, clock: Clock) -> None:
    core = verifier(repo, clock)
    await core.refresh()
    with pytest.raises(CredentialRefused) as refused:
        await core.verify(None)
    assert refused.value.reason == "missing_token"


async def test_lone_surrogate_is_unknown_without_lookup(repo: Repo, clock: Clock) -> None:
    core = verifier(repo, clock)
    await core.refresh()
    with pytest.raises(CredentialRefused) as refused:
        await core.verify("\ud800")
    assert refused.value.reason == "unknown_token"
    assert repo.lookups == 0


async def test_oversized_token_is_unknown_without_hashing_or_lookup(
    repo: Repo,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core = verifier(repo, clock)
    await core.refresh()
    hashes = 0

    def count_hash(value: bytes) -> object:
        nonlocal hashes
        hashes += 1
        return sha256(value)

    monkeypatch.setattr(verifier_module, "sha256", count_hash)
    with pytest.raises(CredentialRefused) as refused:
        await core.verify("x" * 4097)
    assert refused.value.reason == "unknown_token"
    assert hashes == 0
    assert repo.lookups == 0


async def test_token_at_length_limit_can_authenticate(repo: Repo, clock: Clock) -> None:
    token = "x" * 4096
    repo.rows.append(row(token))
    core = verifier(repo, clock)
    await core.refresh()
    assert (await core.verify(token)).client_id == "client"
    assert repo.lookups == 0


@pytest.mark.parametrize("disposition", ["unknown", "revoked", "expired"])
async def test_misses_have_distinct_dispositions(
    repo: Repo,
    clock: Clock,
    disposition: str,
) -> None:
    token = "tok-" + "r" * 40
    if disposition == "revoked":
        repo.rows.append(replace(row(token), revoked_at=clock.utc(), revoked_reason="ended"))
    elif disposition == "expired":
        repo.rows.append(row(token, expires_at=clock.utc()))
    core = verifier(repo, clock)
    await core.refresh()
    with pytest.raises(CredentialRefused) as refused:
        await core.verify(token)
    assert refused.value.reason == disposition + "_token"
    assert repo.lookups == 1


async def test_expiry_is_checked_between_refreshes(repo: Repo, clock: Clock) -> None:
    token = "tok-" + "e" * 40
    repo.rows.append(row(token, expires_at=clock.utc() + timedelta(seconds=10)))
    core = verifier(repo, clock)
    await core.refresh()
    await core.verify(token)
    clock.advance(10)
    with pytest.raises(CredentialRefused) as refused:
        await core.verify(token)
    assert refused.value.reason == "expired_token"
    assert repo.loads == 1
    assert repo.lookups == 0


async def test_failed_refresh_serves_only_until_last_success_plus_ninety_seconds(
    repo: Repo,
    clock: Clock,
) -> None:
    token = "tok-" + "v" * 40
    repo.rows.append(row(token))
    core = verifier(repo, clock)
    await core.refresh()
    repo.fail = True
    clock.advance(30)
    assert not await core.refresh()
    clock.advance(60)
    assert (await core.verify(token)).client_id == "client"
    clock.advance(0.001)
    with pytest.raises(CredentialRefused) as refused:
        await core.verify(token)
    assert refused.value.reason == "registry_unavailable"
    assert not await core.refresh()
    with pytest.raises(CredentialRefused) as refused:
        await core.verify(token)
    assert refused.value.reason == "registry_unavailable"
    repo.fail = False
    assert await core.refresh()
    assert (await core.verify(token)).client_id == "client"


async def test_initial_refresh_failure_stays_unavailable(repo: Repo, clock: Clock) -> None:
    repo.fail = True
    core = verifier(repo, clock)
    assert not await core.refresh()
    with pytest.raises(CredentialRefused) as refused:
        await core.verify("tok-" + "v" * 40)
    assert refused.value.reason == "registry_unavailable"


async def test_refresh_failure_logs_only_the_exception_type(clock: Clock) -> None:
    message = "tok-" + "x" * 40

    class FailingRepo(Repo):
        async def active_rows(self, now: datetime) -> list[CredentialRow]:
            raise RuntimeError(message)

    core = verifier(FailingRepo(), clock)
    with capture_logs() as logs:
        assert not await core.refresh()
    assert message not in repr(logs)
    assert logs == [
        {
            "event": "credentials.registry_refresh_failed",
            "log_level": "warning",
            "error": "RuntimeError",
        }
    ]


async def test_snapshot_age_includes_time_spent_loading(clock: Clock) -> None:
    token = "tok-" + "v" * 40

    class SlowRepo(Repo):
        async def active_rows(self, now: datetime) -> list[CredentialRow]:
            loaded = await super().active_rows(now)
            clock.advance(30)
            return loaded

    slow = SlowRepo()
    slow.rows.append(row(token))
    core = verifier(slow, clock)
    assert await core.refresh()
    clock.advance(60)
    assert (await core.verify(token)).client_id == "client"
    clock.advance(0.001)
    with pytest.raises(CredentialRefused) as refused:
        await core.verify(token)
    assert refused.value.reason == "registry_unavailable"
    assert slow.lookups == 0


async def test_disposition_failure_is_a_safe_registry_refusal(repo: Repo, clock: Clock) -> None:
    core = verifier(repo, clock)
    await core.refresh()
    repo.fail = True
    with pytest.raises(CredentialRefused) as refused:
        await core.verify("tok-" + "u" * 40)
    assert refused.value.reason == "registry_unavailable"


async def test_active_miss_with_failed_refresh_is_unavailable(repo: Repo, clock: Clock) -> None:
    token = "tok-" + "a" * 40

    class FailingActiveRepo(Repo):
        async def disposition_by_digest(
            self,
            token_sha256: bytes,
            now: datetime,
        ) -> tuple[str | None, Disposition]:
            result = await super().disposition_by_digest(token_sha256, now)
            self.fail = True
            return result

    active_repo = FailingActiveRepo()
    core = verifier(active_repo, clock)
    await core.refresh()
    active_repo.rows.append(row(token))
    with pytest.raises(CredentialRefused) as refused:
        await core.verify(token)
    assert refused.value.reason == "registry_unavailable"


async def test_secrets_are_absent_from_principal_refusal_and_logs(repo: Repo, clock: Clock) -> None:
    token = "tok-" + "v" * 40
    credential = row(token)
    repo.rows.append(credential)
    core = verifier(repo, clock)
    with capture_logs() as logs:
        await core.refresh()
        principal = await core.verify(token)
        with pytest.raises(CredentialRefused) as refused:
            await core.verify("tok-" + "u" * 40)
        repo.fail = True
        assert not await core.refresh()
    rendered = repr((principal, credential, core, logs, refused.value)) + str(refused.value)
    assert token not in rendered
    assert credential.token_sha256.hex() not in rendered
    assert repr(credential.token_sha256) not in rendered
    assert not hasattr(principal, "token_sha256")
    assert not hasattr(principal, "token")


async def test_repository_error_text_is_never_logged_or_chained(repo: Repo, clock: Clock) -> None:
    token = "tok-" + "x" * 40
    digest = sha256(token.encode()).hexdigest()

    class LeakyRepo(Repo):
        async def active_rows(self, now: datetime) -> list[CredentialRow]:
            raise RuntimeError(token + digest)

        async def disposition_by_digest(
            self,
            token_sha256: bytes,
            now: datetime,
        ) -> tuple[str | None, Disposition]:
            raise RuntimeError(token + digest)

    leaky_repo = LeakyRepo()
    core = verifier(leaky_repo, clock)
    with capture_logs() as logs:
        assert not await core.refresh()
        with pytest.raises(CredentialRefused) as refused:
            await core.verify(token)
        # Establish an empty successful snapshot to exercise the lookup failure too.
        core._repository = repo
        await core.refresh()
        core._repository = leaky_repo
        with pytest.raises(CredentialRefused) as lookup_refused:
            await core.verify(token)
    import traceback

    rendered = repr(logs) + "".join(traceback.format_exception(lookup_refused.value))
    rendered += repr(refused.value)
    assert token not in rendered
    assert digest not in rendered


async def test_restart_recovers_revocation_after_one_lookup(repo: Repo, clock: Clock) -> None:
    token = "tok-" + "r" * 40
    repo.rows.append(replace(row(token), revoked_at=clock.utc(), revoked_reason="ended"))
    restarted = verifier(repo, clock)
    await restarted.refresh()
    for _ in range(2):
        with pytest.raises(CredentialRefused) as refused:
            await restarted.verify(token)
        assert refused.value.reason == "revoked_token"
    assert repo.lookups == 1


async def test_unknown_flood_uses_one_lookup_per_five_seconds(repo: Repo, clock: Clock) -> None:
    core = verifier(repo, clock)
    await core.refresh()
    for number in range(100):
        with pytest.raises(CredentialRefused) as refused:
            await core.verify("tok-" + "u" * 40 + str(number))
        assert refused.value.reason == "unknown_token"
    assert repo.lookups == 1
    clock.advance(5)
    with pytest.raises(CredentialRefused):
        await core.verify("tok-" + "u" * 40)
    assert repo.lookups == 2


async def test_lookup_budget_is_shared_across_verifiers(repo: Repo, clock: Clock) -> None:
    first, second = verifier(repo, clock), verifier(repo, clock)
    await first.refresh()
    await second.refresh()
    for core in (first, second):
        with pytest.raises(CredentialRefused):
            await core.verify("tok-" + "u" * 40)
    assert repo.lookups == 1


async def test_active_miss_refreshes_and_retries_once(repo: Repo, clock: Clock) -> None:
    token = "tok-" + "a" * 40
    core = verifier(repo, clock)
    await core.refresh()
    repo.rows.append(row(token))
    assert (await core.verify(token)).client_id == "client"
    assert repo.loads == 2
    assert repo.lookups == 1


@pytest.mark.parametrize("disposition", ["revoked", "expired"])
async def test_cached_terminal_reason_survives_throttled_misses(
    repo: Repo,
    clock: Clock,
    disposition: str,
) -> None:
    token = "tok-" + "r" * 40
    credential = row(token, expires_at=clock.utc())
    if disposition == "revoked":
        credential = replace(credential, revoked_at=clock.utc(), revoked_reason="ended")
    repo.rows.append(credential)
    core = verifier(repo, clock)
    await core.refresh()
    with pytest.raises(CredentialRefused):
        await core.verify(token)
    with pytest.raises(CredentialRefused):
        await core.verify("tok-" + "u" * 40)
    with pytest.raises(CredentialRefused) as refused:
        await core.verify(token)
    assert refused.value.reason == disposition + "_token"
    assert repo.lookups == 1


async def test_notify_refreshes_the_snapshot(repo: Repo, clock: Clock) -> None:
    token = "tok-" + "n" * 40
    core = verifier(repo, clock)
    await core.refresh()
    repo.rows.append(row(token))
    await core.notify()
    assert (await core.verify(token)).client_id == "client"
    assert repo.loads == 2
    assert repo.lookups == 0


async def test_listener_reconnect_forces_a_full_refresh(repo: Repo, clock: Clock) -> None:
    token = "tok-" + "n" * 40
    core = verifier(repo, clock)
    await core.refresh()
    repo.rows.append(row(token))
    await core.run_listener_reconnected()
    assert (await core.verify(token)).client_id == "client"
    assert repo.loads == 2
    assert repo.lookups == 0


async def test_periodic_refresh_covers_a_lost_notify_within_thirty_seconds(
    repo: Repo,
    clock: Clock,
) -> None:
    token = "tok-" + "p" * 40
    waits: list[float] = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) > 1:
            raise asyncio.CancelledError
        repo.rows.append(row(token))
        clock.advance(seconds)

    core = CredentialVerifier(repo, clock=clock.utc, monotonic=clock.monotonic, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await core.run_refresh_loop()
    assert waits == [30.0, 30.0]
    assert repo.loads == 2
    assert (await core.verify(token)).client_id == "client"
    assert repo.lookups == 0


async def test_periodic_loop_recovers_after_a_failed_refresh(repo: Repo, clock: Clock) -> None:
    waits = 0

    async def sleep(seconds: float) -> None:
        nonlocal waits
        waits += 1
        if waits == 3:
            raise asyncio.CancelledError
        clock.advance(seconds)
        repo.fail = waits == 1

    core = CredentialVerifier(repo, clock=clock.utc, monotonic=clock.monotonic, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await core.run_refresh_loop()
    assert repo.loads == 3
    assert core._last_success == 60.0


async def test_snapshot_copies_mutable_permission_lists(repo: Repo, clock: Clock) -> None:
    token = "tok-" + "s" * 40
    credential = row(token)
    repo.rows.append(credential)
    core = verifier(repo, clock)
    await core.refresh()
    credential.families.append("delivery")
    credential.issuers.append("new-issuer")
    principal = await core.verify(token)
    assert principal.families == frozenset({"read", "write"})
    assert principal.issuers == frozenset({"issuer"})


async def test_overlapping_refreshes_cannot_publish_a_stale_snapshot(
    repo: Repo,
    clock: Clock,
) -> None:
    token = "tok-" + "s" * 40
    entered, release = asyncio.Event(), asyncio.Event()

    class DelayedRepo(Repo):
        async def active_rows(self, now: datetime) -> list[CredentialRow]:
            loaded = await super().active_rows(now)
            if self.loads == 1:
                entered.set()
                await release.wait()
            return loaded

    delayed = DelayedRepo()
    delayed.rows.append(row(token))
    core = verifier(delayed, clock)
    first = asyncio.create_task(core.refresh())
    await entered.wait()
    delayed.rows.clear()
    second = asyncio.create_task(core.refresh())
    # Let the second refresh contend while the first one is still loading.
    progressed = asyncio.Event()
    asyncio.get_running_loop().call_soon(progressed.set)
    await progressed.wait()
    release.set()
    await asyncio.gather(first, second)
    with pytest.raises(CredentialRefused) as refused:
        await core.verify(token)
    assert refused.value.reason == "unknown_token"


def test_refusal_exception_rejects_unrecognised_secret_input() -> None:
    token = "tok-" + "x" * 40
    with pytest.raises(ValueError) as refused:
        CredentialRefused(token)
    assert token not in str(refused.value)


async def test_terminal_disposition_cache_is_bounded_and_lru(repo: Repo, clock: Clock) -> None:
    tokens = ["tok-" + "r" * 40 + str(number) for number in range(1025)]
    repo.rows = [
        replace(row(token), revoked_at=clock.utc(), revoked_reason="ended") for token in tokens
    ]
    core = verifier(repo, clock)
    for token in tokens[:1024]:
        await core.refresh()
        with pytest.raises(CredentialRefused) as refused:
            await core.verify(token)
        assert refused.value.reason == "revoked_token"
        clock.advance(5)
    # Touch the oldest entry, then insert one more: the second-oldest is evicted.
    await core.refresh()
    with pytest.raises(CredentialRefused):
        await core.verify(tokens[0])
    with pytest.raises(CredentialRefused):
        await core.verify(tokens[-1])
    assert len(core._dispositions) == 1024
    with pytest.raises(CredentialRefused) as cached:
        await core.verify(tokens[0])
    assert cached.value.reason == "revoked_token"
    with pytest.raises(CredentialRefused) as evicted:
        await core.verify(tokens[1])
    assert evicted.value.reason == "unknown_token"
    assert repo.lookups == 1025


async def test_active_retry_is_bounded_even_if_row_disappears(repo: Repo, clock: Clock) -> None:
    class VanishingRepo(Repo):
        async def disposition_by_digest(
            self,
            token_sha256: bytes,
            now: datetime,
        ) -> tuple[str | None, Disposition]:
            self.lookups += 1
            return "client", "active"

    vanishing = VanishingRepo()
    core = verifier(vanishing, clock)
    await core.refresh()
    with pytest.raises(CredentialRefused) as refused:
        await core.verify("tok-" + "a" * 40)
    assert refused.value.reason == "unknown_token"
    assert vanishing.loads == 2
    assert vanishing.lookups == 1


async def test_reactivated_expired_row_discards_its_obsolete_disposition(
    repo: Repo,
    clock: Clock,
) -> None:
    token = "tok-" + "e" * 40
    expired = row(token, expires_at=clock.utc())
    repo.rows.append(expired)
    core = verifier(repo, clock)
    await core.refresh()
    with pytest.raises(CredentialRefused) as refused:
        await core.verify(token)
    assert refused.value.reason == "expired_token"
    renewed = replace(expired, expires_at=clock.utc() + timedelta(hours=1))
    repo.rows = [renewed]
    await core.notify()
    assert (await core.verify(token)).client_id == "client"
    repo.rows = [replace(renewed, revoked_at=clock.utc(), revoked_reason="ended")]
    clock.advance(5)
    await core.notify()
    with pytest.raises(CredentialRefused) as revoked:
        await core.verify(token)
    assert revoked.value.reason == "revoked_token"
