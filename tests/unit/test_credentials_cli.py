"""Exercise operator gestures without needing PostgreSQL or exposing secrets."""

from __future__ import annotations

import hashlib
import io
import json
import tomllib
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from brain_v42.repositories.pg_client_credentials import (
    ClientCredentialError,
    CredentialRow,
    ElevationRow,
    PgClientCredentialRepo,
)

NOW = datetime(2026, 10, 6, 12, tzinfo=UTC)
SESSION_ID = uuid4()
CONNECTION = "allowed-connection-private-suffix"
EXCLUDED = "excluded-connection-private-suffix"


class FakeTTY(io.StringIO):
    """Keep terminal output separate from the operator's input."""

    def __init__(self, answer: str) -> None:
        super().__init__(answer)
        self.prompt = ""

    def write(self, value: str) -> int:
        self.prompt += value
        return len(value)


def credential(**changes: Any) -> CredentialRow:
    return replace(
        CredentialRow(
            uuid4(),
            "workstation-claude",
            b"x" * 32,
            ["read"],
            [],
            False,
            NOW,
            "operator",
            None,
            None,
            None,
            None,
        ),
        **changes,
    )


def elevation(**changes: Any) -> ElevationRow:
    return replace(
        ElevationRow(
            uuid4(),
            SESSION_ID,
            [CONNECTION],
            ["workstation-claude"],
            NOW,
            NOW + timedelta(hours=1),
            "operator",
            "maintenance",
            "cli",
            None,
            ["red-rail"],
            1,
            None,
            None,
        ),
        **changes,
    )


class FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def mappings(self) -> FakeResult:
        return self

    def one_or_none(self) -> Any:
        return self.rows[0] if self.rows else None

    def one(self) -> Any:
        return self.rows[0]

    def all(self) -> list[Any]:
        return self.rows


class FakeSession:
    def __init__(self) -> None:
        self.owner = {"status": "open", "nature": None}
        self.links = [
            {"connection_id": CONNECTION, "client_id": "workstation-claude", "first_seen_at": NOW},
            {"connection_id": EXCLUDED, "client_id": "red-rail", "first_seen_at": NOW},
            {"connection_id": "legacy-connection-private", "client_id": None, "first_seen_at": NOW},
        ]

    async def execute(self, statement: Any) -> FakeResult:
        if "FROM brain_sessions" in str(statement):
            return FakeResult([self.owner] if self.owner else [])
        return FakeResult([row.copy() for row in self.links])


class FakeRepo:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.rows = [credential(), credential(revoked_at=NOW, revoked_reason="rotated")]
        self.grants = [elevation()]
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.audit: list[dict[str, Any]] = []
        self.committed = False
        self.error: Exception | None = None

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield self.session
        if self.error:
            raise self.error
        self.committed = True

    async def issue(self, **kwargs: Any) -> CredentialRow:
        assert kwargs["session"] is self.session
        self.calls.append(("issue", kwargs))
        self.audit.append({"event": "credentials.issued", "reason": kwargs["reason"]})
        return credential(client_id=kwargs["client_id"], families=kwargs["families"])

    async def revoke(
        self, credential_id: UUID, reason: str, now: datetime, **kwargs: Any
    ) -> CredentialRow:
        self.calls.append(("revoke", {"id": credential_id, "reason": reason, **kwargs}))
        return credential(revoked_at=now)

    async def list_rows(self) -> list[CredentialRow]:
        return self.rows

    async def grant_elevation(self, **kwargs: Any) -> ElevationRow:
        self.calls.append(("elevate", kwargs))
        allowed = kwargs["elevatable_client_ids"]
        links = [
            row
            for row in self.session.links
            if row["connection_id"] in kwargs["connection_ids"] and row["client_id"]
        ]
        frozen = [row for row in links if row["client_id"] in allowed]
        excluded = [row for row in links if row["client_id"] not in allowed]
        if not frozen:
            raise ClientCredentialError("no_elevatable_connection", "no allowlisted pair")
        granted = elevation(
            connection_ids=[row["connection_id"] for row in frozen],
            connection_client_ids=[row["client_id"] for row in frozen],
            excluded_client_ids=sorted({row["client_id"] for row in excluded}),
            excluded_connection_count=len(excluded),
        )
        self.grants = [granted]
        return granted

    async def end_elevation(self, elevation_id: UUID, now: datetime, **kwargs: Any) -> ElevationRow:
        self.calls.append(("unelevate", {"id": elevation_id, **kwargs}))
        return elevation(revoked_at=now)

    async def active_elevations(self, now: datetime) -> list[ElevationRow]:
        return self.grants


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> Any:
    from brain_v42.scripts import credentials_cli

    repo = FakeRepo()
    monkeypatch.setattr(credentials_cli, "PgClientCredentialRepo", lambda factory: repo)
    monkeypatch.setattr(credentials_cli, "get_session_factory", lambda: object())
    monkeypatch.setattr(
        credentials_cli,
        "get_settings",
        lambda: SimpleNamespace(elevatable_client_ids={"workstation-claude"}),
    )

    async def dispose() -> None:
        pass

    monkeypatch.setattr(credentials_cli, "dispose_engine", dispose)
    return credentials_cli, repo


def test_issue_prints_only_one_token_after_commit(
    cli: Any, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module, repo = cli

    def generate(size: int) -> str:
        assert size == 32
        return "unit-test-secret-material"

    monkeypatch.setattr(module.secrets, "token_urlsafe", generate)
    assert (
        module.main(
            [
                "issue",
                "--client-id",
                "workstation-claude",
                "--families",
                "read,write",
                "--reason",
                "setup",
            ]
        )
        == 0
    )
    output = capsys.readouterr()
    token = "bk1_unit-test-secret-material"
    assert output.out == token + "\n"
    assert token not in output.err
    assert token not in json.dumps(repo.audit)
    assert repo.calls[0][1]["token_sha256"] == hashlib.sha256(token.encode()).digest()
    assert repo.calls[0][1]["reason"] == "setup"
    assert repo.committed


@pytest.mark.parametrize(
    "families", ["admin", "elevate,read", "read,elevate", "unknown", "", "read,"]
)
def test_invalid_families_are_refused_before_writes(
    cli: Any, capsys: pytest.CaptureFixture[str], families: str
) -> None:
    module, repo = cli
    assert (
        module.main(
            [
                "issue",
                "--client-id",
                "workstation-claude",
                "--families",
                families,
                "--reason",
                "setup",
            ]
        )
        == 2
    )
    assert repo.calls == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("expires", ["1h", "2027-01-01T00:00:00Z"])
def test_issue_parses_expiry(cli: Any, expires: str) -> None:
    module, repo = cli
    assert (
        module.main(
            [
                "issue",
                "--client-id",
                "red-rail",
                "--families",
                "delivery",
                "--expires",
                expires,
                "--reason",
                "setup",
            ]
        )
        == 0
    )
    assert repo.calls[0][1]["expires_at"].tzinfo is not None


@pytest.mark.parametrize(
    "arguments",
    [
        ["issue", "--client-id", "Invalid", "--families", "read", "--reason", "setup"],
        [
            "issue",
            "--client-id",
            "red-rail",
            "--families",
            "read",
            "--expires",
            "yesterday",
            "--reason",
            "setup",
        ],
        [
            "issue",
            "--client-id",
            "red-rail",
            "--families",
            "read",
            "--expires",
            "2027-01-01",
            "--reason",
            "setup",
        ],
        ["issue", "--client-id", "red-rail", "--families", "read", "--reason", " "],
        ["elevate", str(SESSION_ID), "--ttl", "1h", "--reason", "x" * 201, "--yes"],
        ["revoke", "invalid-id", "--reason", "rotated"],
    ],
)
def test_invalid_arguments_are_bounded_refusals(
    cli: Any, arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    assert module.main(arguments) == 2
    assert repo.calls == []
    assert capsys.readouterr().out == ""


def test_list_json_has_exact_public_fields_and_includes_revoked(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    assert module.main(["list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert all(
        set(row) == {"id", "client_id", "families", "transition", "expires_at", "revoked_at"}
        for row in rows
    )
    assert rows[1]["revoked_at"] == NOW.isoformat()
    assert repo.audit == []
    assert repo.calls == []


@pytest.mark.parametrize(
    ("owner", "reason"),
    [
        ({"status": "open", "nature": "agent"}, "session_not_operator"),
        ({"status": "closed", "nature": None}, "session_not_open"),
        ({}, "unknown_session"),
    ],
)
def test_elevate_refuses_invalid_session(
    cli: Any, owner: dict[str, Any], reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    repo.session.owner = owner
    assert (
        module.main(["elevate", str(SESSION_ID), "--ttl", "1h", "--reason", "maintenance", "--yes"])
        == 2
    )
    output = capsys.readouterr()
    assert reason in output.err
    assert output.out == ""
    assert repo.calls == []


def test_elevate_accepts_non_agent_non_null_session_nature(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    repo.session.owner = {"status": "open", "nature": "operator"}
    assert (
        module.main(["elevate", str(SESSION_ID), "--ttl", "1h", "--reason", "maintenance", "--yes"])
        == 0
    )
    output = capsys.readouterr()
    assert str(repo.grants[0].id) in output.out
    assert "session_not_operator" not in output.err
    assert repo.calls[0][0] == "elevate"
    assert repo.calls[0][1]["session_id"] == SESSION_ID
    assert repo.committed


@pytest.mark.parametrize("ttl", ["4h1s", "5h", "0s", "-1h", "bad"])
def test_elevate_refuses_invalid_window(
    cli: Any, ttl: str, capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    assert (
        module.main(
            ["elevate", str(SESSION_ID), "--ttl=" + ttl, "--reason", "maintenance", "--yes"]
        )
        == 2
    )
    assert "invalid_window" in capsys.readouterr().err
    assert repo.calls == []


@pytest.mark.parametrize(
    "links", [[], [{"connection_id": CONNECTION, "client_id": None, "first_seen_at": NOW}]]
)
def test_elevate_refuses_without_attribution(
    cli: Any, links: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    repo.session.links = links
    assert (
        module.main(["elevate", str(SESSION_ID), "--ttl", "1h", "--reason", "maintenance", "--yes"])
        == 2
    )
    assert "no_attributed_connection" in capsys.readouterr().err
    assert repo.calls == []


def test_elevate_refuses_without_allowlisted_pair(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    repo.session.links = [repo.session.links[1]]
    assert (
        module.main(["elevate", str(SESSION_ID), "--ttl", "1h", "--reason", "maintenance", "--yes"])
        == 2
    )
    assert "no_elevatable_connection" in capsys.readouterr().err
    assert repo.calls == []


def test_elevate_confirms_frozen_allowlisted_pairs(
    cli: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    tty = FakeTTY("yes\n")
    monkeypatch.setattr(tty, "isatty", lambda: True)
    monkeypatch.setattr(module, "_open_tty", lambda: tty)
    assert module.main(["elevate", str(SESSION_ID), "--ttl", "4h", "--reason", "maintenance"]) == 0
    output = capsys.readouterr()
    assert CONNECTION not in output.out + output.err
    assert EXCLUDED not in output.out + output.err
    assert "allowed-…" in output.err and "excluded…" in output.err
    assert "red-rail" in output.err and "excluded" in output.err
    assert NOW.isoformat() in output.err
    assert repo.grants[0].connection_ids == [CONNECTION]
    assert repo.grants[0].excluded_client_ids == ["red-rail"]
    assert repo.grants[0].excluded_connection_count == 1
    assert repo.calls[0][1]["via"] == "cli"
    assert repo.calls[0][1]["requested_by_client_id"] is None
    assert repo.calls[0][1]["session"] is repo.session
    assert str(repo.grants[0].id) in output.out


@pytest.mark.parametrize("answer", ["no\n", ""])
def test_declined_confirmation_writes_nothing(
    cli: Any, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    module, repo = cli
    tty = FakeTTY(answer)
    monkeypatch.setattr(tty, "isatty", lambda: True)
    monkeypatch.setattr(module, "_open_tty", lambda: tty)
    assert module.main(["elevate", str(SESSION_ID), "--ttl", "1h", "--reason", "maintenance"]) == 2
    assert repo.calls == []


def test_missing_tty_refuses_without_writes(cli: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    module, repo = cli

    def unavailable() -> Any:
        raise OSError("no TTY")

    monkeypatch.setattr(module, "_open_tty", unavailable)
    assert module.main(["elevate", str(SESSION_ID), "--ttl", "1h", "--reason", "maintenance"]) == 2
    assert repo.calls == []


@pytest.mark.parametrize("command", ["revoke", "unelevate"])
def test_end_gestures_use_repository_transaction(cli: Any, command: str) -> None:
    module, repo = cli
    identifier = uuid4()
    assert module.main([command, str(identifier), "--reason", "rotated"]) == 0
    assert repo.calls[0][0] == command
    assert repo.calls[0][1]["id"] == identifier
    assert repo.calls[0][1]["session"] is repo.session
    assert repo.calls[0][1]["reason"] == "rotated"
    assert repo.committed


@pytest.mark.parametrize("json_mode", [True, False])
def test_elevations_never_expose_connection_ids(
    cli: Any, json_mode: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    assert module.main(["elevations"] + (["--json"] if json_mode else [])) == 0
    output = capsys.readouterr().out
    assert CONNECTION not in output
    assert "connection_ids" not in output
    assert str(repo.grants[0].id) in output
    assert repo.audit == []


def test_plain_list_is_an_aligned_public_table(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    assert module.main(["list"]) == 0
    output = capsys.readouterr().out
    assert "client_id" in output and "revoked_at" in output
    assert "token_sha256" not in output
    assert str(repo.rows[1].id) in output


def test_commit_failure_never_prints_token_or_database_parameters(
    cli: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    repo.error = RuntimeError("private SQL parameters and digest")
    assert (
        module.main(["issue", "--client-id", "red-rail", "--families", "read", "--reason", "setup"])
        == 2
    )
    output = capsys.readouterr()
    assert output.out == ""
    assert "private SQL" not in output.err
    assert "RuntimeError" in output.err


async def test_repository_issue_keeps_reason_in_the_same_audit_transaction() -> None:
    row = credential()
    writes: list[Any] = []

    class Session:
        async def execute(self, statement: Any) -> FakeResult:
            writes.append(statement.compile().params)
            return FakeResult(
                [
                    vars(
                        SimpleNamespace(
                            **{field: getattr(row, field) for field in row.__dataclass_fields__}
                        )
                    )
                ]
            )

    await PgClientCredentialRepo().issue(
        "workstation-claude", b"x" * 32, ["read"], [], "operator", reason="setup", session=Session()
    )
    assert writes[1]["payload"]["reason"] == "setup"
    assert "token_sha256" not in writes[1]["payload"]


async def test_repository_unelevate_records_ending_reason_without_changing_grant_reason() -> None:
    row = elevation()
    writes: list[Any] = []

    class Session:
        async def execute(self, statement: Any) -> FakeResult:
            writes.append(statement.compile().params)
            return FakeResult([asdict(row)])

        async def scalar(self, statement: Any) -> str:
            return "operator:slot"

    await PgClientCredentialRepo().end_elevation(
        row.id, NOW, author="operator", reason="finished", session=Session()
    )
    assert writes[1]["payload"]["reason"] == "maintenance"
    assert writes[1]["payload"]["ending_reason"] == "finished"
    assert writes[1]["payload"]["via"] == "cli"


def test_connections_attached_during_confirmation_are_not_frozen(
    cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, repo = cli
    tty = FakeTTY("yes\n")
    monkeypatch.setattr(tty, "isatty", lambda: True)

    def confirmation() -> Any:
        repo.session.links.append(
            {
                "connection_id": "late-connection-private",
                "client_id": "workstation-claude",
                "first_seen_at": NOW,
            }
        )
        return tty

    monkeypatch.setattr(module, "_open_tty", confirmation)
    assert module.main(["elevate", str(SESSION_ID), "--ttl", "1h", "--reason", "maintenance"]) == 0
    assert repo.grants[0].connection_ids == [CONNECTION]


def test_console_entry_point_names_sync_main() -> None:
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    assert (
        project["project"]["scripts"]["brain-credentials"]
        == "brain_v42.scripts.credentials_cli:main"
    )


@pytest.mark.parametrize(
    "patterns",
    [
        [],
        ["@project"],
        ["red-rail", "red", "operator", "agent:*", "service:*", "red"],
        ["0.a:b-", "a" * 64, "a" * 64 + "*"],
    ],
)
def test_issue_stores_sorted_unique_issuers(cli: Any, patterns: list[str]) -> None:
    module, repo = cli
    arguments = ["issue", "--client-id", "red-rail", "--families", "read", "--reason", "setup"]
    for pattern in patterns:
        arguments.extend(["--issuer", pattern])
    assert module.main(arguments) == 0
    assert repo.calls[0][1]["issuers"] == sorted(set(patterns))


@pytest.mark.parametrize(
    "pattern",
    ["*", "@other", "a*b", "a**", "a?", "a[bc]", "a b", "", "A", ":a", "a" * 65],
)
def test_invalid_issuer_is_refused_before_issue(
    cli: Any, pattern: str, capsys: pytest.CaptureFixture[str]
) -> None:
    module, repo = cli
    assert (
        module.main(
            [
                "issue",
                "--client-id",
                "red-rail",
                "--families",
                "read",
                "--reason",
                "setup",
                "--issuer",
                pattern,
            ]
        )
        == 2
    )
    assert repo.calls == []
    output = capsys.readouterr()
    assert output.out == ""
    assert "--issuer" in output.err


def test_confirmation_prompt_is_written_to_the_tty(
    cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _ = cli
    tty = FakeTTY("yes\n")
    monkeypatch.setattr(tty, "isatty", lambda: True)
    monkeypatch.setattr(module, "_open_tty", lambda: tty)
    assert module.main(["elevate", str(SESSION_ID), "--ttl", "1h", "--reason", "maintenance"]) == 0
    assert "[y/N]" in tty.prompt


def test_tty_supports_nonseekable_read_write_streams(
    cli: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, _ = cli

    class Terminal(io.RawIOBase):
        def __init__(self) -> None:
            super().__init__()
            self.answer = io.BytesIO(b"yes\n")

        def readable(self) -> bool:
            return True

        def writable(self) -> bool:
            return True

        def readinto(self, target: Any) -> int:
            return self.answer.readinto(target)

        def write(self, value: Any) -> int:
            return len(value)

        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(io, "FileIO", lambda *args, **kwargs: Terminal())
    with module._open_tty() as tty:
        assert tty.isatty()
        assert not tty.seekable()
        tty.write("Confirm? ")
        tty.flush()
        assert tty.readline() == "yes\n"
