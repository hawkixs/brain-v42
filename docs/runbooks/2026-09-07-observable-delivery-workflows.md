# Immutable delivery release and production canary

This runbook prepares and deploys brain-v42 `0.5.0` from one immutable source
revision. It covers artifact construction, recovery proof, all-writer cutover,
schema 053, the delivery observer, and a documentation-only production canary.

Nothing in this file proves that a host was changed. The operator must create a
separate, dated production receipt from evidence captured during an authorized
window. Add a link to that receipt only after the receipt exists.

## Authority and stop conditions

The build and read-only inventory may run before the change window. Database,
Docker, systemd, GitHub, and Brain mutations require explicit production
authority. Before the window, identify the operator, source SHA, change record,
rollback owner, and evidence directory.

Stop at the first failed command, changed identity, stale observation, unexpected
writer, health failure, or receipt mismatch. Preserve the evidence and diagnose
that failure. Do not stack a speculative fix, downgrade schema 053, start a failed
Dream or model-liveness job to test it, bypass protected checks, or reuse a stale
preflight result.

The final source must be the merged, CI-tested `main` revision whose tree matches
the tested pull-request revision. A branch name, configured SHA, successful build,
or green CI result does not attest a running process. The deployment preflight
binds the live interpreter, imports, source files, manifest, schema, health route,
and effective systemd configuration.

## Fixed paths and operator inputs

Use one absolute Python 3.12 interpreter selected during preparation. Do not
assume that `python3.12` resolves through the operator's `PATH`.

```bash
set -Eeuo pipefail
set +x
umask 077

VERSION=0.5.0
SOURCE_SHA='<full merged and CI-tested 40-hex SHA>'
BUILD_PYTHON='<absolute path to the selected Python 3.12 interpreter>'
WINDOW_ID='<unique UTC timestamp or change-record ID>'
RELEASE_PARENT="$HOME/.local/share/brain-v42/releases"
RELEASE="$RELEASE_PARENT/$SOURCE_SHA"
PRIVATE_CONFIG="$HOME/.config/brain-v42"
CANONICAL_ENV="$HOME/hawkixs_infra/git_repo/brain_v42/.env"
OBSERVER_ENV="$PRIVATE_CONFIG/delivery-observer.env"
MCP_DELIVERY_ENV="$PRIVATE_CONFIG/delivery-mcp.env"
PREFLIGHT_CONFIG="$PRIVATE_CONFIG/delivery-preflight-$SOURCE_SHA.json"
CANARY_PREFLIGHT_CONFIG="$PRIVATE_CONFIG/delivery-preflight-canary-$SOURCE_SHA.json"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
EVIDENCE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/brain-v42-delivery/$SOURCE_SHA/$WINDOW_ID"

case "$SOURCE_SHA" in
  (????????????????????????????????????????) ;;
  (*) printf '%s\n' 'refusing: SOURCE_SHA must contain 40 characters' >&2; exit 2 ;;
esac
case "$SOURCE_SHA" in
  (*[!0-9a-f]*) printf '%s\n' 'refusing: SOURCE_SHA must be lowercase hexadecimal' >&2; exit 2 ;;
esac
case "$BUILD_PYTHON" in (/*) ;; (*) exit 2 ;; esac
case "$WINDOW_ID" in (''|*[!A-Za-z0-9._-]*) exit 2 ;; esac
test -x "$BUILD_PYTHON" && test ! -L "$BUILD_PYTHON"
test "$($BUILD_PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" = 3.12
test "${RELEASE%/}" = "$HOME/.local/share/brain-v42/releases/$SOURCE_SHA"
if test -e "$PRIVATE_CONFIG" || test -L "$PRIVATE_CONFIG"; then
  test -d "$PRIVATE_CONFIG" && test ! -L "$PRIVATE_CONFIG"
  test "$(stat -c '%u:%a' "$PRIVATE_CONFIG")" = "$(id -u):700"
else
  install -d -m 0700 "$PRIVATE_CONFIG"
fi
test ! -e "$EVIDENCE_DIR" && test ! -L "$EVIDENCE_DIR"
install -d -m 0700 "$EVIDENCE_DIR"
readonly VERSION SOURCE_SHA BUILD_PYTHON WINDOW_ID RELEASE_PARENT RELEASE PRIVATE_CONFIG \
  CANONICAL_ENV OBSERVER_ENV MCP_DELIVERY_ENV PREFLIGHT_CONFIG CANARY_PREFLIGHT_CONFIG \
  USER_UNIT_DIR EVIDENCE_DIR

wait_for_mcp_health() {
  local output="$1"
  local output_dir
  local temporary
  local attempt
  case "$output" in
    (/*/*) output_dir="${output%/*}" ;;
    (/*) output_dir=/ ;;
    (*) return 2 ;;
  esac
  test -d "$output_dir" && test ! -L "$output_dir" || return 1
  if test -e "$output" || test -L "$output"; then
    test -f "$output" && test ! -L "$output" || return 1
  fi
  temporary="$(mktemp "$output_dir/.mcp-health.XXXXXX")" || return 1
  if ! chmod 0600 "$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  for attempt in 1 2 3 4 5; do
    if curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8765/health \
      --output "$temporary"; then
      if test -e "$output" || test -L "$output"; then
        if ! test -f "$output" || test -L "$output"; then
          rm -f -- "$temporary"
          return 1
        fi
      fi
      if mv -f -- "$temporary" "$output"; then
        return 0
      fi
      rm -f -- "$temporary"
      return 1
    fi
    sleep 1
  done
  rm -f -- "$temporary"
  return 1
}
readonly -f wait_for_mcp_health
```

The release layout is:

```text
~/.local/share/brain-v42/releases/<full-source-sha>/
├── artifacts/
│   ├── brain-v42.tar.gz
│   ├── brain_v42-0.5.0-py3-none-any.whl
│   ├── runtime-requirements.txt
│   └── uv.lock
├── brain-v42/                     retained source from the same SHA
├── systemd/                       isolated observer render
├── systemd-dropins/               reviewed immutable path overrides
├── venv/                          copied Python 3.12 interpreter
└── delivery-release.json          non-secret hash manifest
```

There is no mutable `current` link. The wheel supplies `brain_v42.*`, Alembic,
and the observer. The retained archive supplies `.mcp.json`, Dream, and root
scripts that are outside the wheel.

## Writer and trigger inventory

Remeasure every row before the window. The last read-only inventory established
the following baseline; any difference requires an explicit explanation before
continuing.

| Writer surface | Immutable launch required | Baseline state | Trigger handled separately | Dormant `must_be_active` |
| --- | --- | --- | --- | --- |
| `brain-mcp-http.service` | `RELEASE/venv/bin/python -m brain_v42.mcp.server --http-server` | active | `brain-mcp-http-watchdog.timer` active | `true` |
| `brain-metrics.service` | `RELEASE/venv/bin/python -m brain_v42.metrics` | active | none | `true` |
| `brain-mcp-reaper.service` | `RELEASE/venv/bin/python -m brain_v42.maintenance.reap_stale_mcp --max-age-hours <preserved value>` | inactive oneshot | `brain-mcp-reaper.timer` active | `false` |
| `brain-v42-dream.service` | `/bin/bash RELEASE/brain-v42/scripts/dream.sh brain-v42` | failed, no process | `brain-v42-dream.timer` active | `false` |
| `brain-v42-model-liveness.service` | `RELEASE/venv/bin/python -m scripts.probe_model_liveness` | failed, no process | `brain-v42-model-liveness.timer` active | `false` |
| `brain-v42-automation.service` | `RELEASE/venv/bin/python -m brain_v42.automation` | inactive and disabled | none | `false` |
| `brain-v42-graph-recon.service` | `RELEASE/venv/bin/python -m brain_v42.scripts.rebuild_graph_projection` | inactive and disabled | `brain-v42-graph-recon.timer` inactive and disabled | `false` |
| `brain-v42-delivery-observer.service` | `RELEASE/venv/bin/python -m brain_v42.delivery_observer --env-file OBSERVER_ENV` | absent before first rollout | none | `false`, then `true` |

The watchdog is a trigger, not an application writer. Freeze it before stopping
MCP and restore it only after MCP is healthy. Preserve every unit's existing
working directory, private `EnvironmentFile` references, arguments, enablement,
and unrelated drop-ins. Metrics has no effective `EnvironmentFile`; do not give
it the observer's environment. Dream stays failed and out of scope for repair.

Capture safe state without printing environment values or process arguments:

```bash
WRITERS=(
  brain-mcp-http.service
  brain-metrics.service
  brain-mcp-reaper.service
  brain-v42-dream.service
  brain-v42-model-liveness.service
  brain-v42-automation.service
  brain-v42-graph-recon.service
  brain-v42-delivery-observer.service
)
TRIGGERS=(
  brain-mcp-http-watchdog.timer
  brain-mcp-reaper.timer
  brain-v42-dream.timer
  brain-v42-model-liveness.timer
  brain-v42-graph-recon.timer
)
: > "$EVIDENCE_DIR/unit-state.before"
chmod 0600 "$EVIDENCE_DIR/unit-state.before"
for unit in "${WRITERS[@]}" "${TRIGGERS[@]}"; do
  enabled="$(systemctl --user is-enabled "$unit" 2>/dev/null || true)"
  active="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" \
    >> "$EVIDENCE_DIR/unit-state.before"
  systemctl --user show "$unit" --no-pager \
    -p LoadState -p ActiveState -p UnitFileState -p MainPID \
    -p FragmentPath -p DropInPaths \
    >> "$EVIDENCE_DIR/unit-properties.before" 2>&1 || true
done
chmod 0600 "$EVIDENCE_DIR/unit-properties.before"
```

Inspect effective `ExecStart`, `WorkingDirectory`, controlled environment names,
and private-file paths through a private filtered review. Never copy complete
commands, DSNs, environment values, `/proc/*/environ`, or private file contents
into a receipt or issue.

## Build the exact release

Run this section from a clean checkout at `SOURCE_SHA`. Version `0.5.0` must
already be committed in `pyproject.toml` and the root editable entry in `uv.lock`.
The operator must not edit metadata after the source revision is chosen.

The accepted host inventory has no active OpenTelemetry settings and lacks the
tracing packages, so the default runtime export preserves existing capabilities.
Recheck safe installed metadata and the effective MCP and metrics setting names
before this build. If tracing is enabled, stop and select the matching locked
extra; do not resolve optional dependencies during host installation.

```bash
test "$(git rev-parse --verify HEAD^{commit})" = "$SOURCE_SHA"
test -z "$(git status --porcelain)"
test "$(git show HEAD:pyproject.toml | sed -n 's/^version = "\([^"]*\)"$/\1/p' | head -1)" = "$VERSION"
test "$(git show HEAD:uv.lock | sed -n '/^name = "brain-v42"$/,+1{s/^version = "\([^"]*\)"$/\1/p;}' | head -1)" = "$VERSION"

install -d -m 0700 "$RELEASE_PARENT"
test ! -e "$RELEASE" && test ! -L "$RELEASE"
mkdir -m 0700 "$RELEASE"
mkdir -m 0700 "$RELEASE/artifacts"

git archive --format=tar.gz --prefix=brain-v42/ "$SOURCE_SHA" \
  > "$RELEASE/artifacts/brain-v42.tar.gz"
git archive --format=tar --prefix=brain-v42/ "$SOURCE_SHA" \
  | tar -xf - -C "$RELEASE"
cd "$RELEASE/brain-v42"
install -m 0600 uv.lock "$RELEASE/artifacts/uv.lock"

uv export --locked --no-dev --no-emit-project --format requirements-txt \
  -o "$RELEASE/artifacts/runtime-requirements.txt"
uv build --wheel --python "$BUILD_PYTHON" --out-dir "$RELEASE/artifacts"
WHEEL="$RELEASE/artifacts/brain_v42-0.5.0-py3-none-any.whl"
test -f "$WHEEL" && test ! -L "$WHEEL"

"$BUILD_PYTHON" -m venv --copies "$RELEASE/venv"
test -x "$RELEASE/venv/bin/python" && test ! -L "$RELEASE/venv/bin/python"
uv pip sync --python "$RELEASE/venv/bin/python" --require-hashes \
  --link-mode copy "$RELEASE/artifacts/runtime-requirements.txt"
uv pip install --python "$RELEASE/venv/bin/python" --no-deps \
  --link-mode copy "$WHEEL"
readonly WHEEL
```

Generate the complete manifest. It maps every installed `brain_v42/` wheel file
to the same bytes in the source archive and maps every retained `scripts/` file
plus `.mcp.json` to the extracted source tree.

```bash
env RELEASE="$RELEASE" SOURCE_SHA="$SOURCE_SHA" VERSION="$VERSION" \
  "$RELEASE/venv/bin/python" -I - <<'PY'
import hashlib
import json
import os
import tarfile
import zipfile
from pathlib import Path

release = Path(os.environ["RELEASE"])
source_sha = os.environ["SOURCE_SHA"]
version = os.environ["VERSION"]
artifacts = release / "artifacts"
wheel = artifacts / "brain_v42-0.5.0-py3-none-any.whl"


def digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def record(path: Path) -> dict[str, str]:
    return {
        "path": str(path.relative_to(release)),
        "sha256": digest(path.read_bytes()),
    }


manifest = {
    "schema_version": 1,
    "source_sha": source_sha,
    "minimum_guarded_sha": "fcc9328ff6e7f061879af2540c69717fc3061434",
    "version": version,
    "source_archive": record(artifacts / "brain-v42.tar.gz"),
    "wheel": record(wheel),
    "uv_lock": record(artifacts / "uv.lock"),
    "interpreter": record(release / "venv/bin/python"),
    "package_payload": [],
    "source_payload": [],
}

with (
    tarfile.open(artifacts / "brain-v42.tar.gz", "r:gz") as source,
    zipfile.ZipFile(wheel) as distribution,
):
    members = {item.name: item for item in source.getmembers() if item.isfile()}
    for wheel_path in sorted(
        name
        for name in distribution.namelist()
        if name.startswith("brain_v42/") and not name.endswith("/")
    ):
        if wheel_path == "brain_v42/alembic.ini":
            source_path = "brain-v42/alembic.ini"
        elif wheel_path.startswith("brain_v42/alembic/"):
            source_path = "brain-v42/" + wheel_path.removeprefix("brain_v42/")
        else:
            source_path = "brain-v42/src/" + wheel_path
        member = members[source_path]
        source_stream = source.extractfile(member)
        assert source_stream is not None
        source_body = source_stream.read()
        wheel_body = distribution.read(wheel_path)
        assert source_body == wheel_body
        installed_path = Path("venv/lib/python3.12/site-packages") / wheel_path
        assert (release / installed_path).read_bytes() == wheel_body
        manifest["package_payload"].append(
            {
                "source_path": source_path,
                "wheel_path": wheel_path,
                "installed_path": str(installed_path),
                "sha256": digest(wheel_body),
            }
        )
    for archive_path, member in sorted(members.items()):
        if archive_path.startswith("brain-v42/scripts/") or archive_path == "brain-v42/.mcp.json":
            source_stream = source.extractfile(member)
            assert source_stream is not None
            body = source_stream.read()
            assert (release / archive_path).read_bytes() == body
            manifest["source_payload"].append(
                {
                    "archive_path": archive_path,
                    "extracted_path": archive_path,
                    "sha256": digest(body),
                }
            )

target = release / "delivery-release.json"
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(manifest, stream, sort_keys=True, separators=(",", ":"))
PY
```

Run the smoke from outside the checkout and retained source tree. It starts no
service and proves no production process.

```bash
SMOKE_DIR="$(mktemp -d)"
(
  cd "$SMOKE_DIR"
  env RELEASE="$RELEASE" "$RELEASE/venv/bin/python" -I - <<'PY'
import importlib.metadata
import os
from pathlib import Path

import brain_v42
from brain_v42.release import shipped_alembic_head

release = Path(os.environ["RELEASE"]).resolve()
module = Path(brain_v42.__file__).resolve()
assert module.is_relative_to(release / "venv")
assert importlib.metadata.version("brain_v42") == "0.5.0"
assert shipped_alembic_head() == "053"
PY
  "$RELEASE/venv/bin/python" -m brain_v42.delivery_observer --help
  "$RELEASE/venv/bin/python" "$RELEASE/brain-v42/scripts/check_delivery_deployment.py" --help
  "$RELEASE/venv/bin/python" "$RELEASE/brain-v42/scripts/verify_delivery_canary.py" --help
)
chmod -R go-w "$RELEASE"
```

Retain the release, archive, wheel, exported requirements, lock, and manifest.
Never harden system paths, shared data, or the uv cache with the release command.

## Private configuration

The dedicated observer file already exists on this host with a user-provided PAT
or complete GitHub App fields. The canonical application file already contains
the selected `POSTGRES_URL`. Before the window, require both files to be private,
regular, and owned by the service user. The following preparation reads only
those two selected files in an empty environment, preserves the existing observer
credential mode, writes every required observer key atomically, and proves the
observer DSN is exactly the parsed canonical value. It never prints either value
or asks for another credential.

```bash
test -f "$CANONICAL_ENV" && test ! -L "$CANONICAL_ENV"
test "$(stat -c '%u:%a' "$CANONICAL_ENV")" = "$(id -u):600"
test -f "$OBSERVER_ENV" && test ! -L "$OBSERVER_ENV"
test "$(stat -c '%u:%a' "$OBSERVER_ENV")" = "$(id -u):600"

if ! env -i HOME="$HOME" CANONICAL_ENV="$CANONICAL_ENV" OBSERVER_ENV="$OBSERVER_ENV" \
  "$RELEASE/venv/bin/python" -I - <<'PY'
import io
import os
import stat
import tempfile
from pathlib import Path

from dotenv import dotenv_values

from brain_v42.delivery_observer.auth import (
    load_private_environment,
    read_private_file,
)
from brain_v42.delivery_observer.config import load_observer_settings

COMMON = {
    "BRAIN_DELIVERY_ENABLED": "true",
    "BRAIN_DELIVERY_REPOSITORY_REGISTRY": (
        '{"brain-v42":{"1337360966":"hawkixs/brain-v42"}}'
    ),
    "BRAIN_DELIVERY_POLL_SECONDS": "60",
    "BRAIN_DELIVERY_FRESHNESS_SECONDS": "600",
    "BRAIN_DELIVERY_REQUEST_BUDGET_PER_MINUTE": "40",
    "BRAIN_DELIVERY_MAX_CONCURRENT_REQUESTS": "2",
    "BRAIN_DELIVERY_REQUEST_TIMEOUT_SECONDS": "10",
    "BRAIN_DELIVERY_MAX_RESPONSE_BYTES": "4194304",
    "BRAIN_DELIVERY_GITHUB_API_ORIGIN": "https://api.github.com",
    "BRAIN_DELIVERY_GITHUB_API_VERSION": "2026-03-10",
}
TOKEN = {"BRAIN_DELIVERY_GITHUB_TOKEN"}
APP = {
    "BRAIN_DELIVERY_GITHUB_APP_ID",
    "BRAIN_DELIVERY_GITHUB_INSTALLATION_ID",
    "BRAIN_DELIVERY_GITHUB_PRIVATE_KEY_PATH",
}


def canonical_postgres(path: Path) -> str:
    text = read_private_file(path).decode("utf-8")
    parsed = dotenv_values(stream=io.StringIO(text), interpolate=False)
    value = parsed.get("POSTGRES_URL")
    if not isinstance(value, str) or not value:
        raise ValueError
    return value


def validate(path: Path, desired: dict[str, str], postgres: str) -> None:
    selected = load_private_environment(path)
    if selected != desired or selected["BRAIN_DELIVERY_POSTGRES_URL"] != postgres:
        raise ValueError
    settings = load_observer_settings(path)
    assert settings.enabled is True
    assert settings.postgres_url is not None
    assert settings.repositories_for("brain-v42") == {1337360966: "hawkixs/brain-v42"}
    assert settings.poll_seconds == 60
    assert settings.freshness_seconds == 600
    assert settings.request_budget_per_minute == 40
    assert settings.max_concurrent_requests == 2
    assert settings.request_timeout_seconds == 10
    assert settings.max_response_bytes == 4194304
    assert settings.github_api_origin == "https://api.github.com"
    assert settings.github_api_version == "2026-03-10"


try:
    canonical = Path(os.environ["CANONICAL_ENV"])
    observer = Path(os.environ["OBSERVER_ENV"])
    original = load_private_environment(observer)
    original_stat = observer.stat(follow_symlinks=False)
    selected_token = TOKEN & original.keys()
    selected_app = APP & original.keys()
    if (selected_token and selected_app) or (
        not selected_token and selected_app != APP
    ) or (not selected_token and not selected_app):
        raise ValueError

    desired = dict(COMMON)
    postgres = canonical_postgres(canonical)
    desired["BRAIN_DELIVERY_POSTGRES_URL"] = postgres
    for key in sorted(selected_token or selected_app):
        desired[key] = original[key]
    if any(not value or any(char in value for char in "\r\n\0") for value in desired.values()):
        raise ValueError

    descriptor, temporary_name = tempfile.mkstemp(
        dir=observer.parent,
        prefix=".delivery-observer.env.",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            for key, value in desired.items():
                stream.write(f"{key}={value}\n")
            stream.flush()
            os.fsync(stream.fileno())
        validate(temporary, desired, postgres)
        current_stat = observer.stat(follow_symlinks=False)
        unchanged = (
            original_stat.st_dev,
            original_stat.st_ino,
            original_stat.st_mtime_ns,
            original_stat.st_size,
            stat.S_IMODE(original_stat.st_mode),
            original_stat.st_uid,
        ) == (
            current_stat.st_dev,
            current_stat.st_ino,
            current_stat.st_mtime_ns,
            current_stat.st_size,
            stat.S_IMODE(current_stat.st_mode),
            current_stat.st_uid,
        )
        if not unchanged or stat.S_IMODE(current_stat.st_mode) != 0o600:
            raise ValueError
        os.replace(temporary, observer)
        directory = os.open(observer.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
except Exception:
    raise SystemExit(1) from None
PY
then
  printf '%s\n' 'refusing: private observer preparation failed' >&2
  exit 2
fi
```

Use either the dedicated PAT field or all three GitHub App fields; never combine
partial App credentials with a PAT. The repository registry remains
numeric-authoritative. The executable preparation attests the following complete
selected-file shape; ambient variables and loader defaults cannot supply a
missing key.

```text
BRAIN_DELIVERY_ENABLED=true
BRAIN_DELIVERY_POSTGRES_URL=<explicit production PostgreSQL URL>
BRAIN_DELIVERY_REPOSITORY_REGISTRY={"brain-v42":{"1337360966":"hawkixs/brain-v42"}}
BRAIN_DELIVERY_POLL_SECONDS=60
BRAIN_DELIVERY_FRESHNESS_SECONDS=600
BRAIN_DELIVERY_REQUEST_BUDGET_PER_MINUTE=40
BRAIN_DELIVERY_MAX_CONCURRENT_REQUESTS=2
BRAIN_DELIVERY_REQUEST_TIMEOUT_SECONDS=10
BRAIN_DELIVERY_MAX_RESPONSE_BYTES=4194304
BRAIN_DELIVERY_GITHUB_API_ORIGIN=https://api.github.com
BRAIN_DELIVERY_GITHUB_API_VERSION=2026-03-10
BRAIN_DELIVERY_GITHUB_TOKEN=<dedicated observer credential>
```

For GitHub App authentication, omit `BRAIN_DELIVERY_GITHUB_TOKEN` and set
`BRAIN_DELIVERY_GITHUB_APP_ID`, `BRAIN_DELIVERY_GITHUB_INSTALLATION_ID`, and
`BRAIN_DELIVERY_GITHUB_PRIVATE_KEY_PATH` together. The key path points to a
separate private regular file. Do not copy an interactive agent credential.

Keep the MCP mutation switch in its own non-secret private file:

```bash
test ! -e "$MCP_DELIVERY_ENV" && test ! -L "$MCP_DELIVERY_ENV"
printf '%s\n' 'BRAIN_DELIVERY_ENABLED=false' > "$MCP_DELIVERY_ENV"
chmod 0600 "$MCP_DELIVERY_ENV"
test "$(stat -c '%u:%a' "$MCP_DELIVERY_ENV")" = "$(id -u):600"
test -f "$OBSERVER_ENV" && test ! -L "$OBSERVER_ENV"
test "$(stat -c '%u:%a' "$OBSERVER_ENV")" = "$(id -u):600"
```

The observer file has `BRAIN_DELIVERY_ENABLED=true` before dormant preflight,
but the observer unit stays inactive. MCP remains at
`BRAIN_DELIVERY_ENABLED=false` until every writer is pinned and the dormant
preflight passes.

## Render every immutable writer

First render the observer into the release-owned `0700` parent. The installer
does not call systemd.

```bash
cd "$RELEASE/brain-v42"
export BRAIN_DELIVERY_OBSERVER_PYTHON="$RELEASE/venv/bin/python"
export BRAIN_DELIVERY_OBSERVER_ENV_FILE="$OBSERVER_ENV"
test ! -e "$RELEASE/systemd" && test ! -L "$RELEASE/systemd"
"$RELEASE/brain-v42/deploy/systemd/install-delivery-observer.sh" --check-only
"$RELEASE/brain-v42/deploy/systemd/install-delivery-observer.sh" \
  --render-dir "$RELEASE/systemd"
unset BRAIN_DELIVERY_OBSERVER_PYTHON BRAIN_DELIVERY_OBSERVER_ENV_FILE
```

Create one release drop-in for each writer. Set `PATH_TAIL` to the reviewed,
literal absolute path suffix required by the existing services; it must retain
the existing non-Python commands used by Dream. Preserve the reaper's measured
`--max-age-hours` value and its measured home working directory. Do not use
`UnsetEnvironment`: the preflight rejects controlled names in either form.

```bash
PATH_TAIL='<reviewed absolute PATH suffix, without the release venv prefix>'
REAPER_MAX_AGE_HOURS='<preserved positive integer>'
export RELEASE PATH_TAIL REAPER_MAX_AGE_HOURS
"$RELEASE/venv/bin/python" -I - <<'PY'
import os
import re
from pathlib import Path

release = Path(os.environ["RELEASE"])
path_tail = os.environ["PATH_TAIL"]
reaper_age = os.environ["REAPER_MAX_AGE_HOURS"]
assert release.is_absolute() and re.fullmatch(r"/[A-Za-z0-9._/-]+", str(release))
assert re.fullmatch(r"/[A-Za-z0-9._/-]+(?::/[A-Za-z0-9._/-]+)*", path_tail)
assert re.fullmatch(r"[1-9][0-9]*", reaper_age)
reaper_working_directory = Path.home()
assert reaper_working_directory == Path("/home/hawixs")

python = release / "venv/bin/python"
commands = {
    "brain-mcp-http.service": f"{python} -m brain_v42.mcp.server --http-server",
    "brain-metrics.service": f"{python} -m brain_v42.metrics",
    "brain-mcp-reaper.service": (
        f"{python} -m brain_v42.maintenance.reap_stale_mcp --max-age-hours {reaper_age}"
    ),
    "brain-v42-dream.service": f"/bin/bash {release}/brain-v42/scripts/dream.sh brain-v42",
    "brain-v42-model-liveness.service": f"{python} -m scripts.probe_model_liveness",
    "brain-v42-automation.service": f"{python} -m brain_v42.automation",
    "brain-v42-graph-recon.service": (
        f"{python} -m brain_v42.scripts.rebuild_graph_projection"
    ),
}
source_units = {
    "brain-v42-dream.service",
    "brain-v42-model-liveness.service",
}
all_units = [*commands, "brain-v42-delivery-observer.service"]
stage = release / "systemd-dropins"
stage.mkdir(mode=0o700)
for unit in all_units:
    lines = ["[Service]"]
    if unit in commands:
        lines.extend(("ExecStart=", f"ExecStart={commands[unit]}"))
    lines.extend(
        (
            f"Environment=PATH={release}/venv/bin:{path_tail}",
            "Environment=UV_NO_SYNC=1",
            "Environment=PYTHONSAFEPATH=1",
            "Environment=PYTHONHOME=",
        )
    )
    if unit in source_units:
        lines.extend(
            (
                f"Environment=UV_PROJECT_ENVIRONMENT={release}/venv",
                f"Environment=PYTHONPATH={release}/brain-v42",
            )
        )
    else:
        lines.append("Environment=PYTHONPATH=")
    if unit == "brain-mcp-reaper.service":
        lines.append(f"WorkingDirectory={reaper_working_directory}")
    directory = stage / f"{unit}.d"
    directory.mkdir(mode=0o700)
    target = directory / "90-immutable-release.conf"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    target.chmod(0o600)
PY
unset PATH_TAIL REAPER_MAX_AGE_HOURS
```

Review the rendered unit and all eight drop-ins. Effective private environment
files must not assign the six controlled runtime keys: `PATH`,
`UV_PROJECT_ENVIRONMENT`, `UV_NO_SYNC`, `PYTHONSAFEPATH`, `PYTHONPATH`, or
`PYTHONHOME`. If a private file does assign one, its value must be a simple,
unquoted, unescaped value that agrees with the drop-in. Wheel modules require an
empty `PYTHONPATH`; every writer requires an empty `PYTHONHOME`.

Review only the staged files here. Publish them after the recovery proof,
quiescence, and schema 053 migration; do not touch live user units yet.

## Generate the deployment preflight configuration

The private JSON contains no credential. It names the manifest, observer file,
health endpoint, repository, one readable probe pull request, and all eight
writers. Use the merged feature pull request for the dormant probe. Trigger
units are inventory metadata; the checker does not start, stop, or attest them.

```bash
PROBE_PR='<merged feature pull-request number>'
export RELEASE PROBE_PR PREFLIGHT_CONFIG OBSERVER_ENV
"$RELEASE/venv/bin/python" -I - <<'PY'
import json
import os
from pathlib import Path

release = Path(os.environ["RELEASE"])
probe = int(os.environ["PROBE_PR"])
target = Path(os.environ["PREFLIGHT_CONFIG"])
observer_env = Path(os.environ["OBSERVER_ENV"])
assert release.is_absolute() and target.is_absolute() and observer_env.is_absolute()
assert probe > 0
writers = {
    "brain-mcp-http.service": {
        "kind": "wheel_module",
        "module": "brain_v42.mcp.server",
        "must_be_active": True,
    },
    "brain-metrics.service": {
        "kind": "wheel_module",
        "module": "brain_v42.metrics",
        "must_be_active": True,
    },
    "brain-mcp-reaper.service": {
        "kind": "wheel_module",
        "module": "brain_v42.maintenance.reap_stale_mcp",
        "must_be_active": False,
        "trigger_unit": "brain-mcp-reaper.timer",
    },
    "brain-v42-dream.service": {
        "kind": "source_script",
        "source_path": "brain-v42/scripts/dream.sh",
        "must_be_active": False,
        "trigger_unit": "brain-v42-dream.timer",
    },
    "brain-v42-model-liveness.service": {
        "kind": "source_script",
        "source_path": "brain-v42/scripts/probe_model_liveness.py",
        "must_be_active": False,
        "trigger_unit": "brain-v42-model-liveness.timer",
    },
    "brain-v42-automation.service": {
        "kind": "wheel_module",
        "module": "brain_v42.automation",
        "must_be_active": False,
    },
    "brain-v42-graph-recon.service": {
        "kind": "wheel_module",
        "module": "brain_v42.scripts.rebuild_graph_projection",
        "must_be_active": False,
        "trigger_unit": "brain-v42-graph-recon.timer",
    },
    "brain-v42-delivery-observer.service": {
        "kind": "wheel_module",
        "module": "brain_v42.delivery_observer",
        "must_be_active": False,
    },
}
config = {
    "schema_version": 1,
    "release_manifest": str(release / "delivery-release.json"),
    "observer_env_file": str(observer_env),
    "health_endpoint": "http://127.0.0.1:8765/health",
    "required_schema_revision": "053",
    "repository": {"id": 1337360966, "slug": "hawkixs/brain-v42"},
    "probe_pull_request": probe,
    "mode": "dormant",
    "writers": writers,
}
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(config, stream, sort_keys=True, indent=2)
PY
unset PROBE_PR
```

For each later release, choose a new filename and retain the preceding config.
Never replace another release operator's private file silently.

## Fresh backup and disposable recovery proof

Complete this section during the authorized window before quiescence or any
production schema write. Production must still be exactly head 052. The proof
uses PostgreSQL 16 clients inside containers because the host has no native
`pg_dump` or `pg_restore`. It restores owners and ACLs into a private disposable
container with no published port, bind mount, or persistent data volume.

<!-- recovery-procedure:start -->
Set `RECOVERY_DIR` to a new durable private directory. The current production
container and database are `brain_v42_postgres` and `brain`; revalidate both at
the window. The pinned matching PostgreSQL 16 image is identified by digest.

```bash
set -Eeuo pipefail
set +x
set -o noclobber
umask 077

PG_CONTAINER=brain_v42_postgres
PG_USER=brain
PG_DATABASE=brain
RESTORE_IMAGE=sha256:b295c2aa92725ecaaa58ffb6664035b45076318d8ca93ae4a9b0994481862f7d
RECOVERY_DIR="$EVIDENCE_DIR/recovery"

case "$RECOVERY_DIR" in (/*) ;; (*) exit 2 ;; esac
test ! -e "$RECOVERY_DIR" && test ! -L "$RECOVERY_DIR"
mkdir -m 0700 "$RECOVERY_DIR"
test "$(stat -c '%u:%a' "$RECOVERY_DIR")" = "$(id -u):700"

RECOVERY_ASSETS="$RELEASE/brain-v42/ops/recovery"
RELEASE_PYTHON="$RELEASE/venv/bin/python"
ALEMBIC_INI="$RELEASE/venv/lib/python3.12/site-packages/brain_v42/alembic.ini"
MIGRATION_053="$RELEASE/venv/lib/python3.12/site-packages/brain_v42/alembic/versions/053_delivery_workflows.py"
V9_SQL="$RECOVERY_ASSETS/brain-v42-v9-pgrestore.sql"
V9_MANIFEST="$RECOVERY_ASSETS/brain-v42-v9.json"
V9_ACL_SQL="$RECOVERY_ASSETS/brain-v42-v9-acl-pgrestore.sql"
V10_SQL="$RECOVERY_ASSETS/brain-v42-v10-pgrestore.sql"
V10_MANIFEST="$RECOVERY_ASSETS/brain-v42-v10.json"
V10_ACL_SQL="$RECOVERY_ASSETS/brain-v42-v10-acl-pgrestore.sql"

test -x "$RELEASE_PYTHON" && test ! -L "$RELEASE_PYTHON"
test -f "$ALEMBIC_INI" && test -f "$MIGRATION_053"
test "$($RELEASE_PYTHON -I -c 'from brain_v42.release import shipped_alembic_head; print(shipped_alembic_head())')" = 053
test "$(sha256sum -- "$MIGRATION_053" | awk '{print $1}')" = 7a6d9c79d431432db64651b94ef9dacc71d3539fa79089741a89cec5e475c586
test "$(sha256sum -- "$V9_SQL" | awk '{print $1}')" = 7c67b8cd0a303d044a21158e7518d942719214ff8d7aa2eb5eef1e927e8b272b
test "$(sha256sum -- "$V9_ACL_SQL" | awk '{print $1}')" = 8169f2572774414539815e48e0432eedeea7c7ab775bdb00ec79c4af89e461c3
test "$(sha256sum -- "$V10_SQL" | awk '{print $1}')" = 23c59ea8ac9c6b67ede8f682381f6ceb84872638cf81952dbbe584d0928972c1
test "$(sha256sum -- "$V10_ACL_SQL" | awk '{print $1}')" = f04e00afe8446d605a37b8ec2b384d696a5338e95b776246cb42716485557923
test "$(jq -r '.contract_id + ":" + (.schema_version|tostring) + ":" + ((.checks|length)|tostring)' "$V9_MANIFEST")" = 'brain-v42/postgresql-recovery/v9:9:30'
test "$(jq -r '.contract_id + ":" + (.schema_version|tostring) + ":" + ((.checks|length)|tostring)' "$V10_MANIFEST")" = 'brain-v42/postgresql-recovery/v10:10:30'
```

Revalidate the production container, image, clients, head, owner, real source
role inventory, and bounded capacity. The last read-only inventory contained
`brain` and `codex_ro` and did not contain `postgres`; the fresh inventory is
authoritative. Require the two application roles and preserve every additional
real role rather than assuming it away.

```bash
test "$(docker inspect --format '{{.State.Running}}' "$PG_CONTAINER")" = true
test "$(docker inspect --format '{{.Image}}' "$PG_CONTAINER")" = "$RESTORE_IMAGE"
test "$(docker image inspect --format '{{.Id}}' "$RESTORE_IMAGE")" = "$RESTORE_IMAGE"
case "$(docker exec "$PG_CONTAINER" pg_dump --version)" in
  ('pg_dump (PostgreSQL) 16.'*) ;;
  (*) printf '%s\n' 'refusing: production pg_dump is not PostgreSQL 16' >&2; exit 2 ;;
esac
case "$(docker exec "$PG_CONTAINER" pg_restore --version)" in
  ('pg_restore (PostgreSQL) 16.'*) ;;
  (*) printf '%s\n' 'refusing: production pg_restore is not PostgreSQL 16' >&2; exit 2 ;;
esac
docker exec "$PG_CONTAINER" pg_isready -q -U "$PG_USER" -d "$PG_DATABASE"

LIVE_HEAD_BEFORE="$(docker exec "$PG_CONTAINER" psql -X -U "$PG_USER" -d "$PG_DATABASE" -Atq -v ON_ERROR_STOP=1 -c 'SELECT version_num FROM public.alembic_version;')"
test "$LIVE_HEAD_BEFORE" = 052
test "$(docker exec "$PG_CONTAINER" psql -X -U "$PG_USER" -d "$PG_DATABASE" -Atq -v ON_ERROR_STOP=1 -c "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_catalog.pg_database WHERE datname = 'brain';")" = brain

SOURCE_ROLES="$RECOVERY_DIR/source-roles.json"
SOURCE_EXTRA_SUPERUSERS="$RECOVERY_DIR/source-extra-superusers.json"
docker exec "$PG_CONTAINER" psql -X -U "$PG_USER" -d "$PG_DATABASE" -Atq -v ON_ERROR_STOP=1 -c \
  "SELECT COALESCE(jsonb_agg(rolname ORDER BY rolname), '[]'::jsonb)::text
     FROM pg_catalog.pg_roles
    WHERE rolname !~ '^pg_';" > "$SOURCE_ROLES"
docker exec "$PG_CONTAINER" psql -X -U "$PG_USER" -d "$PG_DATABASE" -Atq -v ON_ERROR_STOP=1 -c \
  "SELECT COALESCE(jsonb_agg(rolname ORDER BY rolname), '[]'::jsonb)::text
     FROM pg_catalog.pg_roles
    WHERE rolname !~ '^pg_'
      AND rolname NOT IN ('brain','codex_ro')
      AND rolsuper;" > "$SOURCE_EXTRA_SUPERUSERS"
jq -e 'type == "array" and index("brain") != null and index("codex_ro") != null and all(.[]; type == "string")' "$SOURCE_ROLES" >/dev/null
jq -e 'type == "array" and all(.[]; type == "string")' "$SOURCE_EXTRA_SUPERUSERS" >/dev/null

DB_BYTES="$(docker exec "$PG_CONTAINER" psql -X -U "$PG_USER" -d "$PG_DATABASE" -Atq -v ON_ERROR_STOP=1 -c "SELECT pg_catalog.pg_database_size('brain');")"
case "$DB_BYTES" in (*[!0-9]*|'') exit 2 ;; esac
test "$DB_BYTES" -lt 2147483648
AVAILABLE_BYTES="$(df -PB1 "$RECOVERY_DIR" | awk 'NR == 2 {print $4}')"
case "$AVAILABLE_BYTES" in (*[!0-9]*|'') exit 2 ;; esac
test "$AVAILABLE_BYTES" -ge "$((DB_BYTES + 1073741824))"
```

Create fresh owner-only globals and database archives directly from container
stdout. Keep the globals file private: it contains role password hashes. The
archive preserves owners and ACLs; `--no-owner` and `--no-acl` are forbidden.

```bash
BACKUP="$RECOVERY_DIR/brain-v42-pre053-$SOURCE_SHA.dump"
GLOBALS="$RECOVERY_DIR/brain-v42-pre053-$SOURCE_SHA.globals.sql"
BACKUP_TOC="$RECOVERY_DIR/brain-v42-pre053-$SOURCE_SHA.toc"
BACKUP_SCHEMA="$RECOVERY_DIR/brain-v42-pre053-$SOURCE_SHA.schema.sql"

docker exec "$PG_CONTAINER" pg_dumpall \
  -U "$PG_USER" --database="$PG_DATABASE" --globals-only > "$GLOBALS"
docker exec "$PG_CONTAINER" pg_dump \
  -U "$PG_USER" --dbname="$PG_DATABASE" --format=custom > "$BACKUP"

LIVE_HEAD_AFTER="$(docker exec "$PG_CONTAINER" psql -X -U "$PG_USER" -d "$PG_DATABASE" -Atq -v ON_ERROR_STOP=1 -c 'SELECT version_num FROM public.alembic_version;')"
test "$LIVE_HEAD_AFTER" = 052
test "$LIVE_HEAD_AFTER" = "$LIVE_HEAD_BEFORE"
test -s "$GLOBALS" && test -s "$BACKUP"
test "$(stat -c '%u:%a' "$GLOBALS")" = "$(id -u):600"
test "$(stat -c '%u:%a' "$BACKUP")" = "$(id -u):600"
docker exec -i "$PG_CONTAINER" pg_restore --list < "$BACKUP" > "$BACKUP_TOC"
test -s "$BACKUP_TOC"
test ! -e "$BACKUP_SCHEMA" && test ! -L "$BACKUP_SCHEMA"
docker exec -i "$PG_CONTAINER" pg_restore --create --schema-only --file=- < "$BACKUP" \
  > "$BACKUP_SCHEMA"
test -s "$BACKUP_SCHEMA"
test "$(stat -c '%u:%a' "$BACKUP_SCHEMA")" = "$(id -u):600"
test "$(grep -Ec '^CREATE DATABASE brain([[:space:]]|;)' "$BACKUP_SCHEMA")" = 1
test "$(grep -Ec '^ALTER DATABASE brain OWNER TO brain;$' "$BACKUP_SCHEMA")" = 1
grep -Eq '^CREATE ROLE brain;$' "$GLOBALS"
grep -Eq '^CREATE ROLE codex_ro;$' "$GLOBALS"

BACKUP_SHA256="$(sha256sum -- "$BACKUP" | awk '{print $1}')"
GLOBALS_SHA256="$(sha256sum -- "$GLOBALS" | awk '{print $1}')"
case "$BACKUP_SHA256$GLOBALS_SHA256" in (*[!0-9a-f]*|'') exit 2 ;; esac
readonly BACKUP_SHA256 GLOBALS_SHA256
```

Create a unique bootstrap superuser outside the captured source-role inventory.
The disposable container uses the pinned image, the bridge network, a 4 GiB
tmpfs, a 6 GiB memory limit, no published port, no bind mount, and no persistent
volume. Its generated credential stays in a new `0600` file.

```bash
RESTORE_TOKEN="${SOURCE_SHA:0:12}-$$"
RESTORE_NAME="brain-v42-restore-$RESTORE_TOKEN"
RESTORE_SUPERUSER="brain_v42_restore_${SOURCE_SHA:0:12}_$$"
RESTORE_ENV="$RECOVERY_DIR/disposable-bootstrap.env"
RESTORE_LABEL=brain-v42.restore-proof-token
case "$RESTORE_SUPERUSER" in (brain|postgres|'') exit 2 ;; (*[!a-zA-Z0-9_]*) exit 2 ;; esac
test "${#RESTORE_SUPERUSER}" -le 63
jq -e --arg user "$RESTORE_SUPERUSER" 'index($user) == null' "$SOURCE_ROLES" >/dev/null
! docker inspect "$RESTORE_NAME" >/dev/null 2>&1

RESTORE_SUPERUSER="$RESTORE_SUPERUSER" RESTORE_ENV="$RESTORE_ENV" \
  "$RELEASE_PYTHON" -I - <<'PY'
import os
import secrets
from pathlib import Path

target = Path(os.environ["RESTORE_ENV"])
user = os.environ["RESTORE_SUPERUSER"]
assert user not in {"brain", "postgres"}
body = (
    f"POSTGRES_USER={user}\n"
    f"POSTGRES_PASSWORD={secrets.token_urlsafe(48)}\n"
    "POSTGRES_DB=postgres\n"
    "PGDATA=/var/lib/postgresql/data/pgdata\n"
)
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    stream.write(body)
PY
test "$(stat -c '%u:%a' "$RESTORE_ENV")" = "$(id -u):600"

RESTORE_CID=''
cleanup_owned_restore() {
  local current_id current_token
  test -n "${RESTORE_CID:-}" || return 0
  current_id="$(docker inspect --format '{{.Id}}' "$RESTORE_CID" 2>/dev/null)" || {
    RESTORE_CID=''
    return 0
  }
  current_token="$(docker inspect --format "{{index .Config.Labels \"$RESTORE_LABEL\"}}" "$RESTORE_CID")"
  if test "$current_id" != "$RESTORE_CID" || test "$current_token" != "$RESTORE_TOKEN"; then
    printf '%s\n' 'refusing cleanup: disposable-container ownership proof failed' >&2
    return 1
  fi
  docker rm -f -v "$RESTORE_CID" >/dev/null
  ! docker inspect "$RESTORE_CID" >/dev/null 2>&1
  RESTORE_CID=''
}
trap cleanup_owned_restore EXIT INT TERM

RESTORE_CID="$(docker create \
  --name "$RESTORE_NAME" \
  --label "$RESTORE_LABEL=$RESTORE_TOKEN" \
  --network bridge \
  --pull never \
  --env-file "$RESTORE_ENV" \
  --tmpfs /var/lib/postgresql/data:rw,nosuid,nodev,noexec,size=4294967296 \
  --memory 6g --memory-swap 6g --cpus 2 --pids-limit 256 \
  --stop-timeout 30 \
  "$RESTORE_IMAGE")"
case "$RESTORE_CID" in (????????????????????????????????????????????????????????????????) ;; (*) exit 2 ;; esac

docker inspect "$RESTORE_CID" | jq -e \
  --arg cid "$RESTORE_CID" --arg image "$RESTORE_IMAGE" \
  --arg label_key "$RESTORE_LABEL" --arg token "$RESTORE_TOKEN" '
    length == 1
    and .[0].Id == $cid
    and .[0].Image == $image
    and .[0].Config.Labels[$label_key] == $token
    and .[0].HostConfig.NetworkMode == "bridge"
    and ((.[0].HostConfig.PortBindings // {}) | length) == 0
    and ((.[0].HostConfig.Binds // []) | length) == 0
    and ((.[0].Mounts // []) | map(select(.Type == "bind" or .Type == "volume")) | length) == 0
    and (.[0].HostConfig.Tmpfs["/var/lib/postgresql/data"] | contains("size=4294967296"))
    and .[0].HostConfig.Memory == 6442450944
    and .[0].HostConfig.MemorySwap == 6442450944
    and .[0].HostConfig.NanoCpus == 2000000000
    and .[0].HostConfig.PidsLimit == 256
  ' >/dev/null

docker start "$RESTORE_CID" >/dev/null
RESTORE_READY=0
for second in $(seq 1 120); do
  if docker exec "$RESTORE_CID" pg_isready -q -U "$RESTORE_SUPERUSER" -d postgres; then
    RESTORE_READY=1
    break
  fi
  test "$(docker inspect --format '{{.State.Running}}' "$RESTORE_CID")" = true || break
  sleep 1
done
if test "$RESTORE_READY" != 1; then
  docker logs "$RESTORE_CID" > "$RECOVERY_DIR/disposable-startup.private.log" 2>&1 || true
  exit 1
fi
test "$(docker exec "$RESTORE_CID" psql -X -U "$RESTORE_SUPERUSER" -d postgres -Atq -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname = '$RESTORE_SUPERUSER' AND rolsuper;")" = 1
test "$(docker exec "$RESTORE_CID" psql -X -U "$RESTORE_SUPERUSER" -d postgres -Atq -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM pg_catalog.pg_roles WHERE rolname IN ('brain','codex_ro');")" = 0
```

Replay the globals without filtering, then restore the owner/ACL-preserving
archive with `--create --exit-on-error`. Private logs may contain role statements
and must never be attached to a public receipt.

```bash
timeout -s KILL 5m docker exec -i "$RESTORE_CID" \
  psql -X -q -U "$RESTORE_SUPERUSER" -d postgres -v ON_ERROR_STOP=1 -f - \
  < "$GLOBALS" \
  > "$RECOVERY_DIR/globals-restore.private.stdout" \
  2> "$RECOVERY_DIR/globals-restore.private.stderr"

RESTORED_ROLES="$RECOVERY_DIR/restored-roles-after-globals.json"
docker exec "$RESTORE_CID" psql -X -U "$RESTORE_SUPERUSER" -d postgres -Atq -v ON_ERROR_STOP=1 -c \
  "SELECT COALESCE(jsonb_agg(rolname ORDER BY rolname), '[]'::jsonb)::text
     FROM pg_catalog.pg_roles
    WHERE rolname !~ '^pg_';" > "$RESTORED_ROLES"
jq -e --slurpfile source "$SOURCE_ROLES" --arg bootstrap "$RESTORE_SUPERUSER" \
  'sort == (($source[0] + [$bootstrap]) | unique | sort)' "$RESTORED_ROLES" >/dev/null

timeout -s KILL 30m docker exec -i "$RESTORE_CID" \
  pg_restore --create --exit-on-error \
    --username="$RESTORE_SUPERUSER" --dbname=postgres \
  < "$BACKUP" \
  > "$RECOVERY_DIR/database-restore.private.stdout" \
  2> "$RECOVERY_DIR/database-restore.private.stderr"

test "$(docker exec "$RESTORE_CID" psql -X -U brain -d brain -Atq -v ON_ERROR_STOP=1 -c 'SELECT version_num FROM public.alembic_version;')" = 052
test "$(docker exec "$RESTORE_CID" psql -X -U brain -d brain -Atq -v ON_ERROR_STOP=1 -c "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_catalog.pg_database WHERE datname = 'brain';")" = brain
```

Define strict receipt assertions. A bare `30/30` result is insufficient: these
checks also bind contract generation, exact head, check IDs, object counts,
owners, grants, and the measured extra-superuser inventory.

```bash
assert_base_receipt() {
  local result=$1 manifest=$2 contract=$3 schema=$4 head=$5 tables=$6 foreign_keys=$7 indexes=$8
  jq -e \
    --slurpfile manifest "$manifest" \
    --arg contract "$contract" --arg head "$head" \
    --argjson schema "$schema" --argjson tables "$tables" \
    --argjson foreign_keys "$foreign_keys" --argjson indexes "$indexes" '
      def check($id): first(.checks[] | select(.id == $id));
      .contract_id == $contract
      and .schema_version == $schema
      and (.checks | type == "array")
      and (.checks | length) == 30
      and ((.checks | map(.id) | length) == (.checks | map(.id) | unique | length))
      and ((.checks | map(.id) | sort) == ($manifest[0].checks | map(.id) | sort))
      and ([.checks[] | select(.status != "pass")] | length) == 0
      and check("alembic_head").observed == $head
      and check("catalog_counts").expected == {
        foreign_keys: $foreign_keys,
        indexes: $indexes,
        invalid_indexes: 0,
        unvalidated_constraints: 0
      }
      and check("catalog_counts").observed == check("catalog_counts").expected
      and (check("table_set").expected | length) == $tables
      and check("table_set").observed == check("table_set").expected
      and check("sequence_shape").status == "pass"
    ' "$result" >/dev/null
}

assert_acl_receipt() {
  local result=$1 contract=$2 schema=$3
  jq -e \
    --slurpfile source_extra "$SOURCE_EXTRA_SUPERUSERS" \
    --arg bootstrap "$RESTORE_SUPERUSER" \
    --arg contract "$contract" --argjson schema "$schema" '
      .contract_id == $contract
      and .schema_version == $schema
      and (.checks | length) == 1
      and .checks[0].id == "acl_and_ownership"
      and .checks[0].status == "pass"
      and .checks[0].expected == {
        contract_grant_mismatches: 0,
        relation_owner_mismatches: 0,
        role_privilege_mismatches: 0,
        unexpected_grantee_mismatches: 0
      }
      and (.checks[0].observed | {
        contract_grant_mismatches,
        relation_owner_mismatches,
        role_privilege_mismatches,
        unexpected_grantee_mismatches
      }) == .checks[0].expected
      and ((.checks[0].observed.tolerated_superuser_roles | sort)
           == (($source_extra[0] + [$bootstrap]) | unique | sort))
    ' "$result" >/dev/null
}
```

Run v9 and ACL proofs against the restored head 052. Stop on either failure.

```bash
V9_RESULT="$RECOVERY_DIR/brain-v42-v9-pgrestore-result.json"
V9_ACL_RESULT="$RECOVERY_DIR/brain-v42-v9-acl-pgrestore-result.json"
docker exec -i "$RESTORE_CID" psql -X -U brain -d brain -Atq \
  -v ON_ERROR_STOP=1 -f - < "$V9_SQL" > "$V9_RESULT" \
  2> "$RECOVERY_DIR/v9-pgrestore.private.stderr"
docker exec -i "$RESTORE_CID" psql -X -U brain -d brain -Atq \
  -v ON_ERROR_STOP=1 -f - < "$V9_ACL_SQL" > "$V9_ACL_RESULT" \
  2> "$RECOVERY_DIR/v9-acl-pgrestore.private.stderr"
assert_base_receipt "$V9_RESULT" "$V9_MANIFEST" \
  brain-v42/postgresql-recovery/v9 9 052 35 27 136
assert_acl_receipt "$V9_ACL_RESULT" \
  brain-v42/postgresql-recovery/v9-acl-pgrestore 9
```

Build a private clone DSN from the validated observer PostgreSQL URL. This
creates no new user-supplied password prerequisite and never prints the secret.
The host reaches only the inspected bridge IP on container port 5432.

```bash
RESTORE_HOST="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$RESTORE_CID")"
case "$RESTORE_HOST" in (*[!0-9.]*|'') exit 2 ;; esac
test "$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$RESTORE_CID")" = bridge
RESTORE_DSN_FILE="$RECOVERY_DIR/disposable-brain-postgres-url"
env OBSERVER_ENV="$OBSERVER_ENV" RESTORE_HOST="$RESTORE_HOST" \
  RESTORE_DSN_FILE="$RESTORE_DSN_FILE" "$RELEASE_PYTHON" -I - <<'PY'
import os
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from brain_v42.delivery_observer.config import load_observer_settings

settings = load_observer_settings(Path(os.environ["OBSERVER_ENV"]))
assert settings.postgres_url is not None
parts = urlsplit(settings.postgres_url.get_secret_value())
assert parts.username == "brain" and parts.password is not None
assert parts.path == "/brain"
password = unquote(parts.password)
host = os.environ["RESTORE_HOST"]
body = f"postgresql+asyncpg://brain:{quote(password, safe='')}@{host}:5432/brain"
target = Path(os.environ["RESTORE_DSN_FILE"])
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    stream.write(body)
PY
test "$(stat -c '%u:%a' "$RESTORE_DSN_FILE")" = "$(id -u):600"

DSN_PROBE_RESULT="$RECOVERY_DIR/disposable-dsn-probe.json"
POSTGRES_URL="$(< "$RESTORE_DSN_FILE")" EXPECTED_RESTORE_HOST="$RESTORE_HOST" \
  timeout -s KILL 30s "$RELEASE_PYTHON" -I - <<'PY' \
  > "$DSN_PROBE_RESULT" 2> "$RECOVERY_DIR/disposable-dsn-probe.private.stderr"
import asyncio
import json
import os
import asyncpg


async def main() -> None:
    dsn = os.environ["POSTGRES_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(dsn, timeout=10, command_timeout=10)
    try:
        row = await connection.fetchrow(
            """SELECT current_user AS role,
                      current_database() AS database,
                      host(inet_server_addr()) AS host,
                      inet_server_port() AS port,
                      (SELECT version_num FROM public.alembic_version) AS head"""
        )
        observed = dict(row)
        assert observed == {
            "role": "brain",
            "database": "brain",
            "host": os.environ["EXPECTED_RESTORE_HOST"],
            "port": 5432,
            "head": "052",
        }
        print(json.dumps(observed, sort_keys=True))
    finally:
        await connection.close()


asyncio.run(main())
PY
jq -e --arg host "$RESTORE_HOST" \
  '. == {database:"brain", head:"052", host:$host, port:5432, role:"brain"}' \
  "$DSN_PROBE_RESULT" >/dev/null
```

Migrate the clone through the installed wheel as `brain`, then prove the exact
empty additive state and v10/ACL contracts.

```bash
(
  cd "$RECOVERY_DIR"
  POSTGRES_URL="$(< "$RESTORE_DSN_FILE")" \
  BRAIN_ALEMBIC_ALLOW_PROD=1 PYTHONSAFEPATH=1 PYTHONPATH='' \
    timeout -s KILL 10m "$RELEASE_PYTHON" -m alembic \
      -c "$ALEMBIC_INI" upgrade 053 \
    > "$RECOVERY_DIR/alembic-052-to-053.private.stdout" \
    2> "$RECOVERY_DIR/alembic-052-to-053.private.stderr"
)
test "$(docker exec "$RESTORE_CID" psql -X -U brain -d brain -Atq -v ON_ERROR_STOP=1 -c 'SELECT version_num FROM public.alembic_version;')" = 053
test "$(docker exec "$RESTORE_CID" psql -X -U brain -d brain -Atq -v ON_ERROR_STOP=1 -c '
  SELECT
    (SELECT count(*) FROM delivery_artifact_bindings) +
    (SELECT count(*) FROM delivery_confirmations) +
    (SELECT count(*) FROM delivery_contract_revisions) +
    (SELECT count(*) FROM delivery_dependencies) +
    (SELECT count(*) FROM delivery_events) +
    (SELECT count(*) FROM delivery_receipts) +
    (SELECT count(*) FROM delivery_snapshots) +
    (SELECT count(*) FROM delivery_workflows);')" = 0

V10_RESULT="$RECOVERY_DIR/brain-v42-v10-pgrestore-result.json"
V10_ACL_RESULT="$RECOVERY_DIR/brain-v42-v10-acl-pgrestore-result.json"
docker exec -i "$RESTORE_CID" psql -X -U brain -d brain -Atq \
  -v ON_ERROR_STOP=1 -f - < "$V10_SQL" > "$V10_RESULT" \
  2> "$RECOVERY_DIR/v10-pgrestore.private.stderr"
docker exec -i "$RESTORE_CID" psql -X -U brain -d brain -Atq \
  -v ON_ERROR_STOP=1 -f - < "$V10_ACL_SQL" > "$V10_ACL_RESULT" \
  2> "$RECOVERY_DIR/v10-acl-pgrestore.private.stderr"
assert_base_receipt "$V10_RESULT" "$V10_MANIFEST" \
  brain-v42/postgresql-recovery/v10 10 053 43 46 156
assert_acl_receipt "$V10_ACL_RESULT" \
  brain-v42/postgresql-recovery/v10-acl-pgrestore 10
```

Create a non-secret summary only after all four assertions pass. Then remove
only the container whose exact CID and unique label the cleanup function owns.

```bash
test "$(sha256sum -- "$BACKUP" | awk '{print $1}')" = "$BACKUP_SHA256"
test "$(sha256sum -- "$GLOBALS" | awk '{print $1}')" = "$GLOBALS_SHA256"
RECOVERY_SUMMARY="$RECOVERY_DIR/recovery-proof-summary.json"
jq -n \
  --arg source_sha "$SOURCE_SHA" --arg image_id "$RESTORE_IMAGE" \
  --arg backup_sha256 "$BACKUP_SHA256" --arg globals_sha256 "$GLOBALS_SHA256" \
  --arg v9_sha256 "$(sha256sum -- "$V9_RESULT" | awk '{print $1}')" \
  --arg v9_acl_sha256 "$(sha256sum -- "$V9_ACL_RESULT" | awk '{print $1}')" \
  --arg v10_sha256 "$(sha256sum -- "$V10_RESULT" | awk '{print $1}')" \
  --arg v10_acl_sha256 "$(sha256sum -- "$V10_ACL_RESULT" | awk '{print $1}')" '
  {
    schema_version: 1,
    status: "passed",
    source_sha: $source_sha,
    restore_image_id: $image_id,
    backup_sha256: $backup_sha256,
    globals_sha256: $globals_sha256,
    receipts: {
      v9_pgrestore_sha256: $v9_sha256,
      v9_acl_pgrestore_sha256: $v9_acl_sha256,
      v10_pgrestore_sha256: $v10_sha256,
      v10_acl_pgrestore_sha256: $v10_acl_sha256
    }
  }' > "$RECOVERY_SUMMARY"
test "$(stat -c '%u:%a' "$RECOVERY_SUMMARY")" = "$(id -u):600"

CLEANED_RESTORE_CID="$RESTORE_CID"
cleanup_owned_restore
trap - EXIT INT TERM
! docker inspect "$CLEANED_RESTORE_CID" >/dev/null 2>&1
test "$(sha256sum -- "$BACKUP" | awk '{print $1}')" = "$BACKUP_SHA256"
test "$(sha256sum -- "$GLOBALS" | awk '{print $1}')" = "$GLOBALS_SHA256"
test -s "$RECOVERY_SUMMARY"
```

If globals replay, restore, v9, clone migration, or v10 fails, the trap removes
only the owned disposable container. Keep the dump, unmodified globals, private
logs, DSN/bootstrap files, receipts, and summary owner-only. Do not reuse the
evidence directory for a second attempt. Production remains at 052 until the
separate quiesced migration below.
<!-- recovery-procedure:end -->

## Quiesce all writers

Proceed only after the fresh dump restored at 052 and the disposable clone passed
the v9, v9 ACL, migrated-053 v10, and v10 ACL assertions.

```bash
ACTIVE_TRIGGERS="$EVIDENCE_DIR/triggers.active.before"
: > "$ACTIVE_TRIGGERS"
chmod 0600 "$ACTIVE_TRIGGERS"
for unit in "${TRIGGERS[@]}"; do
  if systemctl --user is-active --quiet "$unit"; then
    printf '%s\n' "$unit" >> "$ACTIVE_TRIGGERS"
    systemctl --user stop "$unit"
  fi
done

# Stop only active writers. Failed and inactive units keep their measured state.
for unit in "${WRITERS[@]}"; do
  if systemctl --user is-active --quiet "$unit"; then
    systemctl --user stop "$unit"
  fi
done

for unit in "${WRITERS[@]}"; do
  pid="$(systemctl --user show "$unit" -p MainPID --value)"
  test "$pid" = 0
done
```

Recheck for ad hoc or generic launches that can write the same database. A clean
eight-unit inventory does not prove that no other writer exists. Reconcile the
private action log, process inventory, and trigger inventory; refuse unsupported
launches before migration.

## Apply additive schema 053

Immediately before Alembic, reread both private files in an empty environment,
require the complete observer key set, and repeat exact raw equality with the
canonical `POSTGRES_URL`. Pass that value to Alembic only inside the Python
process; never print it or place it in a command argument. Run the installed
wheel while every writer remains quiescent.

```bash
test "$(docker exec brain_v42_postgres psql -X -U brain -d brain -Atq \
  -v ON_ERROR_STOP=1 -c 'SELECT version_num FROM public.alembic_version;')" = 052
ALEMBIC_INI="$RELEASE/venv/lib/python3.12/site-packages/brain_v42/alembic.ini"
if ! env -i HOME="$HOME" CANONICAL_ENV="$CANONICAL_ENV" \
  OBSERVER_ENV="$OBSERVER_ENV" ALEMBIC_INI="$ALEMBIC_INI" \
  BRAIN_ALEMBIC_ALLOW_PROD=1 PYTHONSAFEPATH=1 \
  "$RELEASE/venv/bin/python" -I - <<'PY' \
  > "$EVIDENCE_DIR/alembic-052-to-053.private.stdout" \
  2> "$EVIDENCE_DIR/alembic-052-to-053.private.stderr"
import io
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from dotenv import dotenv_values

from brain_v42.delivery_observer.auth import (
    load_private_environment,
    read_private_file,
)
from brain_v42.delivery_observer.config import load_observer_settings

REQUIRED = {
    "BRAIN_DELIVERY_ENABLED": "true",
    "BRAIN_DELIVERY_REPOSITORY_REGISTRY": (
        '{"brain-v42":{"1337360966":"hawkixs/brain-v42"}}'
    ),
    "BRAIN_DELIVERY_POLL_SECONDS": "60",
    "BRAIN_DELIVERY_FRESHNESS_SECONDS": "600",
    "BRAIN_DELIVERY_REQUEST_BUDGET_PER_MINUTE": "40",
    "BRAIN_DELIVERY_MAX_CONCURRENT_REQUESTS": "2",
    "BRAIN_DELIVERY_REQUEST_TIMEOUT_SECONDS": "10",
    "BRAIN_DELIVERY_MAX_RESPONSE_BYTES": "4194304",
    "BRAIN_DELIVERY_GITHUB_API_ORIGIN": "https://api.github.com",
    "BRAIN_DELIVERY_GITHUB_API_VERSION": "2026-03-10",
}
TOKEN = {"BRAIN_DELIVERY_GITHUB_TOKEN"}
APP = {
    "BRAIN_DELIVERY_GITHUB_APP_ID",
    "BRAIN_DELIVERY_GITHUB_INSTALLATION_ID",
    "BRAIN_DELIVERY_GITHUB_PRIVATE_KEY_PATH",
}

try:
    canonical_text = read_private_file(Path(os.environ["CANONICAL_ENV"])).decode("utf-8")
    canonical = dotenv_values(
        stream=io.StringIO(canonical_text),
        interpolate=False,
    ).get("POSTGRES_URL")
    if not isinstance(canonical, str) or not canonical:
        raise ValueError

    observer = Path(os.environ["OBSERVER_ENV"])
    selected = load_private_environment(observer)
    required_keys = set(REQUIRED) | {"BRAIN_DELIVERY_POSTGRES_URL"}
    accepted_shapes = {
        frozenset(required_keys | TOKEN),
        frozenset(required_keys | APP),
    }
    if frozenset(selected) not in accepted_shapes:
        raise ValueError
    if any(selected[key] != value for key, value in REQUIRED.items()):
        raise ValueError
    if selected["BRAIN_DELIVERY_POSTGRES_URL"] != canonical:
        raise ValueError

    settings = load_observer_settings(observer)
    assert settings.enabled is True
    assert settings.postgres_url is not None
    assert settings.repositories_for("brain-v42") == {1337360966: "hawkixs/brain-v42"}
    os.environ["POSTGRES_URL"] = canonical
    command.upgrade(Config(os.environ["ALEMBIC_INI"]), "053")
except Exception:
    raise SystemExit(1) from None
PY
then
  printf '%s\n' 'refusing: private target attestation or migration failed' >&2
  exit 2
fi
test "$(docker exec brain_v42_postgres psql -X -U brain -d brain -Atq \
  -v ON_ERROR_STOP=1 -c 'SELECT version_num FROM public.alembic_version;')" = 053
test "$(docker exec brain_v42_postgres psql -X -U brain -d brain -Atq \
  -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM information_schema.tables \
    WHERE table_schema='public' AND table_name LIKE 'delivery_%';")" = 8
```

Migration 053 is additive and starts with no delivery rows. If migration or the
post-check fails, keep writers stopped and diagnose. Do not downgrade to 052.

## Publish the reviewed systemd configuration

Publish the reviewed files only after the recovery proof, quiescence, and
migration have passed. Back up only paths this release replaces; preserve all
other fragments and drop-ins. Do not change permissions on an existing unit or
drop-in directory.

```bash
UNIT_BACKUP="$EVIDENCE_DIR/systemd-before"
test ! -e "$UNIT_BACKUP" && test ! -L "$UNIT_BACKUP"
mkdir -m 0700 "$UNIT_BACKUP"
if test -e "$USER_UNIT_DIR" || test -L "$USER_UNIT_DIR"; then
  test -d "$USER_UNIT_DIR" && test ! -L "$USER_UNIT_DIR"
else
  install -d -m 0755 "$USER_UNIT_DIR"
fi

for unit in "${WRITERS[@]}"; do
  target_dir="$USER_UNIT_DIR/$unit.d"
  target="$target_dir/90-immutable-release.conf"
  if test -e "$target_dir" || test -L "$target_dir"; then
    test -d "$target_dir" && test ! -L "$target_dir"
  else
    install -d -m 0700 "$target_dir"
  fi
  if test -e "$target" || test -L "$target"; then
    cp -a -- "$target" "$UNIT_BACKUP/$unit.90-immutable-release.conf"
  fi
  staged="$RELEASE/systemd-dropins/$unit.d/90-immutable-release.conf"
  test -f "$staged" && test ! -L "$staged"
  next="$(mktemp "$target_dir/.90-immutable-release.conf.XXXXXX")"
  install -m 0644 "$staged" "$next"
  cmp -s "$staged" "$next"
  mv -f -- "$next" "$target"
done

observer_unit="$USER_UNIT_DIR/brain-v42-delivery-observer.service"
if test -e "$observer_unit" || test -L "$observer_unit"; then
  cp -a -- "$observer_unit" "$UNIT_BACKUP/brain-v42-delivery-observer.service"
fi
test -f "$RELEASE/systemd/brain-v42-delivery-observer.service" \
  && test ! -L "$RELEASE/systemd/brain-v42-delivery-observer.service"
observer_next="$(mktemp "$USER_UNIT_DIR/.brain-v42-delivery-observer.service.XXXXXX")"
install -m 0644 "$RELEASE/systemd/brain-v42-delivery-observer.service" "$observer_next"
cmp -s "$RELEASE/systemd/brain-v42-delivery-observer.service" "$observer_next"
mv -f -- "$observer_next" "$observer_unit"

mcp_mode_dir="$USER_UNIT_DIR/brain-mcp-http.service.d"
if test -e "$mcp_mode_dir" || test -L "$mcp_mode_dir"; then
  test -d "$mcp_mode_dir" && test ! -L "$mcp_mode_dir"
else
  install -d -m 0700 "$mcp_mode_dir"
fi
mcp_mode="$mcp_mode_dir/91-delivery-mode.conf"
if test -e "$mcp_mode" || test -L "$mcp_mode"; then
  cp -a -- "$mcp_mode" "$UNIT_BACKUP/brain-mcp-http.service.91-delivery-mode.conf"
fi
mcp_mode_next="$(mktemp "$mcp_mode_dir/.91-delivery-mode.conf.XXXXXX")"
printf '%s\n' '[Service]' \
  'EnvironmentFile=%h/.config/brain-v42/delivery-mcp.env' \
  > "$mcp_mode_next"
chmod 0644 "$mcp_mode_next"
mv -f -- "$mcp_mode_next" "$mcp_mode"

systemctl --user daemon-reload
systemd-analyze --user verify "$observer_unit"
```

Do not start the observer yet.

## Start guarded writers in dormant mode

Start MCP and metrics first. Leave every trigger frozen until the dormant
preflight proves all eight launch surfaces. Leave observer, automation,
graph-recon, Dream, and model-liveness in their measured inactive or failed
service states.

```bash
systemctl --user daemon-reload
systemctl --user start brain-mcp-http.service brain-metrics.service
systemctl --user is-active --quiet brain-mcp-http.service
systemctl --user is-active --quiet brain-metrics.service
wait_for_mcp_health "$EVIDENCE_DIR/mcp-health-dormant.json"
```

Run a fresh dormant preflight against the effective merged systemd configuration,
not the rendered files alone:

```bash
"$RELEASE/venv/bin/python" "$RELEASE/brain-v42/scripts/check_delivery_deployment.py" \
  --config "$PREFLIGHT_CONFIG" \
  | tee "$EVIDENCE_DIR/deployment-preflight-dormant.json"
jq -e --arg sha "$SOURCE_SHA" \
  '.status == "ok" and .source_sha == $sha and .schema_revision == "053"' \
  "$EVIDENCE_DIR/deployment-preflight-dormant.json" >/dev/null

while IFS= read -r unit; do
  test -n "$unit" || continue
  test "$unit" = brain-mcp-http-watchdog.timer && continue
  systemctl --user start "$unit"
done < "$ACTIVE_TRIGGERS"
if grep -qx brain-mcp-http-watchdog.timer "$ACTIVE_TRIGGERS"; then
  systemctl --user start brain-mcp-http-watchdog.timer
fi
```

This is the first gate that attests the running MCP and metrics processes. It
also checks every inactive writer's immutable launch, runtime environment,
manifest payloads, health version/head, schema, and guarded ancestry. Only after
it passes are the triggers that were active before the window restored, with the
watchdog restored last.

## Create H1 and the normal canary pull request

Before enabling delivery mutation or starting the observer, create the harmless
documentation commit H1 on a dedicated branch from deployed `main`, push that
branch, and open the normal canary pull request. Let the nine protected checks
run. Record its actual positive pull-request number here; all later canary steps
use this already-created pull request.

```bash
CANARY_PR='<actual documentation canary pull-request number>'
case "$CANARY_PR" in
  (''|*[!0-9]*) printf '%s\n' 'refusing: invalid canary PR number' >&2; exit 2 ;;
esac
test "$CANARY_PR" -gt 0
readonly CANARY_PR
```

## Activate MCP delivery and the observer

Replace only the non-secret MCP switch, restart MCP, then start the observer.
Create the canary preflight config from the dormant config with the already
created canary PR number and an active observer requirement.

```bash
MCP_DELIVERY_NEXT="$(mktemp "$PRIVATE_CONFIG/.delivery-mcp.env.XXXXXX")"
printf '%s\n' 'BRAIN_DELIVERY_ENABLED=true' > "$MCP_DELIVERY_NEXT"
chmod 0600 "$MCP_DELIVERY_NEXT"
mv -f -- "$MCP_DELIVERY_NEXT" "$MCP_DELIVERY_ENV"
systemctl --user restart brain-mcp-http.service
systemctl --user is-active --quiet brain-mcp-http.service
wait_for_mcp_health "$EVIDENCE_DIR/mcp-health-enabled.json"
systemctl --user start brain-v42-delivery-observer.service
systemctl --user is-active --quiet brain-v42-delivery-observer.service

env PREFLIGHT_CONFIG="$PREFLIGHT_CONFIG" \
  CANARY_PREFLIGHT_CONFIG="$CANARY_PREFLIGHT_CONFIG" \
  CANARY_PR="$CANARY_PR" "$RELEASE/venv/bin/python" -I - <<'PY'
import json
import os
from pathlib import Path

source = Path(os.environ["PREFLIGHT_CONFIG"])
target = Path(os.environ["CANARY_PREFLIGHT_CONFIG"])
canary_pr = int(os.environ["CANARY_PR"])
assert canary_pr > 0
config = json.loads(source.read_text(encoding="utf-8"))
config["mode"] = "canary"
config["probe_pull_request"] = canary_pr
config["writers"]["brain-v42-delivery-observer.service"]["must_be_active"] = True
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(config, stream, sort_keys=True, indent=2)
PY

"$RELEASE/venv/bin/python" "$RELEASE/brain-v42/scripts/check_delivery_deployment.py" \
  --config "$CANARY_PREFLIGHT_CONFIG" \
  | tee "$EVIDENCE_DIR/deployment-preflight-canary.json"
jq -e --arg sha "$SOURCE_SHA" \
  '.status == "ok" and .source_sha == $sha and .schema_revision == "053"' \
  "$EVIDENCE_DIR/deployment-preflight-canary.json" >/dev/null
systemctl --user enable brain-v42-delivery-observer.service
systemctl --user is-enabled --quiet brain-v42-delivery-observer.service
```

## Documentation-only delivery canary

Use a dedicated branch from deployed `main` and one clearly labeled documentation
file. The contract has one `canary-docs` deliverable for repository ID
`1337360966`, target `main`, explicit acceptance, zero required approvals, and
the nine protected check-run selectors. Every selector uses
`provider_id=15368`; do not substitute an App slug or `app_id`. Replace the
contract's required runbook context SHA with the final deployed `SOURCE_SHA`
before creating it.

Use this reviewed contract body. The SHA placeholder is deliberately invalid as
production evidence; replace it with the deployed 40-character `SOURCE_SHA`
immediately before `brain_delivery_contract_set`.

```json
{
  "schema_version": 1,
  "objective": "Verify the production observable delivery workflow with one documentation-only pull request.",
  "constraints": [
    "Brain never launches or controls execution agents.",
    "Use normal repository checks and merge permissions.",
    "Do not change existing session lifecycle records."
  ],
  "acceptance_criteria": [
    "A second commit invalidates evidence for the first commit.",
    "Legacy resolution is rejected before required delivery evidence is available.",
    "The observer records integration and explicit requester acceptance.",
    "A later observer poll advances the confirmation while preserving immutable receipts.",
    "The shared read-only briefing reports the actual current receipt."
  ],
  "priority": 20,
  "context_refs": [
    {
      "kind": "repository_document",
      "repository_id": 1337360966,
      "sha": "<deployed SOURCE_SHA>",
      "path": "docs/runbooks/2026-09-07-observable-delivery-workflows.md",
      "required": true
    }
  ],
  "dependencies": [],
  "deliverables": [
    {
      "key": "canary-docs",
      "repository": "hawkixs/brain-v42",
      "repository_id": 1337360966,
      "target_branch": "main",
      "required_checks": [
        {"kind": "check_run", "name": "lint-ruff", "app_slug": null, "provider_id": 15368},
        {"kind": "check_run", "name": "lint-mypy", "app_slug": null, "provider_id": 15368},
        {"kind": "check_run", "name": "test-unit", "app_slug": null, "provider_id": 15368},
        {"kind": "check_run", "name": "test-integration", "app_slug": null, "provider_id": 15368},
        {"kind": "check_run", "name": "test-coverage", "app_slug": null, "provider_id": 15368},
        {"kind": "check_run", "name": "security-bandit", "app_slug": null, "provider_id": 15368},
        {"kind": "check_run", "name": "security-gitleaks", "app_slug": null, "provider_id": 15368},
        {"kind": "check_run", "name": "security-pip-audit", "app_slug": null, "provider_id": 15368},
        {"kind": "check_run", "name": "security-pip-audit-embedding-supervisor", "app_slug": null, "provider_id": 15368}
      ],
      "no_checks_reason": null,
      "review": {"required_approvals": 0, "allowed_reviewers": []}
    }
  ],
  "acceptance_mode": "explicit"
}
```

General tool signatures and read semantics live in
[`../MCP_TOOLS.md`](../MCP_TOOLS.md). The procedure below records only returned
identities; it does not create or imply an agent or session lifecycle.

<!-- canary-verifier-interface:start -->
The verifier reruns the deployment preflight, reads the persisted delivery view,
and, for every positive phase, independently reads the current GitHub state. It
never mutates Brain or GitHub and performs one observation per invocation.

The authorized operator prepares a raw bearer file from the existing
`MCP_HTTP_TOKEN` in `~/.config/brain-v42/mcp-token.env`, without putting the token
in a command argument or terminal output. It is a separate regular, non-symlink,
owner-only `0600` file containing only printable ASCII token bytes, with no
prefix, whitespace, or trailing newline.

```bash
CANARY_MCP_TOKEN="$PRIVATE_CONFIG/delivery-canary-mcp-token-$SOURCE_SHA"
test -f "$CANARY_MCP_TOKEN" && test ! -L "$CANARY_MCP_TOKEN"
test "$(stat -c '%u:%a' "$CANARY_MCP_TOKEN")" = "$(id -u):600"
CANARY_MCP_TOKEN="$CANARY_MCP_TOKEN" "$RELEASE/venv/bin/python" -I - <<'PY'
import os
from pathlib import Path

from brain_v42.delivery_observer.auth import read_private_file

value = read_private_file(Path(os.environ["CANARY_MCP_TOKEN"])).decode("ascii")
assert value == value.strip()
assert 1 <= len(value) <= 8192
assert all(33 <= ord(character) <= 126 for character in value)
PY
```

Store each invocation's config as a new private `0600` file and retain it beside
the JSON result. The exact schema permits only the keys below. For
`missing-proof`, omit `expected_head` and the three fields after it. Every
positive phase requires `expected_head`. The final three fields are optional:
use `expected_delivery_digest` to pin a digest already returned by an earlier
phase, and use either `prior_success_confirmation_id` or an aware RFC 3339
`not_before` value when the result must prove a later observer poll.

```json
{
  "deployment_config": "/absolute/private/delivery-preflight-canary-SOURCE_SHA.json",
  "mcp_url": "http://127.0.0.1:8765/mcp",
  "mcp_token_file": "/absolute/private/delivery-canary-mcp-token-SOURCE_SHA",
  "actor_project": "brain-v42",
  "ticket_id": "FULL-UUID",
  "repository_id": 1337360966,
  "pr_number": 1,
  "deliverable_key": "canary-docs",
  "contract_revision": 1,
  "attempt": 1,
  "contract_digest": "64-lowercase-hex",
  "expected_head": "40-or-64-lowercase-hex",
  "expected_delivery_digest": "64-lowercase-hex",
  "prior_success_confirmation_id": "FULL-UUID",
  "not_before": "aware-RFC3339-timestamp"
}
```

The MCP URL must be HTTPS or literal HTTP loopback and must include a path. It
must not contain credentials, a query, or a fragment. `deployment_config` is the
active canary preflight created above, and both referenced paths are absolute.
Replace every example identity with an actual returned value; `pr_number: 1` is
only a JSON type example. MCP calls are limited to five seconds each and ten
seconds overall, one connection, and 256 KiB per response. The client rejects
redirects, proxy environment, and compressed responses. Current GitHub reads use
the observer's ten-second request, 4 MiB response, two-connection, and
40-requests-per-minute bounds. The verifier performs no retry.

Run the five phase commands from the immutable release. Set `CANARY_CONFIG` to
the new phase-specific file before each command. Set `CANARY_RESULT` to a new
absolute filename for that single invocation so H1, H2, and later-poll evidence
cannot overwrite one another; the top-level `umask 077` keeps it private.

```bash
test -f "$CANARY_CONFIG" && test ! -L "$CANARY_CONFIG"
test "$(stat -c '%u:%a' "$CANARY_CONFIG")" = "$(id -u):600"
case "$CANARY_RESULT" in (/*) ;; (*) exit 2 ;; esac
test ! -e "$CANARY_RESULT" && test ! -L "$CANARY_RESULT"
```

For `missing-proof`:

```bash
"$RELEASE/venv/bin/python" "$RELEASE/brain-v42/scripts/verify_delivery_canary.py" \
  --config "$CANARY_CONFIG" --phase missing-proof \
  | tee "$CANARY_RESULT"
```

For `observed`:

```bash
"$RELEASE/venv/bin/python" "$RELEASE/brain-v42/scripts/verify_delivery_canary.py" \
  --config "$CANARY_CONFIG" --phase observed \
  | tee "$CANARY_RESULT"
```

For `verified`:

```bash
"$RELEASE/venv/bin/python" "$RELEASE/brain-v42/scripts/verify_delivery_canary.py" \
  --config "$CANARY_CONFIG" --phase verified \
  | tee "$CANARY_RESULT"
```

For `integrated`:

```bash
"$RELEASE/venv/bin/python" "$RELEASE/brain-v42/scripts/verify_delivery_canary.py" \
  --config "$CANARY_CONFIG" --phase integrated \
  | tee "$CANARY_RESULT"
```

For `accepted`:

```bash
"$RELEASE/venv/bin/python" "$RELEASE/brain-v42/scripts/verify_delivery_canary.py" \
  --config "$CANARY_CONFIG" --phase accepted \
  | tee "$CANARY_RESULT"
```

For a syntactically valid invocation, a verification refusal exits with code 2
and emits exactly `status`, `phase`, `timestamp`, and `failure`. The closed
failure vocabulary is:

- `brain_transport_or_shape_invalid`
- `running_artifact_unverified`
- `canary_identity_mismatch`
- `current_generation_unverified`
- `current_github_proof_invalid`
- `required_checks_unverified`
- `receipt_invalid_or_missing`

A success always emits `status`, `phase`, `timestamp`, `outcome`,
`source="deployment_preflight_rerun"`, `source_sha`, and `schema_revision`.
`missing-proof` reports `outcome="expected_missing_proof"`,
`contract={ticket_id,contract_revision,contract_digest,deliverable_key,
repository_id}`, and `assessment={id,version,health}`. It deliberately omits the
configured PR number and attempt because no binding has measured them. It also
refuses a satisfied assessment, integration or completion eligibility,
fulfillment, accepted state, or any integration or fulfillment receipt.

A positive phase reports these additional exact shapes:

- `contract={ticket_id,contract_revision,contract_digest,deliverable_key,
  repository_id,pr_number,attempt}`
- `assessment={id,version,assessed_at,observed_at,fresh_until,delivery_digest,
  stage,requirements_satisfied}`
- `binding={id,binding_version,head_sha,base_sha,integration_sha}`
- `current_confirmation={id,snapshot_id,collection_started_at,
  collection_finished_at}`
- `github={provider_id,repository_id,pr_number,head_sha,base_sha,
  integration_sha,integration_revision,state,collected_at,
  check_records_collected}`

`verified` and later add `required_checks`; each entry has `record_id`, `app_id`,
`kind`, `name`, `app_slug`, `head_sha`, `check_suite_id`, and `record_url`. A
successful conclusion is enforced but not repeated. `integrated` and later add
`integration_receipt`, and `accepted` adds `fulfillment_receipt`. Each receipt
shape is `{id,milestone,workflow_version,assessment_id,decision_time,binding_id,
binding_version,snapshot_id,snapshot_digest,success_confirmation_id,
latest_attempt_confirmation_id,head_sha,base_sha,integration_sha,
integration_revision,collection_started_at,collection_finished_at}`.

An old head is proved only by the retained successful `observed` JSON captured
while that head was current. A prior-confirmation or `not_before` constraint
proves that a later successful poll advanced the confirmation; it does not prove
that no other writer exists. A frozen older receipt may remain valid across a
later poll of the same head only when the generation, H/B/integration identities,
digests, and current freshness checks still agree.
<!-- canary-verifier-interface:end -->

Run the canary in this order:

1. Use the H1 commit and normal pull request created before activation. Confirm
   that all nine protected checks ran; do not create another pull request.
2. Call `brain_ticket_create` with `from_project='brain-v42'`,
   `to_project='brain-v42'`, `kind='request'`, the explicit canary title/body,
   and `extraction='skipped'`. Parse the full ticket UUID from the returned
   confirmation.
3. Call `brain_delivery_contract_set` with `expected_revision=0`, a stable
   idempotency key, `actor_project='brain-v42'`, a reason, the full ticket UUID,
   and the reviewed one-deliverable contract. Retain returned revision 1 and its
   content digest.
4. Run verifier phase `missing-proof`. Then call
   `brain_ticket_transition(action='resolve', author_project='brain-v42', ...)`;
   it must fail with the documented delivery guard, and a read must show the
   ticket still open.
5. Read `brain_delivery_get`, then call `brain_delivery_bind_pr` with the returned
   `expected_revision`, current assessment `expected_workflow_version`,
   `repository_id=1337360966`, PR number, `deliverable_key='canary-docs'`,
   `actor_project='brain-v42'`, the full ticket UUID, and a stable idempotency
   key. Do not call `brain_delivery_refresh`; let the observer poll autonomously.
6. After the first successful observation, run verifier phase `observed` and
   retain its JSON as the only proof of H1. The current view alone cannot prove
   an older head later.
7. Push harmless documentation commit H2. Record its SHA and push time. H2 must
   differ from H1. Let the observer supersede the H1 assessment, and verify that
   legacy `resolve` still fails before merge even when CI is green.
8. Run phase `observed` for H2 with the prior-confirmation or `not_before` gate,
   then wait for the real nine H2 checks and run phase `verified`.
9. Merge normally after protected checks pass. Never use admin bypass. Retain the
   actual integration SHA and wait for autonomous observation before phase
   `integrated`.
10. Call `brain_delivery_accept` as the requester with the current revision,
    using `expected_revision`, `expected_attempt`,
    `expected_delivery_digest`, `actor_project='brain-v42'`, the full ticket
    UUID, and a rationale tied to the observed canary. Caller identity comes
    from the MCP `X-Brain-Agent` boundary, not an argument. Run phase `accepted`
    against the resulting fulfillment receipt.
11. While the ticket remains open, wait for another autonomous poll. Run phase
    `accepted` again with `prior_success_confirmation_id` or `not_before` to prove
    a later confirmation with the same H2 and immutable receipts. A verifier
    result does not prove the absence of unmeasured writers.
12. Keep the ticket open while Root performs the shared read-only briefing proof
    described below. Preserve its matched integration and fulfillment receipt
    IDs without changing the original session lifecycle.
13. After both the later-poll witness and briefing proof pass, call
    `brain_ticket_transition(action='resolve', author_project='brain-v42', ...)`
    through the authorized legacy path. Read the closed ticket with
    `brain_ticket_get`, then read `brain_delivery_get` and require the integration
    and fulfillment receipt IDs to equal the retained pre-close IDs. Do not rerun
    verifier phase `accepted` after closure; this closure proof is a distinct
    persisted ticket and delivery read.

Record H1, H2, integration SHA, check run IDs and URLs, contract/attempt/digests,
assessment and confirmation IDs, integration and fulfillment receipt IDs, and
timestamps exactly as returned. Do not invent missing events, timing, p50/p95,
or polling claims from a single sample.

## Shared read-only briefing proof

After acceptance, Root may call the deployed `make_session_briefing_loader`
directly with the actual PostgreSQL repositories and services. Use a separate
bounded `NullPool` engine with `default_transaction_read_only=on`, a five-second
statement timeout, and the selected private PostgreSQL URL. Load project
`brain-v42` and original session UUID
`9420bd3c-7732-4d11-897a-0a34b733fdc3` once per bounded attempt. Retry read-only
only if the observer changes the current assessment between reads.

Emit only safe UUIDs, digests, booleans, timestamp, source SHA, and module path,
with `proof_kind=shared_readonly_briefing_loader`. Do not emit the full briefing,
focus, decisions, learnings, DSN, or exception text. Do not use `app_lifecycle`:
it starts background writers. Do not invoke session start, resume, capture,
heartbeat, end, abandon, or any lifecycle MCP tool. This proof is a shared
read-only loader result, not a lifecycle response.

## Compatible forward rollback

After production reaches 053, rollback means pausing delivery and selecting a
release that already contains the guard and supports 053, or deploying a forward
fix. Never run `alembic downgrade 052` and never restore the pre-053 dump over a
live 053 database.

At the first rollout symptom, capture the live unit state immediately, before the
first rollback mutation. This entry snapshot is the only source for rollback
restoration; retain the pre-rollout `unit-state.before` file as audit evidence.

```bash
ROLLBACK_STATE="$(mktemp "$EVIDENCE_DIR/unit-state.rollback-entry.XXXXXX")"
chmod 0600 "$ROLLBACK_STATE"
for unit in "${WRITERS[@]}" "${TRIGGERS[@]}"; do
  enabled="$(systemctl --user is-enabled "$unit" 2>/dev/null || true)"
  active="$(systemctl --user is-active "$unit" 2>/dev/null || true)"
  printf '%s\t%s\t%s\n' "$unit" "$enabled" "$active" >> "$ROLLBACK_STATE"
done
readonly ROLLBACK_STATE
```

First pause mutation entry points while MCP still serves reads. Confirm through
the normal MCP boundary that a delivery read succeeds and a harmless delivery
mutation refuses with `delivery_disabled` before stopping processes.

```bash
ROLLBACK_PAUSE_NEXT="$(mktemp "$PRIVATE_CONFIG/.delivery-mcp.env.rollback.XXXXXX")"
printf '%s\n' 'BRAIN_DELIVERY_ENABLED=false' > "$ROLLBACK_PAUSE_NEXT"
chmod 0600 "$ROLLBACK_PAUSE_NEXT"
mv -f -- "$ROLLBACK_PAUSE_NEXT" "$MCP_DELIVERY_ENV"
systemctl --user restart brain-mcp-http.service
systemctl --user is-active --quiet brain-mcp-http.service
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:8765/health \
  > "$EVIDENCE_DIR/mcp-health-rollback-paused.json"

systemctl --user stop brain-v42-delivery-observer.service
for unit in "${TRIGGERS[@]}"; do
  systemctl --user stop "$unit"
done
for unit in "${WRITERS[@]}"; do
  if systemctl --user is-active --quiet "$unit"; then
    systemctl --user stop "$unit"
  fi
done
for unit in "${WRITERS[@]}"; do
  test "$(systemctl --user show "$unit" -p MainPID --value)" = 0
done
```

Select a retained release whose source is at or ahead of the permanent guard,
whose installed wheel supports schema 053, and whose own dormant preflight file
was retained. The preflight performs the authoritative remote ancestry check.
Do not select an unguarded pre-053 release or restore its old drop-ins.

```bash
ROLLBACK_SHA='<full 40-hex SHA of a retained compatible release>'
ROLLBACK_RELEASE="$RELEASE_PARENT/$ROLLBACK_SHA"
ROLLBACK_PREFLIGHT="$PRIVATE_CONFIG/delivery-preflight-$ROLLBACK_SHA.json"
case "$ROLLBACK_SHA" in
  (????????????????????????????????????????) ;;
  (*) exit 2 ;;
esac
case "$ROLLBACK_SHA" in (*[!0-9a-f]*) exit 2 ;; esac
test -d "$ROLLBACK_RELEASE" && test ! -L "$ROLLBACK_RELEASE"
test -x "$ROLLBACK_RELEASE/venv/bin/python" \
  && test ! -L "$ROLLBACK_RELEASE/venv/bin/python"
test -f "$ROLLBACK_RELEASE/delivery-release.json" \
  && test ! -L "$ROLLBACK_RELEASE/delivery-release.json"
test -f "$ROLLBACK_PREFLIGHT" && test ! -L "$ROLLBACK_PREFLIGHT"
test "$(stat -c '%u:%a' "$ROLLBACK_PREFLIGHT")" = "$(id -u):600"
jq -e --arg sha "$ROLLBACK_SHA" \
  --arg guard fcc9328ff6e7f061879af2540c69717fc3061434 \
  '.source_sha == $sha and .minimum_guarded_sha == $guard' \
  "$ROLLBACK_RELEASE/delivery-release.json" >/dev/null
jq -e --arg manifest "$ROLLBACK_RELEASE/delivery-release.json" \
  '.mode == "dormant" and .required_schema_revision == "053"
   and .release_manifest == $manifest
   and .writers["brain-v42-delivery-observer.service"].must_be_active == false' \
  "$ROLLBACK_PREFLIGHT" >/dev/null
test "$($ROLLBACK_RELEASE/venv/bin/python -I -c \
  'from brain_v42.release import shipped_alembic_head; print(shipped_alembic_head())')" = 053
"$ROLLBACK_RELEASE/venv/bin/python" -m brain_v42.delivery_observer --help >/dev/null
"$ROLLBACK_RELEASE/venv/bin/python" \
  "$ROLLBACK_RELEASE/brain-v42/scripts/check_delivery_deployment.py" --help >/dev/null
```

Publish that release's reviewed fragments atomically. Keep the separate MCP file
paused and leave the observer inactive.

```bash
for unit in "${WRITERS[@]}"; do
  target_dir="$USER_UNIT_DIR/$unit.d"
  test -d "$target_dir" && test ! -L "$target_dir"
  staged="$ROLLBACK_RELEASE/systemd-dropins/$unit.d/90-immutable-release.conf"
  test -f "$staged" && test ! -L "$staged"
  target="$target_dir/90-immutable-release.conf"
  next="$(mktemp "$target_dir/.90-immutable-release.conf.rollback.XXXXXX")"
  install -m 0644 "$staged" "$next"
  cmp -s "$staged" "$next"
  mv -f -- "$next" "$target"
done
observer_unit="$USER_UNIT_DIR/brain-v42-delivery-observer.service"
observer_next="$(mktemp "$USER_UNIT_DIR/.brain-v42-delivery-observer.service.rollback.XXXXXX")"
install -m 0644 \
  "$ROLLBACK_RELEASE/systemd/brain-v42-delivery-observer.service" "$observer_next"
cmp -s "$ROLLBACK_RELEASE/systemd/brain-v42-delivery-observer.service" "$observer_next"
mv -f -- "$observer_next" "$observer_unit"

systemctl --user daemon-reload
systemd-analyze --user verify "$observer_unit"
systemctl --user start brain-mcp-http.service brain-metrics.service
systemctl --user is-active --quiet brain-mcp-http.service
systemctl --user is-active --quiet brain-metrics.service
wait_for_mcp_health "$EVIDENCE_DIR/mcp-health-forward-rollback.json"
"$ROLLBACK_RELEASE/venv/bin/python" \
  "$ROLLBACK_RELEASE/brain-v42/scripts/check_delivery_deployment.py" \
  --config "$ROLLBACK_PREFLIGHT" \
  | tee "$EVIDENCE_DIR/deployment-preflight-forward-rollback.json"
jq -e --arg sha "$ROLLBACK_SHA" \
  '.status == "ok" and .source_sha == $sha and .schema_revision == "053"' \
  "$EVIDENCE_DIR/deployment-preflight-forward-rollback.json" >/dev/null

while IFS=$'\t' read -r unit _enabled active; do
  test "$active" = active || continue
  case " ${WRITERS[*]} ${TRIGGERS[*]} " in
    (*" $unit "*) ;;
    (*) printf '%s\n' "refusing unrecognized recorded unit: $unit" >&2; exit 2 ;;
  esac
  case "$unit" in
    (brain-mcp-http.service|brain-metrics.service|brain-v42-delivery-observer.service) ;;
    (*) systemctl --user start "$unit" ;;
  esac
done < "$ROLLBACK_STATE"
```

Verify health version/head, effective unit paths, process imports, logs, MCP
reads, and dependent paths. Keep delivery paused until the fault is fixed and a
fresh canary preflight passes. If verification fails, stop and preserve the
evidence; do not stack another release change.

The fresh dump and globals remain recovery assets through rollback closure. A
production restore is a separate destructive recovery procedure with separate
authority; it is not an automatic response to an application rollback.

## Production receipt

The dated receipt must identify the deployed source SHA, wheel/archive/lock and
manifest hashes, recovery-summary hash, migration proof, before/after writer and
trigger states, dormant and active preflight JSON, H1/H2/check/integration
identities, all verifier receipts, explicit acceptance, the later-poll witness,
and the shared read-only briefing proof. Include measured failures and rollback
actions if any. Repository publication follows the normal protected pull-request
path. Optional main-push image publication is separate and does not attest this
host rollout.
