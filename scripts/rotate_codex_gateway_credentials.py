#!/usr/bin/env python3
"""Rotate Brain PostgreSQL credentials and the private Codex gateway bearer."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlsplit

import asyncpg
from sqlalchemy.engine import URL, make_url

_JOURNAL_NAME = ".codex-gateway-rotation-state"
_LOCK_NAME = ".codex-gateway-rotation.lock"
_GATEWAY_ENV_NAME = "codex-gateway.env"
_GATEWAY_URL = "http://brain-codex-gateway:9211"
_DORMANT_LEGACY_GATEWAY_URL = "http://host.docker.internal:9211"
_GATEWAY_PORT = 9211
_SHRIK_ENV = Path("/etc/shrik/env")
_SHRIK_CONTROL = "/usr/local/sbin/brain-shrik-env-control"
_SHRIK_STAGE_NAME = ".shrik-env.install"
_MAX_ENV_BYTES = 128 * 1024
_HEX_SECRET_LENGTH = 64
_REVISION_SHAPE = re.compile(r"[0-9a-z_]+")
_POSTGRES_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)
# Le contrat que la passerelle exige RÉELLEMENT du schéma : dix vues, et pour
# chacune les colonnes que son consommateur lit. La révision Alembic n'est
# qu'un PROXY de cet invariant — `deploy/CODEX_GATEWAY.md` le dit lui-même,
# « ce head conserve les dix vues requises par la gateway ». Un proxy dit non
# à un schéma compatible dès que le numéro bouge (sept migrations durant), et
# oui à un schéma qui a perdu une colonne.
#
# Colonnes mesurées, pas devinées : les neuf premières viennent de
# `tests/integration/db/test_codex_contract_views_036.py::CONTRACT_COLUMNS`,
# la dixième de la spec du consommateur, vérifiée identique en base.
#
# L'assertion est un SUR-ENSEMBLE : une colonne AJOUTÉE passe, seule une
# colonne disparue mord.
_CODEX_GATEWAY_CONTRACT: Mapping[str, tuple[str, ...]] = {
    "codex_brain_entity_v1": (
        "id",
        "type",
        "title",
        "status",
        "freshness_status",
        "content",
        "project_key",
        "updated_at",
        "superseded_by",
        "merged_into",
    ),
    "codex_ticket_v1": (
        "id",
        "kind",
        "title",
        "body",
        "from_project",
        "to_project",
        "status",
        "extraction_status",
        "resolved_at",
        "closed_at",
        "created_at",
        "updated_at",
    ),
    "codex_ticket_message_v1": (
        "id",
        "ticket_id",
        "author_project",
        "body",
        "status_to",
        "created_at",
    ),
    "codex_feature_v1": (
        "id",
        "project_key",
        "name",
        "description",
        "status",
        "status_updated_at",
        "pinned",
        "merged_into",
        "created_at",
        "updated_at",
    ),
    "codex_feature_artifact_v1": (
        "feature_id",
        "artifact_type",
        "artifact_id",
        "similarity_score",
        "created_at",
    ),
    "codex_dream_run_v1": (
        "id",
        "run_date",
        "phase",
        "model",
        "status",
        "phase_dry_run",
        "duration_s",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "api_calls",
        "tool_calls",
        "error_message",
        "created_at",
    ),
    "codex_dream_promotion_v1": (
        "id",
        "dream_run_id",
        "source_learning_id",
        "target_type",
        "target_adr_id",
        "target_runbook_id",
        "cosine_observed",
        "skipped_reason",
        "created_at",
    ),
    "codex_ticket_extraction_proposal_v1": (
        "id",
        "ticket_id",
        "target_type",
        "target_project",
        "payload",
        "rationale",
        "status",
        "applied_entity_id",
        "created_at",
        "applied_at",
    ),
    "codex_roadmap_curation_proposal_v1": (
        "id",
        "op",
        "feature_id",
        "payload",
        "rationale",
        "status",
        "apply_log",
        "created_at",
        "applied_at",
    ),
    "codex_consolidation_log_v1": (
        "id",
        "source_id",
        "target_id",
        "entity_type",
        "similarity",
        "action",
        "created_at",
    ),
}
# DÉRIVÉ, jamais retapé : une seconde liste tenue d'accord à la main annule la
# garde, exactement comme la révision Alembic recopiée dans un test l'avait
# annulée (learning 8dc7e042).
_CODEX_GATEWAY_VIEWS = tuple(_CODEX_GATEWAY_CONTRACT)

# `unnest($1, $2)` PADE le tableau le plus court avec NULL. Un désalignement
# ferait donc remonter `{NULL}` parmi les manquants et exploserait au join,
# en « credential cutover failed ». Les deux tableaux sont construits d'un
# seul parcours pour que ce désalignement soit impossible par construction.
_GATEWAY_CONTRACT_PROOF = """
WITH expected(view_name, column_name) AS (
    SELECT * FROM unnest($1::text[], $2::text[])
)
SELECT
    coalesce(
        array_agg(DISTINCT view_name ORDER BY view_name)
        FILTER (WHERE to_regclass('public.' || view_name) IS NULL),
        '{}'
    ) AS missing_views,
    coalesce(
        array_agg(DISTINCT view_name || '.' || column_name
                  ORDER BY view_name || '.' || column_name)
        FILTER (
            WHERE to_regclass('public.' || view_name) IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM pg_attribute
                WHERE attrelid = to_regclass('public.' || view_name)
                AND attname = column_name
                AND attnum > 0
                AND NOT attisdropped
            )
        ),
        '{}'
    ) AS missing_columns
FROM expected
"""


def _gateway_contract_arrays(
    contract: Mapping[str, tuple[str, ...]],
) -> tuple[list[str], list[str]]:
    """Aplatit le contrat en deux tableaux PARALLÈLES de même longueur.

    Une vue absente est comptée UNE fois — la garde `to_regclass IS NOT NULL`
    du SQL empêche ses colonnes de la suivre dans le rapport, sans quoi une
    seule vue disparue noierait le diagnostic sous douze lignes.
    """
    views: list[str] = []
    columns: list[str] = []
    for view, view_columns in contract.items():
        for column in view_columns:
            views.append(view)
            columns.append(column)
    return views, columns


_SAFE_SUBPROCESS_ENV_KEYS = (
    "DOCKER_CERT_PATH",
    "DOCKER_CONTEXT",
    "DOCKER_HOST",
    "DOCKER_TLS_VERIFY",
    "HOME",
    "PATH",
    "XDG_RUNTIME_DIR",
)


class RotationError(RuntimeError):
    """Secret-free operator failure."""


class CredentialDatabase(Protocol):
    """PostgreSQL boundary needed by the rotation state machine."""

    def probe(self, role: str, password: str) -> bool: ...

    def rotate(
        self,
        current_brain: str,
        next_brain: str,
        current_codex: str,
        next_codex: str,
    ) -> None: ...

    def revision(self, brain_password: str) -> str: ...

    def codex_scope_is_bounded(self, codex_password: str) -> bool: ...

    def missing_gateway_contract(self, codex_password: str) -> tuple[str, ...]: ...


class PrivilegedInstaller(Protocol):
    """Fixed privileged boundary for the single Shrik environment file."""

    def preflight(self, target: Path) -> None: ...

    def install(
        self,
        target: Path,
        payload: bytes,
        *,
        mode: int,
        uid: int,
        gid: int,
    ) -> None: ...


class GatewayProbe(Protocol):
    """Gateway authentication proof executed after consumer recreation."""

    def prove(self, old_token: str, new_token: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class RotationConfig:
    """Non-secret operator inputs for one coordinated rotation."""

    brain_root: Path
    red_root: Path
    private_dir: Path
    shrik_env: Path
    apply: bool
    resume: bool
    rollback: bool
    consumers_stopped_confirmed: bool
    rollback_preflight_confirmed: bool
    consumers_recreated_confirmed: bool
    expected_alembic_revision: str
    """Head Alembic MESURÉ par l'opérateur juste avant la procédure.

    Sans défaut, et c'est le point : la garde comparait à la constante ``"037"``, si
    bien qu'elle s'est périmée à la migration suivante et que la rotation entière est
    devenue inexécutable. Une garde qui code en dur un numéro de révision finit par
    ne plus garder que contre elle-même. Elle n'est pas retirée — elle existe pour
    garantir que la rotation tourne contre le schéma attendu — elle cesse de périmer.
    """


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Sensitive original file state stored only in the private journal."""

    path: str
    content: str = field(repr=False)
    mode: int
    uid: int
    gid: int
    existed: bool


@dataclass(frozen=True, slots=True)
class RotationState:
    """One resumable credential generation."""

    fingerprint: str
    old_brain: str = field(repr=False)
    new_brain: str = field(repr=False)
    old_codex: str = field(repr=False)
    new_codex: str = field(repr=False)
    old_bearer: str = field(repr=False)
    new_bearer: str = field(repr=False)
    snapshots: tuple[FileSnapshot, ...] = field(repr=False)


def _safe_failure(condition: bool, message: str) -> None:
    if condition:
        raise RotationError(message) from None


def _fsync_directory(path: Path) -> None:
    directory_fd = -1
    failed = False
    try:
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(directory_fd)
    except OSError:
        failed = True
    finally:
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                failed = True
    _safe_failure(failed, "durable directory update failed")


def _atomic_write(target: Path, payload: bytes, mode: int) -> None:
    """Install complete bytes atomically without rendering their content."""
    temporary_fd = -1
    temporary_path: Path | None = None
    failed = False
    try:
        try:
            target_stat = target.lstat()
        except FileNotFoundError:
            target_stat = None
        if target_stat is not None and not stat.S_ISREG(target_stat.st_mode):
            failed = True
        if not failed:
            temporary_fd, raw_path = tempfile.mkstemp(
                prefix=f".{target.name}.",
                dir=target.parent,
            )
            temporary_path = Path(raw_path)
            os.fchmod(temporary_fd, mode)
            with os.fdopen(temporary_fd, "wb", closefd=True) as stream:
                temporary_fd = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
            temporary_path = None
            _fsync_directory(target.parent)
    except (OSError, RotationError):
        failed = True
    finally:
        if temporary_fd >= 0:
            try:
                os.close(temporary_fd)
            except OSError:
                failed = True
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                failed = True
    _safe_failure(failed, "atomic credential update failed")


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    lock_fd = -1
    failed = False
    try:
        lock_fd = os.open(
            path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        failed = True
    if failed and lock_fd >= 0:
        os.close(lock_fd)
        lock_fd = -1
    _safe_failure(failed, "another credential cutover is already active")
    try:
        yield
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)


def _assignment_key(raw_line: str) -> str | None:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line.removeprefix("export ").lstrip()
    key, separator, _value = line.partition("=")
    if not separator:
        return None
    return key.strip()


def _assignment_value(raw_line: str) -> str:
    line = raw_line.strip()
    if line.startswith("export "):
        line = line.removeprefix("export ").lstrip()
    return line.partition("=")[2].strip().strip("'\"")


def _managed_values(content: str, keys: set[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    duplicate = False
    for raw_line in content.splitlines():
        key = _assignment_key(raw_line)
        if key not in keys:
            continue
        if key in values:
            duplicate = True
            continue
        values[key] = _assignment_value(raw_line)
    _safe_failure(duplicate, "environment contains duplicate managed keys")
    return values


def _render_environment(
    original: str,
    replacements: dict[str, str],
    *,
    add_missing: set[str],
) -> str:
    """Replace each managed assignment once while preserving unrelated text."""
    seen: set[str] = set()
    rendered: list[str] = []
    duplicate = False
    for raw_line in original.splitlines():
        key = _assignment_key(raw_line)
        if key not in replacements:
            rendered.append(raw_line)
            continue
        if key in seen:
            duplicate = True
            continue
        seen.add(key)
        rendered.append(f"{key}={replacements[key]}")
    _safe_failure(duplicate, "environment contains duplicate managed keys")
    missing = set(replacements) - seen
    _safe_failure(
        not missing.issubset(add_missing),
        "environment is missing managed keys",
    )
    for key in replacements:
        if key in missing:
            rendered.append(f"{key}={replacements[key]}")
    return "\n".join(rendered) + "\n"


def _operator_private_dir() -> Path:
    return Path.home() / ".config" / "brain-v42" / "codex-gateway-rotation"


def _gateway_env(config: RotationConfig) -> Path:
    return config.private_dir.parent / _GATEWAY_ENV_NAME


def _brain_env(config: RotationConfig) -> Path:
    return config.brain_root / ".env"


def _red_data_env(config: RotationConfig) -> Path:
    return config.red_root / "projects" / "red-data" / ".env"


def _red_codex_env(config: RotationConfig) -> Path:
    return config.red_root / "projects" / "red-codex" / ".env.local"


def _trusted_directory(path: Path, *, exact_mode: int | None = None) -> bool:
    try:
        path_stat = path.lstat()
        mode = stat.S_IMODE(path_stat.st_mode)
        group_write_is_untrusted = bool(mode & 0o020) and path_stat.st_gid != os.getgid()
        return (
            stat.S_ISDIR(path_stat.st_mode)
            and path_stat.st_uid == os.getuid()
            and not bool(mode & 0o002)
            and not group_write_is_untrusted
            and (exact_mode is None or mode == exact_mode)
            and path.resolve(strict=True) == path
        )
    except (OSError, RuntimeError):
        return False


def _validate_config_paths(config: RotationConfig) -> None:
    failed = any(
        not path.is_absolute()
        for path in (config.brain_root, config.red_root, config.private_dir, config.shrik_env)
    )
    failed = failed or not _trusted_directory(config.brain_root)
    failed = failed or not _trusted_directory(config.red_root)
    failed = failed or config.private_dir != _operator_private_dir()
    failed = failed or config.private_dir.resolve(strict=False) != config.private_dir
    failed = failed or config.shrik_env != _SHRIK_ENV
    failed = failed or not _trusted_directory(config.private_dir.parent, exact_mode=0o700)
    if config.private_dir.exists():
        failed = failed or not _trusted_directory(config.private_dir, exact_mode=0o700)
    _safe_failure(failed, "canonical credential paths are invalid")


def _read_snapshot(
    path: Path,
    *,
    allowed_modes: set[int],
    required: bool,
    require_operator_owner: bool,
) -> FileSnapshot:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        _safe_failure(required, "required consumer environment is missing")
        return FileSnapshot(
            path=str(path),
            content="",
            mode=0o600,
            uid=os.getuid(),
            gid=os.getgid(),
            existed=False,
        )
    except OSError:
        raise RotationError("consumer environment is unreadable") from None

    mode = stat.S_IMODE(path_stat.st_mode)
    failed = not stat.S_ISREG(path_stat.st_mode)
    failed = failed or path_stat.st_size > _MAX_ENV_BYTES
    failed = failed or mode not in allowed_modes
    failed = failed or (require_operator_owner and path_stat.st_uid != os.getuid())
    failed = failed or path.resolve(strict=True) != path
    _safe_failure(failed, "consumer environment is unsafe")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise RotationError("consumer environment is unreadable") from None
    return FileSnapshot(
        path=str(path),
        content=content,
        mode=mode,
        uid=path_stat.st_uid,
        gid=path_stat.st_gid,
        existed=True,
    )


def _capture_snapshots(config: RotationConfig) -> tuple[FileSnapshot, ...]:
    _validate_config_paths(config)
    return (
        _read_snapshot(
            _brain_env(config),
            allowed_modes={0o600},
            required=True,
            require_operator_owner=True,
        ),
        _read_snapshot(
            _red_data_env(config),
            allowed_modes={0o600, 0o640, 0o660, 0o664},
            required=True,
            require_operator_owner=True,
        ),
        _read_snapshot(
            _red_codex_env(config),
            allowed_modes={0o600},
            required=True,
            require_operator_owner=True,
        ),
        _read_snapshot(
            config.shrik_env,
            allowed_modes={0o600, 0o640},
            required=True,
            require_operator_owner=False,
        ),
        _read_snapshot(
            _gateway_env(config),
            allowed_modes={0o600},
            required=False,
            require_operator_owner=True,
        ),
    )


def _snapshot_map(snapshots: tuple[FileSnapshot, ...]) -> dict[Path, FileSnapshot]:
    return {Path(snapshot.path): snapshot for snapshot in snapshots}


def _validated_dsn(value: str, *, expected_role: str) -> URL:
    failed = not value or value != value.strip()
    parsed: URL | None = None
    try:
        raw_url = urlsplit(value)
        query_items = parse_qsl(
            raw_url.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
        query = dict(query_items)
        failed = failed or len(query) != len(query_items)
        failed = failed or not set(query).issubset({"sslmode"})
        if "sslmode" in query:
            failed = failed or query["sslmode"] not in _POSTGRES_SSLMODES
        failed = failed or bool(raw_url.fragment)
        parsed = make_url(value)
        failed = failed or parsed.drivername not in {"postgresql", "postgresql+asyncpg"}
        failed = failed or parsed.username != expected_role
        failed = failed or not parsed.password
        failed = failed or not parsed.host
        failed = failed or parsed.database != "brain"
        if parsed.port is not None:
            failed = failed or not 1 <= parsed.port <= 65535
    except (AttributeError, TypeError, ValueError):
        failed = True
    _safe_failure(failed or parsed is None, "consumer PostgreSQL DSN is invalid")
    assert parsed is not None
    return parsed


def _render_dsn(original: str, *, expected_role: str, password: str) -> str:
    parsed = _validated_dsn(original, expected_role=expected_role)
    return parsed.set(password=password).render_as_string(hide_password=False)


def _current_credentials(
    config: RotationConfig,
    snapshots: tuple[FileSnapshot, ...],
) -> tuple[str, str, str]:
    snapshot_by_path = _snapshot_map(snapshots)
    brain_values = _managed_values(
        snapshot_by_path[_brain_env(config)].content,
        {"POSTGRES_URL", "POSTGRES_PASSWORD"},
    )
    red_data_values = _managed_values(
        snapshot_by_path[_red_data_env(config)].content,
        {"BRAIN_DB_PASSWORD"},
    )
    red_codex_values = _managed_values(
        snapshot_by_path[_red_codex_env(config)].content,
        {"CODEX_BRAIN_DSN", "CODEX_BRAIN_GATEWAY_URL", "CODEX_BRAIN_GATEWAY_TOKEN"},
    )
    shrik_values = _managed_values(
        snapshot_by_path[config.shrik_env].content,
        {"SHRIK_BRAIN_DSN"},
    )
    gateway_snapshot = snapshot_by_path[_gateway_env(config)]
    gateway_values = _managed_values(
        gateway_snapshot.content,
        {"BRAIN_CODEX_GATEWAY_TOKEN"},
    )

    required = (
        "POSTGRES_URL" in brain_values
        and "BRAIN_DB_PASSWORD" in red_data_values
        and "CODEX_BRAIN_DSN" in red_codex_values
        and "CODEX_BRAIN_GATEWAY_URL" in red_codex_values
        and "CODEX_BRAIN_GATEWAY_TOKEN" in red_codex_values
        and "SHRIK_BRAIN_DSN" in shrik_values
    )
    _safe_failure(not required, "consumer environment is missing managed keys")
    brain_dsn = _validated_dsn(brain_values["POSTGRES_URL"], expected_role="brain")
    codex_dsn = _validated_dsn(red_codex_values["CODEX_BRAIN_DSN"], expected_role="codex_ro")
    shrik_dsn = _validated_dsn(shrik_values["SHRIK_BRAIN_DSN"], expected_role="brain")
    old_brain = brain_dsn.password or ""
    old_codex = codex_dsn.password or ""
    consistent_brain = red_data_values["BRAIN_DB_PASSWORD"] == old_brain
    consistent_brain = consistent_brain and shrik_dsn.password == old_brain
    if "POSTGRES_PASSWORD" in brain_values:
        consistent_brain = consistent_brain and brain_values["POSTGRES_PASSWORD"] == old_brain
    _safe_failure(not consistent_brain, "brain consumer credentials are inconsistent")

    old_bearer = red_codex_values["CODEX_BRAIN_GATEWAY_TOKEN"]
    gateway_bearer = gateway_values.get("BRAIN_CODEX_GATEWAY_TOKEN", "")
    _safe_failure(
        gateway_snapshot.existed and old_bearer != gateway_bearer,
        "gateway bearer consumers are inconsistent",
    )
    current_gateway_url = red_codex_values["CODEX_BRAIN_GATEWAY_URL"]
    dormant_legacy_endpoint = (
        current_gateway_url == _DORMANT_LEGACY_GATEWAY_URL
        and old_bearer == ""
        and not gateway_snapshot.existed
    )
    _safe_failure(
        current_gateway_url not in {"", _GATEWAY_URL} and not dormant_legacy_endpoint,
        "gateway consumer URL is not canonical",
    )
    return old_brain, old_codex, old_bearer


def _fingerprint(config: RotationConfig) -> str:
    paths = (
        config.brain_root,
        config.red_root,
        config.private_dir,
        config.shrik_env,
        _gateway_env(config),
    )
    payload = "\n".join(str(path) for path in paths).encode()
    return hashlib.sha256(payload).hexdigest()


def _state_payload(state: RotationState) -> bytes:
    return (
        json.dumps(
            {
                "fingerprint": state.fingerprint,
                "new_bearer": state.new_bearer,
                "new_brain": state.new_brain,
                "new_codex": state.new_codex,
                "old_bearer": state.old_bearer,
                "old_brain": state.old_brain,
                "old_codex": state.old_codex,
                "snapshots": [
                    {
                        "content": snapshot.content,
                        "existed": snapshot.existed,
                        "gid": snapshot.gid,
                        "mode": snapshot.mode,
                        "path": snapshot.path,
                        "uid": snapshot.uid,
                    }
                    for snapshot in state.snapshots
                ],
                "version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        + b"\n"
    )


def _valid_hex_secret(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HEX_SECRET_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_state(path: Path, config: RotationConfig) -> RotationState:
    failed = False
    state: RotationState | None = None
    try:
        path_stat = path.lstat()
        failed = not stat.S_ISREG(path_stat.st_mode)
        failed = failed or stat.S_IMODE(path_stat.st_mode) != 0o600
        failed = failed or path_stat.st_uid != os.getuid()
        raw = json.loads(path.read_text(encoding="utf-8")) if not failed else {}
        expected_keys = {
            "fingerprint",
            "new_bearer",
            "new_brain",
            "new_codex",
            "old_bearer",
            "old_brain",
            "old_codex",
            "snapshots",
            "version",
        }
        failed = failed or set(raw) != expected_keys
        failed = failed or raw.get("version") != 1
        failed = failed or raw.get("fingerprint") != _fingerprint(config)
        failed = failed or not all(
            _valid_hex_secret(raw.get(key)) for key in ("new_brain", "new_codex", "new_bearer")
        )
        failed = failed or not all(
            isinstance(raw.get(key), str) for key in ("old_brain", "old_codex", "old_bearer")
        )
        snapshots_raw = raw.get("snapshots")
        failed = failed or not isinstance(snapshots_raw, list) or len(snapshots_raw or []) != 5
        snapshots: list[FileSnapshot] = []
        for item in snapshots_raw or []:
            if not isinstance(item, dict) or set(item) != {
                "content",
                "existed",
                "gid",
                "mode",
                "path",
                "uid",
            }:
                failed = True
                continue
            if not (
                isinstance(item["content"], str)
                and isinstance(item["existed"], bool)
                and isinstance(item["gid"], int)
                and isinstance(item["mode"], int)
                and isinstance(item["path"], str)
                and isinstance(item["uid"], int)
            ):
                failed = True
                continue
            snapshots.append(FileSnapshot(**item))
        expected_paths = {
            str(_brain_env(config)),
            str(_red_data_env(config)),
            str(_red_codex_env(config)),
            str(config.shrik_env),
            str(_gateway_env(config)),
        }
        failed = failed or {snapshot.path for snapshot in snapshots} != expected_paths
        if not failed:
            state = RotationState(
                fingerprint=raw["fingerprint"],
                old_brain=raw["old_brain"],
                new_brain=raw["new_brain"],
                old_codex=raw["old_codex"],
                new_codex=raw["new_codex"],
                old_bearer=raw["old_bearer"],
                new_bearer=raw["new_bearer"],
                snapshots=tuple(snapshots),
            )
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        failed = True
    _safe_failure(failed or state is None, "credential rotation journal is invalid")
    assert state is not None
    return state


def _ensure_private_directory(path: Path) -> None:
    failed = False
    try:
        if not path.exists():
            path.mkdir(mode=0o700)
            path.chmod(0o700)
            _fsync_directory(path.parent)
        failed = not _trusted_directory(path, exact_mode=0o700)
    except (OSError, RotationError):
        failed = True
    _safe_failure(failed, "private credential directory validation failed")


def _prepare_state(config: RotationConfig, journal: Path) -> RotationState:
    _safe_failure(journal.exists(), "unfinished credential rotation requires --resume")
    snapshots = _capture_snapshots(config)
    old_brain, old_codex, old_bearer = _current_credentials(config, snapshots)
    state = RotationState(
        fingerprint=_fingerprint(config),
        old_brain=old_brain,
        new_brain=secrets.token_hex(32),
        old_codex=old_codex,
        new_codex=secrets.token_hex(32),
        old_bearer=old_bearer,
        new_bearer=secrets.token_hex(32),
        snapshots=snapshots,
    )
    _atomic_write(journal, _state_payload(state), 0o600)
    return state


def _generation_payloads(config: RotationConfig, state: RotationState) -> dict[Path, bytes]:
    snapshots = _snapshot_map(state.snapshots)
    brain_values = _managed_values(
        snapshots[_brain_env(config)].content,
        {"POSTGRES_URL"},
    )
    red_codex_values = _managed_values(
        snapshots[_red_codex_env(config)].content,
        {"CODEX_BRAIN_DSN"},
    )
    shrik_values = _managed_values(
        snapshots[config.shrik_env].content,
        {"SHRIK_BRAIN_DSN"},
    )
    _safe_failure(
        not all((brain_values, red_codex_values, shrik_values)),
        "credential rotation journal is invalid",
    )
    return {
        _brain_env(config): _render_environment(
            snapshots[_brain_env(config)].content,
            {
                "POSTGRES_URL": _render_dsn(
                    brain_values["POSTGRES_URL"],
                    expected_role="brain",
                    password=state.new_brain,
                ),
                "POSTGRES_PASSWORD": state.new_brain,
            },
            add_missing={"POSTGRES_PASSWORD"},
        ).encode(),
        _red_data_env(config): _render_environment(
            snapshots[_red_data_env(config)].content,
            {"BRAIN_DB_PASSWORD": state.new_brain},
            add_missing=set(),
        ).encode(),
        _red_codex_env(config): _render_environment(
            snapshots[_red_codex_env(config)].content,
            {
                "CODEX_BRAIN_DSN": _render_dsn(
                    red_codex_values["CODEX_BRAIN_DSN"],
                    expected_role="codex_ro",
                    password=state.new_codex,
                ),
                "CODEX_BRAIN_GATEWAY_URL": _GATEWAY_URL,
                "CODEX_BRAIN_GATEWAY_TOKEN": state.new_bearer,
            },
            add_missing=set(),
        ).encode(),
        config.shrik_env: _render_environment(
            snapshots[config.shrik_env].content,
            {
                "SHRIK_BRAIN_DSN": _render_dsn(
                    shrik_values["SHRIK_BRAIN_DSN"],
                    expected_role="brain",
                    password=state.new_brain,
                )
            },
            add_missing=set(),
        ).encode(),
        _gateway_env(config): f"BRAIN_CODEX_GATEWAY_TOKEN={state.new_bearer}\n".encode(),
    }


def _install_generation(
    config: RotationConfig,
    state: RotationState,
    privileged_installer: PrivilegedInstaller,
) -> None:
    payloads = _generation_payloads(config, state)
    snapshots = _snapshot_map(state.snapshots)
    try:
        _atomic_write(_brain_env(config), payloads[_brain_env(config)], 0o600)
        _atomic_write(_red_data_env(config), payloads[_red_data_env(config)], 0o600)
        _atomic_write(_red_codex_env(config), payloads[_red_codex_env(config)], 0o600)
        _atomic_write(_gateway_env(config), payloads[_gateway_env(config)], 0o600)
        shrik_snapshot = snapshots[config.shrik_env]
        privileged_installer.install(
            config.shrik_env,
            payloads[config.shrik_env],
            mode=shrik_snapshot.mode,
            uid=shrik_snapshot.uid,
            gid=shrik_snapshot.gid,
        )
    except RotationError:
        raise
    except Exception:
        raise RotationError("privileged consumer installation failed") from None


def _prove_installed_generation(config: RotationConfig, state: RotationState) -> None:
    current = _snapshot_map(_capture_snapshots(config))
    expected_payloads = _generation_payloads(config, state)
    original = _snapshot_map(state.snapshots)
    expected_modes = {
        _brain_env(config): 0o600,
        _red_data_env(config): 0o600,
        _red_codex_env(config): 0o600,
        config.shrik_env: original[config.shrik_env].mode,
        _gateway_env(config): 0o600,
    }
    failed = set(current) != set(expected_payloads)
    for path, payload in expected_payloads.items():
        snapshot = current.get(path)
        failed = failed or snapshot is None or not snapshot.existed
        if snapshot is None:
            continue
        failed = failed or snapshot.content.encode() != payload
        failed = failed or snapshot.mode != expected_modes[path]

    current_shrik = current.get(config.shrik_env)
    original_shrik = original[config.shrik_env]
    if current_shrik is not None:
        failed = failed or current_shrik.uid != original_shrik.uid
        failed = failed or current_shrik.gid != original_shrik.gid
    _safe_failure(failed, "runtime credential files changed since installation")


def _remove_file(path: Path) -> None:
    try:
        path_stat = path.lstat()
        _safe_failure(not stat.S_ISREG(path_stat.st_mode), "credential rollback failed")
        path.unlink()
        _fsync_directory(path.parent)
    except FileNotFoundError:
        return
    except (OSError, RotationError):
        raise RotationError("credential rollback failed") from None


def _restore_snapshots(
    config: RotationConfig,
    state: RotationState,
    privileged_installer: PrivilegedInstaller,
) -> None:
    snapshots = _snapshot_map(state.snapshots)
    try:
        for path in (_brain_env(config), _red_data_env(config), _red_codex_env(config)):
            snapshot = snapshots[path]
            _atomic_write(path, snapshot.content.encode(), snapshot.mode)
        gateway_snapshot = snapshots[_gateway_env(config)]
        if gateway_snapshot.existed:
            _atomic_write(
                _gateway_env(config),
                gateway_snapshot.content.encode(),
                gateway_snapshot.mode,
            )
        else:
            _remove_file(_gateway_env(config))
        shrik_snapshot = snapshots[config.shrik_env]
        privileged_installer.install(
            config.shrik_env,
            shrik_snapshot.content.encode(),
            mode=shrik_snapshot.mode,
            uid=shrik_snapshot.uid,
            gid=shrik_snapshot.gid,
        )
    except RotationError:
        raise
    except Exception:
        raise RotationError("credential rollback failed") from None


def _credential_generation(database: CredentialDatabase, state: RotationState) -> str:
    old_brain = database.probe("brain", state.old_brain)
    new_brain = database.probe("brain", state.new_brain)
    old_codex = database.probe("codex_ro", state.old_codex)
    new_codex = database.probe("codex_ro", state.new_codex)
    if old_brain and old_codex and not new_brain and not new_codex:
        return "old"
    if new_brain and new_codex and not old_brain and not old_codex:
        return "new"
    raise RotationError("PostgreSQL credential generation is inconsistent") from None


def _refuse_a_broken_gateway_contract(database: CredentialDatabase, codex_password: str) -> None:
    """Preuve DIRECTE que la passerelle a le schéma qu'elle exige.

    Muette dans le cas nominal — aucun log, aucun champ de sortie : une ligne
    émise à chaque rotation s'apprendrait à être sautée. Elle ne parle que
    pour refuser, et alors elle nomme les manquants et EUX SEULS. Joindre le
    contrat entier rejouerait le défaut qu'on corrige : aujourd'hui une vue
    disparue fait bien échouer le préflight, mais par accident, avec un
    message qui accuse les privilèges.
    """
    missing = database.missing_gateway_contract(codex_password)
    _safe_failure(
        bool(missing),
        "Codex gateway contract is not satisfied: " + ", ".join(missing),
    )


def _prove_new_generation(
    database: CredentialDatabase, state: RotationState, expected_revision: str
) -> None:
    _safe_failure(
        _credential_generation(database, state) != "new",
        "new PostgreSQL credentials are not exclusively active",
    )
    # SEULE preuve de contrat du chemin --resume, qui ne repasse jamais par
    # _preflight. Rien n'est ajouté à _rollback_state : l'échappatoire reste
    # sans garde, et deux tests l'épinglent.
    _refuse_a_broken_gateway_contract(database, state.new_codex)
    measured = database.revision(state.new_brain)
    _safe_failure(
        measured != expected_revision,
        f"deployed Alembic revision {measured} differs from the declared {expected_revision}",
    )
    _safe_failure(
        not database.codex_scope_is_bounded(state.new_codex),
        "codex_ro privileges are not bounded",
    )


def _rollback_state(
    config: RotationConfig,
    state: RotationState,
    database: CredentialDatabase,
    privileged_installer: PrivilegedInstaller,
) -> None:
    generation = _credential_generation(database, state)
    if generation == "new":
        database.rotate(
            state.new_brain,
            state.old_brain,
            state.new_codex,
            state.old_codex,
        )
    _restore_snapshots(config, state, privileged_installer)
    _safe_failure(
        _credential_generation(database, state) != "old",
        "credential rollback could not be proven",
    )


def _preflight(
    config: RotationConfig,
    database: CredentialDatabase,
    privileged_installer: PrivilegedInstaller,
) -> dict[str, bool | str | int]:
    snapshots = _capture_snapshots(config)
    old_brain, old_codex, _old_bearer = _current_credentials(config, snapshots)
    try:
        privileged_installer.preflight(config.shrik_env)
    except RotationError:
        raise
    except Exception:
        raise RotationError("privileged Shrik preflight failed") from None
    old_valid = database.probe("brain", old_brain) and database.probe("codex_ro", old_codex)
    _safe_failure(not old_valid, "configured PostgreSQL credentials are not accepted")
    # AVANT la garde de révision, délibérément : quand les deux mordraient,
    # c'est la cause actionnable qui doit parler. « cette vue a disparu » se
    # traite ; « la révision a bougé » n'apprend rien à qui vient de migrer.
    _refuse_a_broken_gateway_contract(database, old_codex)
    revision = database.revision(old_brain)
    _safe_failure(
        revision != config.expected_alembic_revision,
        f"deployed Alembic revision {revision} differs from the declared "
        f"{config.expected_alembic_revision}",
    )
    scope_bounded = database.codex_scope_is_bounded(old_codex)
    _safe_failure(not scope_bounded, "codex_ro privileges are not bounded")
    return {
        "alembic_revision": revision,
        "apply": False,
        "consumer_files_valid": len(snapshots),
        "codex_scope_bounded": scope_bounded,
        "gateway_port": _GATEWAY_PORT,
        "mode_hardening_required": snapshots[1].mode != 0o600,
        "old_credentials_valid": old_valid,
        "status": "preflight_ok",
    }


def _remove_journal(path: Path) -> None:
    try:
        path.unlink()
        _fsync_directory(path.parent)
    except (OSError, RotationError):
        raise RotationError("credential journal cleanup failed") from None


def _apply_or_resume(
    config: RotationConfig,
    state: RotationState,
    database: CredentialDatabase,
    privileged_installer: PrivilegedInstaller,
    gateway_probe: GatewayProbe,
    journal: Path,
) -> dict[str, bool | str | int]:
    if config.resume and config.consumers_recreated_confirmed:
        _prove_new_generation(database, state, config.expected_alembic_revision)
        try:
            bearer_proven = gateway_probe.prove(state.old_bearer, state.new_bearer)
        except Exception:
            raise RotationError("gateway bearer verification failed") from None
        _safe_failure(not bearer_proven, "gateway bearer verification failed")
        _prove_installed_generation(config, state)
        _remove_journal(journal)
        return {
            "alembic_revision": config.expected_alembic_revision,
            "bearer_installed": True,
            "codex_scope_bounded": True,
            "consumer_files_installed": 5,
            "database_credentials_rotated": True,
            "new_bearer_valid": True,
            "new_credentials_valid": True,
            "old_bearer_refused": True,
            "old_credentials_refused": True,
            "status": "rotated",
        }

    apply_failed = False
    try:
        generation = _credential_generation(database, state)
        if generation == "old":
            database.rotate(
                state.old_brain,
                state.new_brain,
                state.old_codex,
                state.new_codex,
            )
        _install_generation(config, state, privileged_installer)
        _prove_new_generation(database, state, config.expected_alembic_revision)
    except Exception:
        apply_failed = True

    if apply_failed:
        rollback_failed = False
        try:
            _rollback_state(config, state, database, privileged_installer)
        except Exception:
            rollback_failed = True
        if rollback_failed:
            raise RotationError(
                "cutover failed; rollback incomplete; private journal retained"
            ) from None
        raise RotationError(
            "cutover failed; previous generation restored; resume required"
        ) from None
    return {
        "alembic_revision": config.expected_alembic_revision,
        "bearer_installed": True,
        "codex_scope_bounded": True,
        "consumer_files_installed": 5,
        "database_credentials_rotated": True,
        "new_credentials_valid": True,
        "old_credentials_refused": True,
        "status": "awaiting_consumer_recreation",
    }


class AsyncpgCredentialDatabase:
    """Live PostgreSQL adapter using new TCP connections for every proof."""

    def __init__(self, dsn: URL) -> None:
        self.host = dsn.host or ""
        self.port = dsn.port or 5432
        self.database = dsn.database or ""
        sslmode = dsn.query.get("sslmode")
        _safe_failure(
            sslmode is not None
            and (not isinstance(sslmode, str) or sslmode not in _POSTGRES_SSLMODES),
            "consumer PostgreSQL DSN is invalid",
        )
        self.sslmode = sslmode

    async def _connect(self, role: str, password: str) -> asyncpg.Connection[Any]:
        return await asyncpg.connect(
            user=role,
            password=password,
            host=self.host,
            port=self.port,
            database=self.database,
            timeout=5,
            ssl=self.sslmode,
            server_settings={"application_name": "brain-credential-cutover"},
        )

    def probe(self, role: str, password: str) -> bool:
        async def check() -> bool:
            connection: asyncpg.Connection[Any] | None = None
            try:
                connection = await self._connect(role, password)
                return bool(await connection.fetchval("SELECT 1") == 1)
            except asyncpg.InvalidPasswordError:
                return False
            except Exception:
                raise RotationError("PostgreSQL credential verification failed") from None
            finally:
                if connection is not None:
                    await connection.close()

        return asyncio.run(check())

    def rotate(
        self,
        current_brain: str,
        next_brain: str,
        current_codex: str,
        next_codex: str,
    ) -> None:
        async def change() -> None:
            connection: asyncpg.Connection[Any] | None = None
            try:
                connection = await self._connect("brain", current_brain)
                current_codex_valid = await connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'codex_ro')"
                )
                if not current_codex_valid:
                    raise RotationError("codex_ro role is missing")
                quoted_brain = await connection.fetchval("SELECT quote_literal($1)", next_brain)
                quoted_codex = await connection.fetchval("SELECT quote_literal($1)", next_codex)
                async with connection.transaction():
                    await connection.execute(f"ALTER ROLE brain PASSWORD {quoted_brain}")
                    await connection.execute(f"ALTER ROLE codex_ro PASSWORD {quoted_codex}")
            except RotationError:
                raise
            except Exception:
                raise RotationError("PostgreSQL credential rotation failed") from None
            finally:
                if connection is not None:
                    await connection.close()

        _safe_failure(
            not self.probe("codex_ro", current_codex),
            "configured codex_ro credential is not accepted",
        )
        asyncio.run(change())

    def revision(self, brain_password: str) -> str:
        async def current() -> str:
            connection: asyncpg.Connection[Any] | None = None
            try:
                connection = await self._connect("brain", brain_password)
                value = await connection.fetchval("SELECT version_num FROM alembic_version")
                if not isinstance(value, str):
                    raise RotationError("Alembic revision is unavailable")
                return value
            except RotationError:
                raise
            except Exception:
                raise RotationError("Alembic revision verification failed") from None
            finally:
                if connection is not None:
                    await connection.close()

        return asyncio.run(current())

    def codex_scope_is_bounded(self, codex_password: str) -> bool:
        async def check() -> bool:
            connection: asyncpg.Connection[Any] | None = None
            try:
                connection = await self._connect("codex_ro", codex_password)
                row = await connection.fetchrow(
                    "WITH expected(name) AS (SELECT unnest($1::text[])) "
                    "SELECT "
                    "(SELECT count(*) FROM expected "
                    " WHERE has_table_privilege(current_user, name, 'SELECT')) = $2 "
                    "AS all_views, "
                    "EXISTS ("
                    " SELECT 1 FROM information_schema.tables "
                    " WHERE table_schema = 'public' AND table_type = 'BASE TABLE' "
                    " AND has_table_privilege("
                    "   current_user, format('%I.%I', table_schema, table_name), 'SELECT'"
                    " )"
                    ") AS base_table_read",
                    list(_CODEX_GATEWAY_VIEWS),
                    len(_CODEX_GATEWAY_VIEWS),
                )
                return bool(row and row["all_views"] and not row["base_table_read"])
            except asyncpg.InvalidPasswordError:
                return False
            except Exception:
                raise RotationError("codex_ro scope verification failed") from None
            finally:
                if connection is not None:
                    await connection.close()

        return asyncio.run(check())

    def missing_gateway_contract(self, codex_password: str) -> tuple[str, ...]:
        async def check() -> tuple[str, ...]:
            connection: asyncpg.Connection[Any] | None = None
            try:
                connection = await self._connect("codex_ro", codex_password)
                views, columns = _gateway_contract_arrays(_CODEX_GATEWAY_CONTRACT)
                row = await connection.fetchrow(_GATEWAY_CONTRACT_PROOF, views, columns)
                if row is None:
                    raise RotationError("Codex gateway contract verification failed")
                return tuple(row["missing_views"]) + tuple(row["missing_columns"])
            # Divergence DÉLIBÉRÉE avec codex_scope_is_bounded, qui rend False
            # sur un mot de passe refusé : ici un refus d'authentification ne
            # doit JAMAIS se lire comme « les vues manquent ». L'identité est
            # de toute façon déjà prouvée en amont, par _preflight comme par
            # _prove_new_generation.
            except Exception:
                raise RotationError("Codex gateway contract verification failed") from None
            finally:
                if connection is not None:
                    await connection.close()

        return asyncio.run(check())


class SudoShrikInstaller:
    """Publish only the fixed Shrik file through one root-owned helper."""

    def __init__(self, private_dir: Path) -> None:
        self.private_dir = private_dir

    @staticmethod
    def _environment() -> dict[str, str]:
        return {
            key: value
            for key in _SAFE_SUBPROCESS_ENV_KEYS
            if (value := os.environ.get(key)) is not None
        }

    def preflight(self, target: Path) -> None:
        _safe_failure(target != _SHRIK_ENV, "Shrik environment target is not canonical")
        result = subprocess.run(
            ["sudo", "-n", _SHRIK_CONTROL, "--check"],
            capture_output=True,
            check=False,
            env=self._environment(),
            text=True,
        )
        _safe_failure(result.returncode != 0, "non-interactive Shrik privilege is unavailable")

    def install(
        self,
        target: Path,
        payload: bytes,
        *,
        mode: int,
        uid: int,
        gid: int,
    ) -> None:
        _safe_failure(
            target != _SHRIK_ENV or mode != 0o640 or uid != 0,
            "privileged Shrik request is not canonical",
        )
        temporary = self.private_dir / _SHRIK_STAGE_NAME
        _atomic_write(temporary, payload, 0o600)
        failed = False
        try:
            result = subprocess.run(
                ["sudo", "-n", _SHRIK_CONTROL, "--publish"],
                capture_output=True,
                check=False,
                env=self._environment(),
                text=True,
            )
            failed = result.returncode != 0
        except Exception:
            failed = True
        finally:
            try:
                temporary.unlink(missing_ok=True)
                _fsync_directory(self.private_dir)
            except OSError:
                failed = True
        _safe_failure(failed, "privileged Shrik installation failed")


_GATEWAY_PROBE_SCRIPT = r"""
import json
import sys
import urllib.error
import urllib.request

payload = json.load(sys.stdin)
base = "http://127.0.0.1:9211/api/killswitches"

def status(token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(base, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
            return response.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code

print(json.dumps({
    "anonymous": status(""),
    "new": status(payload["new"]),
    "old": status(payload["old"]),
}, sort_keys=True))
"""


class DockerGatewayProbe:
    """Probe the private gateway from inside its own network namespace."""

    def __init__(self, brain_root: Path) -> None:
        self.brain_root = brain_root

    def prove(self, old_token: str, new_token: str) -> bool:
        failed = False
        result: subprocess.CompletedProcess[str] | None = None
        try:
            result = subprocess.run(
                [
                    "docker",
                    "compose",
                    "--project-name",
                    "brain-v42",
                    "-f",
                    "docker-compose.yml",
                    "exec",
                    "-T",
                    "brain-codex-gateway",
                    "python",
                    "-c",
                    _GATEWAY_PROBE_SCRIPT,
                ],
                input=json.dumps({"new": new_token, "old": old_token}),
                capture_output=True,
                check=False,
                text=True,
                cwd=self.brain_root,
                env={
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("COMPOSE_")
                },
            )
            failed = result.returncode != 0
            statuses = json.loads(result.stdout) if not failed else {}
            failed = failed or statuses != {"anonymous": 401, "new": 200, "old": 401}
        except Exception:
            failed = True
        _safe_failure(failed or result is None, "gateway bearer verification failed")
        return True


def _database_from_config(config: RotationConfig) -> AsyncpgCredentialDatabase:
    snapshot = _read_snapshot(
        _brain_env(config),
        allowed_modes={0o600},
        required=True,
        require_operator_owner=True,
    )
    values = _managed_values(snapshot.content, {"POSTGRES_URL"})
    _safe_failure("POSTGRES_URL" not in values, "consumer environment is missing managed keys")
    return AsyncpgCredentialDatabase(_validated_dsn(values["POSTGRES_URL"], expected_role="brain"))


def run_rotation(
    config: RotationConfig,
    *,
    database: CredentialDatabase | None = None,
    privileged_installer: PrivilegedInstaller | None = None,
    gateway_probe: GatewayProbe | None = None,
) -> dict[str, bool | str | int]:
    """Run a read-only preflight, one apply phase, resume/finalize, or rollback."""
    _validate_config_paths(config)
    database = database or _database_from_config(config)
    privileged_installer = privileged_installer or SudoShrikInstaller(config.private_dir)
    gateway_probe = gateway_probe or DockerGatewayProbe(config.brain_root)

    if not config.apply and not config.rollback:
        _safe_failure(config.resume, "--resume requires --apply")
        return _preflight(config, database, privileged_installer)

    confirmations = config.consumers_stopped_confirmed and config.rollback_preflight_confirmed
    _safe_failure(not confirmations, "operator confirmations required")
    if config.consumers_recreated_confirmed:
        _safe_failure(not config.resume, "consumer recreation proof requires --resume")
    _ensure_private_directory(config.private_dir)
    journal = config.private_dir / _JOURNAL_NAME
    with _exclusive_lock(config.private_dir / _LOCK_NAME):
        if config.resume or config.rollback:
            _safe_failure(not journal.exists(), "credential rotation journal is required")
            state = _load_state(journal, config)
        else:
            _preflight(config, database, privileged_installer)
            state = _prepare_state(config, journal)

        if config.rollback:
            _rollback_state(config, state, database, privileged_installer)
            _remove_journal(journal)
            return {
                "consumer_files_restored": 5,
                "database_credentials_restored": True,
                "status": "rolled_back",
            }
        return _apply_or_resume(
            config,
            state,
            database,
            privileged_installer,
            gateway_probe,
            journal,
        )


def _alembic_revision(raw: str) -> str:
    """Head déclaré par l'opérateur, validé fail-closed.

    Refuser au parseur plutôt qu'au préflight : l'argument est recopié dans un
    message d'erreur et dans le contrat de sortie JSON, et une révision n'a jamais
    d'autre forme que celle d'un identifiant Alembic.
    """
    candidate = raw.strip()
    if not candidate or len(candidate) > 64 or not _REVISION_SHAPE.fullmatch(candidate):
        raise argparse.ArgumentTypeError("expected an Alembic revision identifier")
    return candidate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expected-alembic-revision",
        required=True,
        type=_alembic_revision,
        help=(
            "Head Alembic mesuré juste avant la procédure, jamais recopié d'un "
            "runbook : select version_num from alembic_version"
        ),
    )
    parser.add_argument("--brain-root", required=True, type=Path)
    parser.add_argument("--red-root", required=True, type=Path)
    parser.add_argument("--private-dir", required=True, type=Path)
    parser.add_argument("--shrik-env", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--consumers-stopped-confirmed", action="store_true")
    parser.add_argument("--rollback-preflight-confirmed", action="store_true")
    parser.add_argument("--consumers-recreated-confirmed", action="store_true")
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return _parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = RotationConfig(
        brain_root=args.brain_root,
        red_root=args.red_root,
        private_dir=args.private_dir,
        shrik_env=args.shrik_env,
        apply=args.apply,
        resume=args.resume,
        rollback=args.rollback,
        consumers_stopped_confirmed=args.consumers_stopped_confirmed,
        rollback_preflight_confirmed=args.rollback_preflight_confirmed,
        consumers_recreated_confirmed=args.consumers_recreated_confirmed,
        expected_alembic_revision=args.expected_alembic_revision,
    )
    try:
        result = run_rotation(config)
    except RotationError as exc:
        print(json.dumps({"error": str(exc), "status": "error"}, sort_keys=True))
        return 2
    except Exception:
        print(
            json.dumps(
                {"error": "credential cutover failed", "status": "error"},
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
