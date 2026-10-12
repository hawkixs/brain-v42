"""Administer per-client credentials through audited, atomic operator gestures."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import io
import json
import re
import secrets
import sys
from collections.abc import Collection, Sequence
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from typing import Any, TextIO
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from brain_v42.config import get_settings
from brain_v42.credentials.families import STORABLE_FAMILIES
from brain_v42.credentials.redact import sanitize_label, short_id
from brain_v42.db.engine import dispose_engine, get_session_factory
from brain_v42.db.tables import brain_session_connections, brain_sessions
from brain_v42.repositories.pg_client_credentials import (
    MAX_ELEVATION,
    ClientCredentialError,
    CredentialRow,
    ElevationRow,
    PgClientCredentialRepo,
)


def _duration(value: str) -> timedelta:
    """Accept explicit units so an operator cannot mistake seconds for hours."""
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?[smhd])+", value):
        raise argparse.ArgumentTypeError("invalid_window: use a duration such as 30m or 1h")
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    try:
        seconds = sum(
            float(number) * units[unit]
            for number, unit in re.findall(r"(\d+(?:\.\d+)?)([smhd])", value)
        )
        result = timedelta(seconds=seconds)
    except (ValueError, OverflowError):
        raise argparse.ArgumentTypeError("invalid_window: duration is too large") from None
    if result <= timedelta(0):
        raise argparse.ArgumentTypeError("invalid_window: duration must be positive")
    return result


def _ttl(value: str) -> timedelta:
    result = _duration(value)
    if result > MAX_ELEVATION:
        raise argparse.ArgumentTypeError("invalid_window: elevation lasts at most 4h")
    return result


def _expires(value: str) -> datetime | timedelta:
    if re.fullmatch(r"(?:\d+(?:\.\d+)?[smhd])+", value):
        return _duration(value)
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expiry needs an ISO timestamp or duration") from None
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("expiry timestamp needs a timezone")
    return result.astimezone(UTC)


def _families(value: str) -> list[str]:
    families = list(dict.fromkeys(value.split(",")))
    if "admin" in families:
        raise argparse.ArgumentTypeError("admin is not storable; use an operator elevation")
    if not set(families) <= STORABLE_FAMILIES:
        raise argparse.ArgumentTypeError(
            "families must be read, write, delivery, telemetry or elevate"
        )
    if "elevate" in families and len(families) != 1:
        raise argparse.ArgumentTypeError("elevate must be the only family")
    return families


def _client_id(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,63}", value):
        raise argparse.ArgumentTypeError("client-id must match [a-z0-9][a-z0-9.-]{0,63}")
    return value


def _issuer(value: str) -> str:
    """Limit delegated actors to exact labels, prefixes or existing projects."""
    if value != "@project" and not re.fullmatch(r"[a-z0-9][a-z0-9.:-]{0,63}\*?", value):
        raise argparse.ArgumentTypeError("issuer must be @project, a label or a trailing-* prefix")
    return value


def _identifier(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("identifier must be a UUID") from None


def _reason(value: str) -> str:
    if not value.strip() or len(value) > 200:
        raise argparse.ArgumentTypeError("reason must contain 1..200 characters and not be blank")
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="brain-credentials", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue", help="Issue a bearer, printed once after commit")
    issue.add_argument("--client-id", type=_client_id, required=True)
    issue.add_argument("--families", type=_families, required=True)
    issue.add_argument("--issuer", type=_issuer, action="append", default=[])
    issue.add_argument("--expires", type=_expires)
    for command in ("revoke", "unelevate"):
        commands.add_parser(command).add_argument("id", type=_identifier)
    elevate = commands.add_parser(
        "elevate", help="Freeze an operator session's allowlisted connections"
    )
    elevate.add_argument("id", type=_identifier)
    elevate.add_argument("--ttl", type=_ttl, required=True)
    elevate.add_argument("--yes", action="store_true", help="Skip TTY confirmation")
    for command in ("issue", "revoke", "elevate", "unelevate"):
        commands.choices[command].add_argument("--reason", type=_reason, required=True)
    for command in ("list", "elevations"):
        commands.add_parser(command).add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _credential_fields(row: CredentialRow) -> dict[str, Any]:
    """Explicit fields keep future registry metadata out of the public CLI contract."""
    return {
        "id": str(row.id),
        "client_id": row.client_id,
        "families": row.families,
        "transition": row.transition,
        "expires_at": _iso(row.expires_at),
        "revoked_at": _iso(row.revoked_at),
    }


def _elevation_fields(row: ElevationRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "session_id": str(row.session_id),
        "expires_at": _iso(row.expires_at),
        "connection_count": len(row.connection_ids),
        "excluded_client_ids": row.excluded_client_ids,
        "excluded_connection_count": row.excluded_connection_count,
    }


def _listing(rows: Sequence[dict[str, Any]], json_mode: bool, fields: Sequence[str]) -> str:
    if json_mode:
        return json.dumps(rows) + "\n"

    def cell(value: Any) -> str:
        if value is None:
            return "-"
        if isinstance(value, list):
            return ",".join(str(sanitize_label(item)) for item in value) or "-"
        return str(value)

    cells = [list(fields), *[[cell(row[key]) for key in fields] for row in rows]]
    widths = [max(len(row[index]) for row in cells) for index in range(len(fields))]
    return "".join(
        "  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)).rstrip()
        + "\n"
        for row in cells
    )


def _open_tty() -> TextIO:
    # A TTY cannot seek; the default buffered r+ wrapper requires seeking.
    return io.TextIOWrapper(io.FileIO("/dev/tty", "r+"), encoding="utf-8", write_through=True)


def _confirm() -> None:
    """Use the controlling TTY, never piped stdin, for a privileged gesture."""
    try:
        with _open_tty() as tty:
            if not tty.isatty():
                raise ClientCredentialError("confirmation_requires_tty", "TTY required")
            tty.write("Grant elevation to the listed allowlisted connections? [y/N] ")
            tty.flush()
            answer = tty.readline().strip().lower()
    except OSError:
        raise ClientCredentialError(
            "confirmation_requires_tty", "TTY required; use --yes"
        ) from None
    if answer not in {"y", "yes"}:
        raise ClientCredentialError("confirmation_declined", "elevation cancelled")


async def _preview(session: AsyncSession, session_id: UUID, allowed: Collection[str]) -> list[str]:
    """Lock displayed pairs so confirmation cannot grant a different attribution."""
    owner = (
        (
            await session.execute(
                sa.select(brain_sessions.c.status, brain_sessions.c.nature)
                .where(brain_sessions.c.id == session_id)
                .with_for_update(read=True)
            )
        )
        .mappings()
        .one_or_none()
    )
    if owner is None:
        raise ClientCredentialError("unknown_session", "session not found")
    if owner["status"] != "open":
        raise ClientCredentialError("session_not_open", "session must be open")
    if owner["nature"] == "agent":
        raise ClientCredentialError("session_not_operator", "session must be an operator session")
    links = brain_session_connections
    rows = (
        (
            await session.execute(
                sa.select(links.c.connection_id, links.c.first_seen_at, links.c.client_id)
                .where(links.c.session_id == session_id, links.c.client_id.is_not(None))
                .order_by(links.c.first_seen_at, links.c.connection_id)
                .with_for_update(read=True)
            )
        )
        .mappings()
        .all()
    )
    attributed = [row for row in rows if row["client_id"] is not None]
    if not attributed:
        raise ClientCredentialError("no_attributed_connection", "no attributed connections")
    sys.stderr.write("connection  first_seen_at  client_id  disposition\n")
    for row in attributed:
        disposition = "allowlisted" if row["client_id"] in allowed else "excluded"
        sys.stderr.write(
            f"{short_id(row['connection_id'])}  {_iso(row['first_seen_at'])}  "
            f"{sanitize_label(row['client_id'])}  {disposition}\n"
        )
    if not any(row["client_id"] in allowed for row in attributed):
        raise ClientCredentialError("no_elevatable_connection", "no allowlisted connections")
    return [row["connection_id"] for row in attributed]


async def _execute(args: argparse.Namespace, repo: PgClientCredentialRepo) -> tuple[str, str]:
    if args.command == "list":
        fields = ("id", "client_id", "families", "transition", "expires_at", "revoked_at")
        return _listing(
            [_credential_fields(row) for row in await repo.list_rows()], args.json, fields
        ), ""
    if args.command == "elevations":
        rows = [_elevation_fields(row) for row in await repo.active_elevations(datetime.now(UTC))]
        fields = (
            "id",
            "session_id",
            "expires_at",
            "connection_count",
            "excluded_client_ids",
            "excluded_connection_count",
        )
        return _listing(rows, args.json, fields), ""
    author = getpass.getuser()
    async with repo.transaction() as session:
        if args.command == "issue":
            token = "bk1_" + secrets.token_urlsafe(32)
            expires_at = args.expires
            if isinstance(expires_at, timedelta):
                expires_at = datetime.now(UTC) + expires_at
            issued = await repo.issue(
                client_id=args.client_id,
                token_sha256=hashlib.sha256(token.encode()).digest(),
                families=args.families,
                issuers=sorted(set(args.issuer)),
                created_by=author,
                expires_at=expires_at,
                reason=args.reason,
                session=session,
            )
            output = token + "\n"
            summary = f"Issued {issued.id} for {issued.client_id} ({','.join(issued.families)}).\n"
        elif args.command == "revoke":
            revoked = await repo.revoke(
                args.id, args.reason, datetime.now(UTC), author=author, session=session
            )
            output, summary = "", f"Revoked {revoked.id} for {revoked.client_id}.\n"
        elif args.command == "unelevate":
            ended = await repo.end_elevation(
                args.id, datetime.now(UTC), author=author, reason=args.reason, session=session
            )
            output, summary = "", f"Ended elevation {ended.id}.\n"
        else:
            allowed = get_settings().elevatable_client_ids
            connections = await _preview(session, args.id, allowed)
            if not args.yes:
                _confirm()
            # Confirmation time must not consume the requested TTL.
            now = datetime.now(UTC)
            granted = await repo.grant_elevation(
                session_id=args.id,
                connection_ids=connections,
                expires_at=now + args.ttl,
                granted_by=author,
                reason=args.reason,
                now=now,
                elevatable_client_ids=allowed,
                via="cli",
                requested_by_client_id=None,
                session=session,
            )
            output, summary = f"{granted.id} {_iso(granted.expires_at)}\n", ""
    return output, summary


async def run_from_args(args: argparse.Namespace) -> int:
    # Engine/repository logging may use stdout. Reserve stdout for the public result,
    # and emit it only once all commits and cleanup have succeeded.
    with redirect_stdout(sys.stderr):
        try:
            repo = PgClientCredentialRepo(get_session_factory())
            output, summary = await _execute(args, repo)
        finally:
            await dispose_engine()
    sys.stderr.write(summary)
    sys.stdout.write(output)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run_from_args(parse_args(argv)))
    except SystemExit as exc:
        return int(exc.code or 0)
    except ClientCredentialError as exc:
        # Only the stable code is safe: exception messages may contain SQL parameters.
        sys.stderr.write(f"Credential operation refused: {exc.code}\n")
        return 2
    except Exception as exc:  # noqa: BLE001 - bounded CLI boundary
        sys.stderr.write(f"Credential operation failed: {type(exc).__name__}\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("Credential operation cancelled.\n")
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
