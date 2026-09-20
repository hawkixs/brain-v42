"""Immutable measurement result types that keep trusted values and failures distinct."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal, cast
from uuid import UUID

from brain_v42.facts.canonical import canonical_json, measurement_digest


class FactTarget(StrEnum):
    """Closed set of locations a fact probe is allowed to observe."""

    PRODUCTION = "production"
    BRAIN_TEST = "brain_test"
    LIVE_RELEASE = "live_release"
    REPOSITORY = "repository"
    HOST = "host"
    GITHUB = "github"
    PROVIDER = "provider"


FACT_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_POSTGRES_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
_RELEASE_SHA = re.compile(r"[0-9a-f]{40}")
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_SOURCE_KINDS = frozenset({"probe", "cache"})
ERROR_CODES = frozenset(
    {
        "timeout",
        "capacity_timeout",
        "briefing_budget",
        "refresh_budget",
        "target_mismatch",
        "probe_error",
        "value_too_large",
        "value_not_canonical",
        "identity_unreadable",
        "definition_drift",
    }
)


class InvalidFactNameError(ValueError):
    """Raised when a fact key cannot be a stable catalogue identifier."""


def validate_fact_name(name: str) -> str:
    """Return a valid fact name so all measurement types share one vocabulary check."""
    if not isinstance(name, str) or FACT_NAME.fullmatch(name) is None:
        raise InvalidFactNameError("fact name must match ^[a-z][a-z0-9_]{0,63}$")
    return name


def _validate_measurement_fields(
    *,
    fact: str,
    definition_version: int,
    target: FactTarget,
    observation_id: UUID,
    measured_at: datetime,
    duration_ms: int,
    ttl_seconds: int,
    source_kind: Literal["probe", "cache"],
) -> None:
    """Reject invalid metadata before it can be presented as a real observation."""
    validate_fact_name(fact)
    if type(definition_version) is not int or definition_version < 1:
        raise ValueError("definition_version must be a positive integer")
    if not isinstance(target, FactTarget):
        raise ValueError("target must be a FactTarget")
    if not isinstance(observation_id, UUID):
        raise ValueError("observation_id must be a UUID")
    if measured_at.tzinfo is None or measured_at.utcoffset() != timedelta(0):
        raise ValueError("measured_at must be timezone-aware UTC")
    if type(duration_ms) is not int or duration_ms < 0:
        raise ValueError("duration_ms must be a non-negative integer")
    if type(ttl_seconds) is not int or ttl_seconds < 0:
        raise ValueError("ttl_seconds must be a non-negative integer")
    if not isinstance(source_kind, str) or source_kind not in _SOURCE_KINDS:
        raise ValueError("source_kind must be 'probe' or 'cache'")


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """PostgreSQL source fields needed to reject a probe pointed at the wrong cluster."""

    system_identifier: str
    database: str
    server_addr: str
    server_port: int

    def __post_init__(self) -> None:
        """Validate all fields locally so composition can fail closed without I/O."""
        if (
            not isinstance(self.system_identifier, str)
            or not self.system_identifier.isascii()
            or not self.system_identifier.isdigit()
            or len(self.system_identifier) > 20
            or (len(self.system_identifier) > 1 and self.system_identifier.startswith("0"))
            or int(self.system_identifier) > (2**64 - 1)
        ):
            # A leading zero is refused rather than normalised: two spellings of
            # one identifier must not exist, and "007" is not how PostgreSQL
            # prints one.
            raise ValueError("system_identifier must be a 64-bit unsigned decimal string")
        if (
            not isinstance(self.database, str)
            or _POSTGRES_IDENTIFIER.fullmatch(self.database) is None
        ):
            raise ValueError("database must be a lowercase PostgreSQL identifier")
        if not isinstance(self.server_addr, str):
            raise ValueError("server_addr must be an IP address literal")
        address = self.server_addr
        # PostgreSQL prints inet_server_addr() with a full-length prefix; only
        # that suffix is stripped — a real network prefix is not an address.
        if address.endswith("/32") or address.endswith("/128"):
            address = address.rsplit("/", maxsplit=1)[0]
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ValueError("server_addr must be an IP address literal") from exc
        if type(self.server_port) is not int or not 1 <= self.server_port <= 65535:
            raise ValueError("server_port must be between 1 and 65535")
        # Canonical spelling: `::1`, `0:0:0:0:0:0:0:1` and the zero-padded form
        # are one address and must compare equal.
        object.__setattr__(self, "server_addr", str(parsed))

    def as_dict(self) -> dict[str, str | int]:
        """Return plain scalar data for comparison and JSON API serialization."""
        return {
            "system_identifier": self.system_identifier,
            "database": self.database,
            "server_addr": self.server_addr,
            "server_port": self.server_port,
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> SourceIdentity:
        """Require every identity field so omitted evidence cannot silently compare equal."""
        expected = frozenset({"system_identifier", "database", "server_addr", "server_port"})
        actual = frozenset(mapping)
        missing = expected - actual
        extra = actual - expected
        if missing:
            raise ValueError(f"source identity has missing keys: {sorted(missing)!r}")
        if extra:
            raise ValueError(f"source identity has extra keys: {sorted(extra)!r}")
        return cls(
            system_identifier=cast(str, mapping["system_identifier"]),
            database=cast(str, mapping["database"]),
            server_addr=cast(str, mapping["server_addr"]),
            server_port=cast(int, mapping["server_port"]),
        )


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    """Release fields that prove which immutable package the process imported.

    A checkout has no release identity: its version function returns ``dev``.
    Refusing that value avoids presenting development source as a shipped release.
    """

    release_sha: str
    package_version: str

    def __post_init__(self) -> None:
        """Reject release values that cannot identify one immutable shipped package."""
        if (
            not isinstance(self.release_sha, str)
            or _RELEASE_SHA.fullmatch(self.release_sha) is None
        ):
            raise ValueError("release_sha must be exactly 40 lowercase hexadecimal characters")
        if (
            not isinstance(self.package_version, str)
            or not self.package_version
            or len(self.package_version) > 64
            or not self.package_version.isascii()
            or not self.package_version.isprintable()
            or self.package_version == "dev"
        ):
            raise ValueError(
                "package_version must be printable ASCII, at most 64 characters, and not dev"
            )

    def as_dict(self) -> dict[str, str]:
        """Return plain scalar data for comparison and JSON API serialization."""
        return {"release_sha": self.release_sha, "package_version": self.package_version}

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> ReleaseIdentity:
        """Require every identity field so omitted evidence cannot silently compare equal."""
        expected = frozenset({"release_sha", "package_version"})
        actual = frozenset(mapping)
        missing = expected - actual
        extra = actual - expected
        if missing:
            raise ValueError(f"source identity has missing keys: {sorted(missing)!r}")
        if extra:
            raise ValueError(f"source identity has extra keys: {sorted(extra)!r}")
        return cls(
            release_sha=cast(str, mapping["release_sha"]),
            package_version=cast(str, mapping["package_version"]),
        )


@dataclass(frozen=True, slots=True)
class HostIdentity:
    """Hostname the process sees, not evidence of who authored a local file."""

    hostname: str

    def __post_init__(self) -> None:
        """Canonicalize a hostname because DNS hostnames compare without case."""
        if (
            not isinstance(self.hostname, str)
            or not self.hostname
            or len(self.hostname) > 253
            or not self.hostname.isascii()
            or any(_HOST_LABEL.fullmatch(label) is None for label in self.hostname.split("."))
        ):
            raise ValueError("hostname must be a DNS hostname of at most 253 ASCII characters")
        object.__setattr__(self, "hostname", self.hostname.lower())

    def as_dict(self) -> dict[str, str]:
        """Return plain scalar data for comparison and JSON API serialization."""
        return {"hostname": self.hostname}

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> HostIdentity:
        """Require every identity field so omitted evidence cannot silently compare equal."""
        expected = frozenset({"hostname"})
        actual = frozenset(mapping)
        missing = expected - actual
        extra = actual - expected
        if missing:
            raise ValueError(f"source identity has missing keys: {sorted(missing)!r}")
        if extra:
            raise ValueError(f"source identity has extra keys: {sorted(extra)!r}")
        return cls(hostname=cast(str, mapping["hostname"]))


Identity = SourceIdentity | ReleaseIdentity | HostIdentity


@dataclass(frozen=True, slots=True)
class Measured:
    """A successful immutable observation whose value text and digest prove each other."""

    fact: str
    definition_version: int
    target: FactTarget
    source: Identity
    value_json: str
    digest: str
    observation_id: UUID
    measured_at: datetime
    duration_ms: int
    ttl_seconds: int
    source_kind: Literal["probe", "cache"]

    def __post_init__(self) -> None:
        """Refuse malformed stored text before it can masquerade as measured evidence."""
        _validate_measurement_fields(
            fact=self.fact,
            definition_version=self.definition_version,
            target=self.target,
            observation_id=self.observation_id,
            measured_at=self.measured_at,
            duration_ms=self.duration_ms,
            ttl_seconds=self.ttl_seconds,
            source_kind=self.source_kind,
        )
        if not isinstance(self.source, (SourceIdentity, ReleaseIdentity, HostIdentity)):
            raise ValueError("source must be an identity")
        if not isinstance(self.value_json, str):
            raise ValueError("value_json must be canonical JSON")
        try:
            parsed = json.loads(self.value_json)
            if not isinstance(parsed, dict):
                raise ValueError
            canonical = canonical_json(cast(dict[str, object], parsed))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("value_json must be canonical JSON") from exc
        if canonical != self.value_json:
            raise ValueError("value_json must be canonical JSON")
        if not isinstance(self.digest, str) or self.digest != measurement_digest(self.value_json):
            raise ValueError("digest must match value_json")

    @property
    def value(self) -> dict[str, object]:
        """Parse anew so a reader can never mutate the value this evidence owns."""
        parsed = json.loads(self.value_json)
        if not isinstance(parsed, dict):  # Construction has already ruled this out.
            raise ValueError("value_json must be an object")
        return cast(dict[str, object], parsed)

    @classmethod
    def from_value(
        cls,
        *,
        fact: str,
        definition_version: int,
        target: FactTarget,
        source: Identity,
        value: Mapping[str, object],
        observation_id: UUID,
        measured_at: datetime,
        duration_ms: int,
        ttl_seconds: int,
        source_kind: Literal["probe", "cache"] = "probe",
    ) -> Measured:
        """Build coupled canonical text and digest together to avoid split-brain evidence."""
        value_json = canonical_json(value)
        return cls(
            fact=fact,
            definition_version=definition_version,
            target=target,
            source=source,
            value_json=value_json,
            digest=measurement_digest(value_json),
            observation_id=observation_id,
            measured_at=measured_at,
            duration_ms=duration_ms,
            ttl_seconds=ttl_seconds,
            source_kind=source_kind,
        )


@dataclass(frozen=True, slots=True)
class Unreadable:
    """A bounded failure result that cannot be confused with a fact value."""

    fact: str
    definition_version: int
    target: FactTarget
    error_code: str
    where: str | None
    observation_id: UUID
    measured_at: datetime
    duration_ms: int
    ttl_seconds: int
    source_kind: Literal["probe", "cache"]

    def __post_init__(self) -> None:
        """Keep errors stable and diagnostics short enough for safe API publication."""
        _validate_measurement_fields(
            fact=self.fact,
            definition_version=self.definition_version,
            target=self.target,
            observation_id=self.observation_id,
            measured_at=self.measured_at,
            duration_ms=self.duration_ms,
            ttl_seconds=self.ttl_seconds,
            source_kind=self.source_kind,
        )
        if not isinstance(self.error_code, str) or self.error_code not in ERROR_CODES:
            raise ValueError("error_code must be in ERROR_CODES")
        if self.where is not None and (not isinstance(self.where, str) or len(self.where) > 120):
            raise ValueError("where must be None or at most 120 characters")


Measurement = Measured | Unreadable


def _json_instant(value: datetime) -> str:
    """Render the already-validated UTC instant in the wire format callers expect."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def measurement_to_json(measurement: Measurement) -> dict[str, object]:
    """Expose a discriminated plain-data shape without leaking mutable stored state."""
    common: dict[str, object] = {
        "fact": measurement.fact,
        "definition_version": measurement.definition_version,
        "target": measurement.target.value,
        "observation_id": str(measurement.observation_id),
        "measured_at": _json_instant(measurement.measured_at),
        "duration_ms": measurement.duration_ms,
        "ttl_seconds": measurement.ttl_seconds,
        "source_kind": measurement.source_kind,
    }
    if isinstance(measurement, Measured):
        return {
            "status": "measured",
            **common,
            "source": measurement.source.as_dict(),
            "value": measurement.value,
            "digest": measurement.digest,
        }
    return {
        "status": "unreadable",
        **common,
        "error_code": measurement.error_code,
        "where": measurement.where,
    }


def with_source_kind(measurement: Measurement, kind: Literal["probe", "cache"]) -> Measurement:
    """Mark a served result as cached while preserving every observed datum unchanged."""
    if isinstance(measurement, Measured):
        return replace(measurement, source_kind=kind)
    return replace(measurement, source_kind=kind)
