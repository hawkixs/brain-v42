"""Hermetic RED contracts for isolated systemd unit rendering.

These tests intentionally exercise the installer through subprocesses.  They
must stay independent from the live user manager and from the caller's HOME.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.unit._fixture_modes import make_directory, write_file

ROOT = Path(__file__).resolve().parents[3]
SYSTEMD_DIR = ROOT / "deploy" / "systemd"
INSTALLER = SYSTEMD_DIR / "install.sh"
SECRET_SENTINEL = "installer-isolation-secret-4f7c98a2"
EXPECTED_UNITS = frozenset(
    {
        "brain-v42-dream.service",
        "brain-v42-dream.timer",
        "brain-v42-graph-recon.service",
        "brain-v42-graph-recon.timer",
        "brain-mcp-http.service",
        "brain-mcp-http-watchdog.service",
        "brain-mcp-http-watchdog.timer",
        "brain-v42-automation.service",
        "brain-v42-embedding-backfill.service",
        "brain-v42-embedding-backfill.timer",
    }
)
VENDOR_USER_UNIT_DIRS = (
    "/usr/local/lib/systemd/user",
    "/usr/local/share/systemd/user",
    "/usr/lib/systemd/user",
    "/usr/share/systemd/user",
    "/lib/systemd/user",
)
_SANITIZED_ENV_KEYS = frozenset(
    {
        "allow_live_access",
        "brain_dream_capability_enforcement",
        "graph_ledger_write_enabled",
        "graph_projector_enabled",
        "graph_projector_neo4j_password",
        "graph_projector_neo4j_url",
        "graph_projector_neo4j_user",
        "mcp_http_dream_tokens",
        "mcp_http_host",
        "mcp_http_port",
        "mcp_http_token",
        "neo4j_password",
        "neo4j_url",
        "neo4j_user",
    }
)


@dataclass(frozen=True, slots=True)
class InstallerFixture:
    repo: Path
    installer: Path
    home: Path
    xdg: Path
    user_unit_dir: Path
    fake_bin: Path
    environment: dict[str, str]
    logs: dict[str, Path]


def _write_executable(path: Path, content: str) -> None:
    make_directory(path.parent, parents=True, exist_ok=True)
    write_file(path, textwrap.dedent(content).lstrip(), mode=0o755)


def _link_tool(fake_bin: Path, name: str) -> None:
    source = shutil.which(name)
    assert source is not None, f"required fixture tool is unavailable: {name}"
    (fake_bin / name).symlink_to(Path(source).resolve())


def _write_live_scan_wrapper(
    fake_bin: Path,
    name: str,
    *,
    extra_guard: str = "",
) -> None:
    source = shutil.which(name)
    assert source is not None, f"required fixture tool is unavailable: {name}"
    _write_executable(
        fake_bin / name,
        f"""
        #!/bin/bash
        if [[ "${{ALLOW_LIVE_ACCESS:-0}}" != "1" ]]; then
          for argument in "$@"; do
            case "$argument" in
              "$LIVE_UNIT_DIR"|"$LIVE_UNIT_DIR"/*)
                printf '%s|%s\n' {shlex.quote(name)} "$argument" >> "$LIVE_SCAN_LOG"
                exit 96
                ;;
            esac
          done
        fi
        {extra_guard}
        exec {shlex.quote(str(Path(source).resolve()))} "$@"
        """,
    )


def _install_tool_wrappers(fake_bin: Path, *, include_analyzer: bool) -> None:
    make_directory(fake_bin, parents=True)
    for name in (
        "basename",
        "dirname",
        "env",
        "id",
    ):
        _link_tool(fake_bin, name)

    for name in (
        "awk",
        "bash",
        "chmod",
        "cp",
        "cut",
        "find",
        "grep",
        "head",
        "mkdir",
        "readlink",
        "realpath",
        "rm",
        "sort",
        "tr",
    ):
        _write_live_scan_wrapper(fake_bin, name)
    real_mv = shutil.which("mv")
    assert real_mv is not None
    _write_live_scan_wrapper(
        fake_bin,
        "mv",
        extra_guard=f"""
        printf 'CALL' >> "$MV_LOG"
        printf '|%s' "$@" >> "$MV_LOG"
        printf '\n' >> "$MV_LOG"
        if [[ -e "$TOOL_CONTROL_ROOT/mv-fail-before" ]]; then
          exit 91
        fi
        if [[ -e "$TOOL_CONTROL_ROOT/mv-block-after" \
          || -e "$TOOL_CONTROL_ROOT/mv-swap-after" ]]; then
          {shlex.quote(str(Path(real_mv).resolve()))} "$@"
          if [[ -e "$TOOL_CONTROL_ROOT/mv-swap-after" ]]; then
            target="${{!#}}"
            {shlex.quote(str(Path(real_mv).resolve()))} \
              "$target" "$TOOL_CONTROL_ROOT/original-render"
            /usr/bin/mkdir "$target"
            printf 'replacement-must-survive\n' > "$target/replacement.txt"
          fi
          : > "$TOOL_CONTROL_ROOT/mv-entered-after"
          trap 'exit 143' TERM INT
          while true; do
            /bin/sleep 0.05
          done
        fi
        """,
    )
    _write_live_scan_wrapper(
        fake_bin,
        "sed",
        extra_guard="""
        if [[ -n "${SED_FAIL_MATCH:-}" && "$*" == *"$SED_FAIL_MATCH"* ]]; then
          exit 95
        fi
        """,
    )
    _write_live_scan_wrapper(
        fake_bin,
        "stat",
        extra_guard="""
        if [[ -f "$TOOL_CONTROL_ROOT/stat-foreign-exact-path" ]]; then
          foreign_exact_path="$(<"$TOOL_CONTROL_ROOT/stat-foreign-exact-path")"
          inspected_path="${!#}"
          if [[ "$inspected_path" == "$foreign_exact_path" && "$*" == *"%u"* ]]; then
            printf '999999\n'
            exit 0
          fi
        fi
        if [[ -f "$TOOL_CONTROL_ROOT/stat-foreign-path" ]]; then
          foreign_path="$(<"$TOOL_CONTROL_ROOT/stat-foreign-path")"
          if [[ "$*" == *"$foreign_path"* ]]; then
            if [[ "$*" == *"%u"* ]]; then
              printf '999999\n'
              exit 0
            fi
            if [[ "$*" == *"%U"* ]]; then
              printf 'foreign-owner\n'
              exit 0
            fi
          fi
        fi
        """,
    )

    _write_executable(
        fake_bin / "systemctl",
        """
        #!/bin/bash
        printf '%s\n' "$*" >> "$SYSTEMCTL_LOG"
        exit 97
        """,
    )
    _write_executable(
        fake_bin / "mktemp",
        """
        #!/bin/bash
        created="$(/usr/bin/mktemp "$@")" || exit $?
        printf '%s\n' "$created" >> "$MKTEMP_LOG"
        printf '%s\n' "$created"
        """,
    )
    if include_analyzer:
        _write_executable(
            fake_bin / "systemd-analyze",
            """
            #!/bin/bash
            set -euo pipefail

            fixture_root="$(/usr/bin/realpath -e -- "${0%/*}/..")"
            analyze_log="$fixture_root/systemd-analyze.log"
            analyze_env_log="$fixture_root/systemd-analyze-env.log"
            analyze_entered_marker="$fixture_root/systemd-analyze-entered"
            verified_dirs_log="$fixture_root/verified-dirs.log"
            verified_hashes_log="$fixture_root/verified-hashes.log"
            verified_units_log="$fixture_root/verified-units.log"
            live_home="$fixture_root/home"
            live_xdg="$fixture_root/xdg"
            live_unit_dir="$live_xdg/systemd/user"

            printf 'ARGS|%s\n' "$*" >> "$analyze_log"
            if [[ ! -e "$fixture_root/analyze-skip-isolation" ]]; then
              while IFS='=' read -r environment_key _; do
                case "$environment_key" in
                  HOME|LANG|PATH|PWD|SHLVL|SYSTEMD_UNIT_PATH|XDG_CACHE_HOME|\
                  XDG_CONFIG_HOME|XDG_DATA_HOME|XDG_RUNTIME_DIR|XDG_STATE_HOME|_)
                    ;;
                  *) exit 92 ;;
                esac
              done < <(/usr/bin/env)
              for required_key in \
                HOME LANG PATH SYSTEMD_UNIT_PATH XDG_CACHE_HOME XDG_CONFIG_HOME \
                XDG_DATA_HOME XDG_RUNTIME_DIR XDG_STATE_HOME; do
                /usr/bin/printenv "$required_key" >/dev/null
              done
              [[ " $* " == *" --user "* ]]
              [[ " $* " == *" verify "* ]]
              [[ " $* " == *" --generators=no "* ]]
              [[ " $* " == *" --man=no "* ]]
              [[ -n "${SYSTEMD_UNIT_PATH:-}" ]]
              [[ -z "${VERIFIER_INHERITED_SENTINEL:-}" ]]
              [[ "$HOME" != "$live_home" ]]
              [[ "$XDG_CONFIG_HOME" != "$live_xdg" ]]
              [[ "$SYSTEMD_UNIT_PATH" != *"$live_unit_dir"* ]]
              for isolated_dir in \
                "$HOME" \
                "$XDG_CONFIG_HOME" \
                "$XDG_CACHE_HOME" \
                "$XDG_DATA_HOME" \
                "$XDG_STATE_HOME" \
                "$XDG_RUNTIME_DIR"; do
                [[ -d "$isolated_dir" ]]
                [[ "$(/usr/bin/stat -c '%a' "$isolated_dir")" == "700" ]]
                shopt -s nullglob dotglob
                entries=("$isolated_dir"/*)
                shopt -u nullglob dotglob
                ((${#entries[@]} == 0))
              done
              printf 'ENV|%s|%s|%s|%s|%s|%s|%s\n' \
                "$HOME" \
                "$XDG_CONFIG_HOME" \
                "$XDG_CACHE_HOME" \
                "$XDG_DATA_HOME" \
                "$XDG_STATE_HOME" \
                "$XDG_RUNTIME_DIR" \
                "$SYSTEMD_UNIT_PATH" >> "$analyze_env_log"
            fi

            fail_match=""
            if [[ -f "$fixture_root/analyze-fail-match" ]]; then
              fail_match="$(<"$fixture_root/analyze-fail-match")"
            fi

            for argument in "$@"; do
              case "$argument" in
                *.service|*.timer)
                  [[ -f "$argument" ]]
                  [[ "$(/usr/bin/stat -c '%a' "${argument%/*}")" == "700" ]]
                  [[ "$(/usr/bin/stat -c '%a' "${argument%/*}/..")" == "700" ]]
                  if /usr/bin/grep -Eq '__REPO_ROOT__|__MCP_PORT__' "$argument"; then
                    exit 94
                  fi
                  basename="${argument##*/}"
                  artifact_hash="$(/usr/bin/sha256sum "$argument")"
                  printf '%s|%s\n' "$basename" "${artifact_hash%% *}" \
                    >> "$verified_hashes_log"
                  printf '%s\n' "${argument%/*}" >> "$verified_dirs_log"
                  printf '%s\n' "$basename" >> "$verified_units_log"
                  if [[ -n "$fail_match" && "$basename" == "$fail_match" ]]; then
                    exit 93
                  fi
                  ;;
              esac
            done

            if [[ -f "$fixture_root/chmod-parent-after-verify" ]]; then
              parent_to_open="$(<"$fixture_root/chmod-parent-after-verify")"
              /usr/bin/chmod 0777 "$parent_to_open"
            fi

            if [[ -e "$fixture_root/analyze-mode-block" ]]; then
              : > "$analyze_entered_marker"
              trap 'exit 143' TERM INT
              while true; do
                /bin/sleep 0.05
              done
            fi
            """,
        )


def _make_fixture(tmp_path: Path, *, include_analyzer: bool = True) -> InstallerFixture:
    repo = make_directory(tmp_path / "repo")
    # copytree only copies the source mode onto its leaf, so create the intermediate
    # levels explicitly rather than letting them inherit the caller's umask.
    make_directory(repo / "deploy")
    shutil.copytree(SYSTEMD_DIR, repo / "deploy" / "systemd")
    make_directory(repo / "scripts")
    shutil.copy2(ROOT / "scripts" / "dream.sh", repo / "scripts" / "dream.sh")
    for script_name in ("check_mcp_http_port.py", "check_graph_projector_env.py"):
        write_file(
            repo / "scripts" / script_name,
            "import json\n"
            "import os\n"
            "import sys\n"
            "from pathlib import Path\n"
            "with Path(os.environ['PREFLIGHT_LOG']).open('a', encoding='utf-8') as stream:\n"
            "    stream.write(json.dumps({\n"
            "        'name': Path(__file__).name,\n"
            "        'argv': sys.argv[1:],\n"
            "        'home': os.environ['HOME'],\n"
            "        'xdg': os.environ['XDG_CONFIG_HOME'],\n"
            "    }, sort_keys=True) + '\\n')\n",
        )
    make_directory(repo / ".venv" / "bin", parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(Path(sys.executable).resolve())

    write_file(
        repo / ".env",
        "POSTGRES_URL=postgresql+asyncpg://example.invalid/brain\n"
        f"ISOLATION_SENTINEL={SECRET_SENTINEL}\n",
        mode=0o600,
    )

    home = tmp_path / "home"
    token_file = home / ".config" / "brain-v42" / "mcp-token.env"
    make_directory(token_file.parent, parents=True)
    write_file(token_file, f"MCP_HTTP_TOKEN={SECRET_SENTINEL}\n", mode=0o600)

    xdg = tmp_path / "xdg"
    user_unit_dir = xdg / "systemd" / "user"
    fake_bin = tmp_path / "fake-bin"
    logs = {
        "analyze": tmp_path / "systemd-analyze.log",
        "analyze_env": tmp_path / "systemd-analyze-env.log",
        "analyze_entered": tmp_path / "systemd-analyze-entered",
        "live_scan": tmp_path / "live-scan.log",
        "mktemp": tmp_path / "mktemp.log",
        "mv": tmp_path / "mv.log",
        "mv_entered_after": tmp_path / "mv-entered-after",
        "preflight": tmp_path / "preflight.log",
        "systemctl": tmp_path / "systemctl.log",
        "verified_dirs": tmp_path / "verified-dirs.log",
        "verified_hashes": tmp_path / "verified-hashes.log",
        "verified_units": tmp_path / "verified-units.log",
    }
    _install_tool_wrappers(fake_bin, include_analyzer=include_analyzer)

    environment = os.environ.copy()
    environment.pop("BASH_ENV", None)
    for key in tuple(environment):
        if key.casefold() in _SANITIZED_ENV_KEYS:
            environment.pop(key)
    environment.update(
        {
            "HOME": str(home),
            "LIVE_HOME": str(home),
            "LIVE_SCAN_LOG": str(logs["live_scan"]),
            "LIVE_UNIT_DIR": str(user_unit_dir),
            "LIVE_XDG": str(xdg),
            "MCP_HTTP_HOST": "127.0.0.1",
            "MCP_HTTP_PORT": "8765",
            "MCP_HTTP_TOKEN": SECRET_SENTINEL,
            "MKTEMP_LOG": str(logs["mktemp"]),
            "MV_LOG": str(logs["mv"]),
            "PATH": str(fake_bin),
            "PREFLIGHT_LOG": str(logs["preflight"]),
            "SYSTEMCTL_LOG": str(logs["systemctl"]),
            "TOOL_CONTROL_ROOT": str(tmp_path),
            "VERIFIER_INHERITED_SENTINEL": SECRET_SENTINEL,
            "XDG_CONFIG_HOME": str(xdg),
        }
    )
    return InstallerFixture(
        repo=repo,
        installer=repo / "deploy" / "systemd" / "install.sh",
        home=home,
        xdg=xdg,
        user_unit_dir=user_unit_dir,
        fake_bin=fake_bin,
        environment=environment,
        logs=logs,
    )


def _run_installer(
    fixture: InstallerFixture,
    *arguments: str,
    environment_updates: dict[str, str] | None = None,
    process_umask: str | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = fixture.environment.copy()
    if environment_updates:
        environment_updates = environment_updates.copy()
        if fail_match := environment_updates.pop("ANALYZE_FAIL_MATCH", None):
            (fixture.fake_bin.parent / "analyze-fail-match").write_text(fail_match)
        if environment_updates.pop("ANALYZE_MODE", None) == "block":
            (fixture.fake_bin.parent / "analyze-mode-block").touch()
        if environment_updates.pop("ANALYZE_REQUIRE_ISOLATION", None) == "0":
            (fixture.fake_bin.parent / "analyze-skip-isolation").touch()
        environment.update(environment_updates)
    command = ["/bin/bash", str(fixture.installer), *arguments]
    if process_umask is not None:
        command = [
            "/bin/bash",
            "-c",
            'umask "$1" || exit; shift; exec "$@"',
            "installer-test-umask",
            process_umask,
            *command,
        ]
    return subprocess.run(
        command,
        cwd=fixture.repo,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def _assert_secret_redacted(result: subprocess.CompletedProcess[str]) -> None:
    assert SECRET_SENTINEL not in _combined_output(result)


def _expected_rendered_artifacts(fixture: InstallerFixture) -> dict[str, str]:
    expected: dict[str, str] = {}
    for unit_name in EXPECTED_UNITS:
        if unit_name == "brain-mcp-http-watchdog.timer":
            source_name = f"{unit_name}.tmpl"
        elif unit_name.endswith(".service"):
            source_name = f"{unit_name}.tmpl"
        else:
            source_name = unit_name
        content = (fixture.repo / "deploy" / "systemd" / source_name).read_text()
        expected[unit_name] = content.replace("__REPO_ROOT__", str(fixture.repo)).replace(
            "__MCP_PORT__",
            "8765",
        )
    return expected


def _assert_no_systemctl(fixture: InstallerFixture) -> None:
    assert not fixture.logs["systemctl"].exists()


def _assert_host_preflights_ran(fixture: InstallerFixture) -> None:
    assert fixture.logs["preflight"].is_file()
    records = [json.loads(line) for line in fixture.logs["preflight"].read_text().splitlines()]
    common = {
        "home": str(fixture.home),
        "xdg": str(fixture.xdg),
    }
    assert records == [
        {
            **common,
            "name": "check_graph_projector_env.py",
            "argv": [
                "--shared",
                str(fixture.repo / ".env"),
                "--private",
                str(fixture.home / ".config" / "brain-v42" / "graph-projector.env"),
            ],
        },
        {
            **common,
            "name": "check_mcp_http_port.py",
            "argv": [
                "--shared",
                str(fixture.repo / ".env"),
                "--expected",
                "8765",
                "--expected-host",
                "127.0.0.1",
                "--token-file",
                str(fixture.home / ".config" / "brain-v42" / "mcp-token.env"),
            ],
        },
    ]


def _logged_temp_paths(fixture: InstallerFixture) -> list[Path]:
    assert fixture.logs["mktemp"].is_file(), "installer did not create an isolated staging path"
    created_paths = [Path(line) for line in fixture.logs["mktemp"].read_text().splitlines()]
    assert created_paths
    return created_paths


def _assert_logged_temp_paths_absent(fixture: InstallerFixture) -> None:
    created_paths = _logged_temp_paths(fixture)
    assert all(
        not created_path.exists() and not created_path.is_symlink()
        for created_path in created_paths
    )


def _assert_no_or_cleaned_temp_paths(fixture: InstallerFixture) -> None:
    if fixture.logs["mktemp"].exists():
        _assert_logged_temp_paths_absent(fixture)


def _assert_check_staging_is_bounded_to_tmp(fixture: InstallerFixture) -> None:
    expected_parent = Path("/tmp").resolve()
    staging_paths = _logged_temp_paths(fixture)
    staging_root = staging_paths[0]
    assert staging_root.parent.resolve() == expected_parent
    assert all(path == staging_root or staging_root in path.parents for path in staging_paths)


def _assert_render_staging_is_sibling(
    fixture: InstallerFixture,
    target: Path,
) -> None:
    expected_parent = target.parent.resolve()
    staging_paths = _logged_temp_paths(fixture)
    staging_root = staging_paths[0]
    assert staging_root.parent.resolve() == expected_parent
    assert all(path == staging_root or staging_root in path.parents for path in staging_paths)


def _assert_render_is_published_by_one_atomic_rename(
    fixture: InstallerFixture,
    target: Path,
) -> None:
    assert fixture.logs["mv"].is_file()
    calls = fixture.logs["mv"].read_text().splitlines()
    assert len(calls) == 1
    arguments = calls[0].split("|")[1:]
    assert "-T" in arguments
    assert arguments[-1] == str(target)
    source = Path(arguments[-2])
    assert source.parent in _logged_temp_paths(fixture)
    assert fixture.logs["verified_dirs"].is_file()
    assert set(fixture.logs["verified_dirs"].read_text().splitlines()) == {str(source)}


def _assert_published_bytes_are_the_verified_bytes(
    fixture: InstallerFixture,
    target: Path,
) -> None:
    assert fixture.logs["verified_hashes"].is_file()
    verified_hashes = dict(
        line.split("|", maxsplit=1)
        for line in fixture.logs["verified_hashes"].read_text().splitlines()
    )
    published_hashes = {
        artifact.name: hashlib.sha256(artifact.read_bytes()).hexdigest()
        for artifact in target.iterdir()
    }
    assert published_hashes == verified_hashes


def _verified_units(fixture: InstallerFixture) -> list[str]:
    assert fixture.logs["verified_units"].is_file()
    return fixture.logs["verified_units"].read_text().splitlines()


def _assert_exact_verification(fixture: InstallerFixture) -> None:
    verified = _verified_units(fixture)
    assert len(verified) == len(EXPECTED_UNITS)
    assert frozenset(verified) == EXPECTED_UNITS


def _assert_hermetic_analyzer_environment(fixture: InstallerFixture) -> None:
    assert fixture.logs["analyze_env"].is_file()
    records = fixture.logs["analyze_env"].read_text().splitlines()
    assert len(records) == 1
    for record in records:
        (
            marker,
            isolated_home,
            isolated_config,
            isolated_cache,
            isolated_data,
            isolated_state,
            isolated_runtime,
            unit_path,
        ) = record.split("|", maxsplit=7)
        assert marker == "ENV"
        for isolated_path in (
            isolated_home,
            isolated_config,
            isolated_cache,
            isolated_data,
            isolated_state,
            isolated_runtime,
        ):
            assert isolated_path
            assert not isolated_path.startswith(str(fixture.home))
            assert not isolated_path.startswith(str(fixture.xdg))
        assert unit_path
        assert str(fixture.user_unit_dir) not in unit_path
        unit_path_parts = unit_path.split(":")
        assert all(unit_path_parts)
        render_path = Path(unit_path_parts[0]).resolve()
        assert any(
            render_path == staging_path.resolve() or staging_path.resolve() in render_path.parents
            for staging_path in _logged_temp_paths(fixture)
        )
        assert fixture.logs["verified_dirs"].is_file()
        verified_dirs = {
            Path(path).resolve() for path in fixture.logs["verified_dirs"].read_text().splitlines()
        }
        assert verified_dirs == {render_path}
        assert tuple(unit_path_parts[1:]) == VENDOR_USER_UNIT_DIRS


def _install_live_sentinels(
    fixture: InstallerFixture,
) -> dict[Path, tuple[int, int, str]]:
    make_directory(fixture.user_unit_dir, parents=True, exist_ok=True)
    sentinels: dict[Path, tuple[int, int, str]] = {}
    for unit_name in EXPECTED_UNITS:
        sentinel = fixture.user_unit_dir / unit_name
        content = f"{unit_name}|SHOULD_NOT_BE_SCANNED={SECRET_SENTINEL}\n"
        sentinel.write_text(content, encoding="utf-8")
        sentinel.chmod(0o000)
        stat_result = sentinel.stat()
        sentinels[sentinel] = (stat_result.st_ino, stat_result.st_mode & 0o777, content)
    fixture.user_unit_dir.chmod(0o000)
    return sentinels


def test_check_only_renders_and_verifies_without_publication(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)

    result = _run_installer(fixture, "--check-only")

    assert result.returncode == 0, result.stderr
    assert "check-only: no managed units changed" in result.stdout
    assert not fixture.user_unit_dir.exists()
    _assert_no_systemctl(fixture)
    _assert_host_preflights_ran(fixture)
    _assert_exact_verification(fixture)
    _assert_hermetic_analyzer_environment(fixture)
    _assert_check_staging_is_bounded_to_tmp(fixture)
    _assert_logged_temp_paths_absent(fixture)
    _assert_secret_redacted(result)


@pytest.mark.parametrize(
    "arguments",
    [
        ("--check-only", "--dry-run"),
        ("--check-only", "--uninstall"),
        ("--check-only", "--check-only"),
        ("--check-only", "--render-dir", "/tmp/unused-render-target"),
        ("--render-dir", "/tmp/unused-render-target", "--dry-run"),
        ("--render-dir", "/tmp/unused-render-target", "--uninstall"),
        (
            "--render-dir",
            "/tmp/unused-render-target",
            "--render-dir",
            "/tmp/second-unused-render-target",
        ),
    ],
)
def test_isolated_modes_reject_conflicting_flags_before_mutation(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    fixture = _make_fixture(tmp_path)

    result = _run_installer(fixture, *arguments)

    assert result.returncode == 2
    assert result.stderr
    assert "Unknown arg:" not in result.stderr
    assert not fixture.user_unit_dir.exists()
    assert not fixture.logs["mktemp"].exists()
    assert not fixture.logs["analyze"].exists()
    _assert_no_systemctl(fixture)
    _assert_secret_redacted(result)


def test_render_dir_requires_a_target_before_mutation(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)

    result = _run_installer(fixture, "--render-dir")

    assert result.returncode == 2
    assert "render-dir" in result.stderr
    assert not fixture.user_unit_dir.exists()
    assert not fixture.logs["mktemp"].exists()
    _assert_no_systemctl(fixture)
    _assert_secret_redacted(result)


@pytest.mark.parametrize("mode", ["check-only", "render-dir"])
def test_isolated_modes_never_scan_or_replace_live_units(tmp_path: Path, mode: str) -> None:
    fixture = _make_fixture(tmp_path)
    sentinels = _install_live_sentinels(fixture)
    live_directory_inode = fixture.user_unit_dir.stat().st_ino
    target = tmp_path / "render-parent" / "rendered"
    make_directory(target.parent)
    arguments = ("--check-only",) if mode == "check-only" else ("--render-dir", str(target))

    result = _run_installer(fixture, *arguments)
    observed_live_directory = (
        fixture.user_unit_dir.stat().st_ino,
        fixture.user_unit_dir.stat().st_mode & 0o777,
    )
    fixture.user_unit_dir.chmod(0o700)
    observed_sentinels: dict[Path, tuple[int, int, str]] = {}
    for sentinel in sentinels:
        stat_result = sentinel.stat()
        sentinel.chmod(0o600)
        observed_sentinels[sentinel] = (
            stat_result.st_ino,
            stat_result.st_mode & 0o777,
            sentinel.read_text(),
        )

    assert result.returncode == 0, result.stderr
    assert observed_live_directory == (live_directory_inode, 0)
    assert {item.name for item in fixture.user_unit_dir.iterdir()} == EXPECTED_UNITS
    assert observed_sentinels == sentinels
    assert not fixture.logs["live_scan"].exists()
    _assert_no_systemctl(fixture)
    _assert_host_preflights_ran(fixture)
    _assert_secret_redacted(result)


@pytest.mark.parametrize("mode", ["check-only", "render-dir"])
def test_isolated_modes_fail_closed_without_systemd_analyze(tmp_path: Path, mode: str) -> None:
    fixture = _make_fixture(tmp_path, include_analyzer=False)
    target = tmp_path / "render-parent" / "rendered"
    make_directory(target.parent)
    arguments = ("--check-only",) if mode == "check-only" else ("--render-dir", str(target))

    result = _run_installer(fixture, *arguments)

    assert result.returncode != 0
    assert "systemd-analyze" in result.stderr
    assert not target.exists()
    assert not fixture.user_unit_dir.exists()
    _assert_no_systemctl(fixture)
    _assert_host_preflights_ran(fixture)
    _assert_no_or_cleaned_temp_paths(fixture)
    _assert_secret_redacted(result)


@pytest.mark.parametrize("mode", ["check-only", "render-dir"])
@pytest.mark.parametrize(
    ("failure_environment", "failure_label"),
    [
        ({"SED_FAIL_MATCH": "brain-v42-graph-recon.service.tmpl"}, "render"),
        ({"ANALYZE_FAIL_MATCH": "brain-v42-graph-recon.service"}, "verify"),
    ],
)
def test_isolated_modes_clean_staging_after_failures(
    tmp_path: Path,
    mode: str,
    failure_environment: dict[str, str],
    failure_label: str,
) -> None:
    fixture = _make_fixture(tmp_path)
    target = tmp_path / "render-parent" / "rendered"
    make_directory(target.parent)
    arguments = ("--check-only",) if mode == "check-only" else ("--render-dir", str(target))

    result = _run_installer(
        fixture,
        *arguments,
        environment_updates=failure_environment,
    )

    assert result.returncode != 0, f"{failure_label} failure unexpectedly succeeded"
    assert not target.exists()
    assert not fixture.user_unit_dir.exists()
    _assert_no_systemctl(fixture)
    if mode == "check-only":
        _assert_check_staging_is_bounded_to_tmp(fixture)
    else:
        _assert_render_staging_is_sibling(fixture, target)
    _assert_logged_temp_paths_absent(fixture)
    _assert_secret_redacted(result)


def _wait_for_marker_or_exit(process: subprocess.Popen[str], marker: Path) -> None:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            break
        time.sleep(0.02)
    assert marker.exists(), "installer never reached the blocking verifier"


@pytest.mark.parametrize("mode", ["check-only", "render-dir"])
@pytest.mark.parametrize("termination_signal", [signal.SIGINT, signal.SIGTERM])
def test_isolated_modes_clean_staging_after_signals(
    tmp_path: Path,
    mode: str,
    termination_signal: signal.Signals,
) -> None:
    fixture = _make_fixture(tmp_path)
    target = tmp_path / "render-parent" / "rendered"
    make_directory(target.parent)
    arguments = ("--check-only",) if mode == "check-only" else ("--render-dir", str(target))
    environment = fixture.environment.copy()
    (fixture.fake_bin.parent / "analyze-mode-block").touch()
    process = subprocess.Popen(
        ["/bin/bash", str(fixture.installer), *arguments],
        cwd=fixture.repo,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_for_marker_or_exit(process, fixture.logs["analyze_entered"])
        os.killpg(process.pid, termination_signal)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert process.returncode != 0
    assert not target.exists()
    assert not fixture.user_unit_dir.exists()
    _assert_no_systemctl(fixture)
    if mode == "check-only":
        _assert_check_staging_is_bounded_to_tmp(fixture)
    else:
        _assert_render_staging_is_sibling(fixture, target)
    _assert_logged_temp_paths_absent(fixture)
    assert SECRET_SENTINEL not in stdout + stderr


def test_render_dir_signal_after_rename_removes_only_its_own_target(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    parent = tmp_path / "render-parent"
    make_directory(parent)
    target = parent / "rendered"
    (tmp_path / "mv-block-after").touch()
    process = subprocess.Popen(
        ["/bin/bash", str(fixture.installer), "--render-dir", str(target)],
        cwd=fixture.repo,
        env=fixture.environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_for_marker_or_exit(process, fixture.logs["mv_entered_after"])
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert process.returncode != 0
    assert not target.exists() and not target.is_symlink()
    assert not fixture.user_unit_dir.exists()
    _assert_no_systemctl(fixture)
    _assert_render_staging_is_sibling(fixture, target)
    _assert_logged_temp_paths_absent(fixture)
    assert SECRET_SENTINEL not in stdout + stderr


def test_render_dir_output_failure_after_rename_removes_its_target(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    parent = tmp_path / "render-parent"
    make_directory(parent)
    target = parent / "rendered"
    process = subprocess.Popen(
        ["/bin/bash", str(fixture.installer), "--render-dir", str(target)],
        cwd=fixture.repo,
        env=fixture.environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    output_lines: list[str] = []
    try:
        while True:
            line = process.stdout.readline()
            assert line, "installer exited before verifier success was logged"
            output_lines.append(line)
            if "systemd-analyze verify: OK" in line:
                break
        process.stdout.close()
        process.wait(timeout=5)
        stderr = process.stderr.read()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert process.returncode != 0
    assert not target.exists() and not target.is_symlink()
    _assert_logged_temp_paths_absent(fixture)
    assert SECRET_SENTINEL not in "".join(output_lines) + stderr


def test_check_only_reports_cleanup_failure(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    real_rm = shutil.which("rm")
    assert real_rm is not None
    _write_executable(
        fixture.fake_bin / "rm",
        f"""
        #!/bin/bash
        for argument in "$@"; do
          case "$argument" in
            /tmp/brain-v42-systemd-check.*) exit 90 ;;
          esac
        done
        exec {shlex.quote(str(Path(real_rm).resolve()))} "$@"
        """,
    )

    result = _run_installer(fixture, "--check-only")
    staging_paths = _logged_temp_paths(fixture)

    assert result.returncode != 0
    assert "failed to remove" in result.stderr
    assert all(path.exists() for path in staging_paths)
    _assert_no_systemctl(fixture)
    _assert_secret_redacted(result)
    for staging_path in staging_paths:
        shutil.rmtree(staging_path)


def test_render_dir_publish_failure_leaves_no_partial_target(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    parent = tmp_path / "render-parent"
    make_directory(parent)
    target = parent / "rendered"
    (tmp_path / "mv-fail-before").touch()

    result = _run_installer(fixture, "--render-dir", str(target))

    assert result.returncode != 0
    assert not target.exists() and not target.is_symlink()
    assert not fixture.user_unit_dir.exists()
    _assert_no_systemctl(fixture)
    _assert_render_staging_is_sibling(fixture, target)
    _assert_logged_temp_paths_absent(fixture)
    _assert_secret_redacted(result)


def test_render_dir_rejects_parent_permissions_changed_after_verify(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    parent = tmp_path / "render-parent"
    parent.mkdir(mode=0o700)
    target = parent / "rendered"
    (tmp_path / "chmod-parent-after-verify").write_text(str(parent), encoding="utf-8")

    try:
        result = _run_installer(fixture, "--render-dir", str(target))
    finally:
        parent.chmod(0o700)

    assert result.returncode != 0
    assert "parent permissions changed" in result.stderr
    assert not target.exists() and not target.is_symlink()
    _assert_render_staging_is_sibling(fixture, target)
    _assert_logged_temp_paths_absent(fixture)
    _assert_no_systemctl(fixture)
    _assert_secret_redacted(result)


def test_render_dir_cleanup_refuses_to_delete_a_replaced_target(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    parent = tmp_path / "render-parent"
    make_directory(parent)
    target = parent / "rendered"
    (tmp_path / "mv-swap-after").touch()
    process = subprocess.Popen(
        ["/bin/bash", str(fixture.installer), "--render-dir", str(target)],
        cwd=fixture.repo,
        env=fixture.environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        _wait_for_marker_or_exit(process, fixture.logs["mv_entered_after"])
        os.killpg(process.pid, signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)

    assert process.returncode != 0
    assert (target / "replacement.txt").read_text() == "replacement-must-survive\n"
    assert not fixture.user_unit_dir.exists()
    _assert_no_systemctl(fixture)
    _assert_render_staging_is_sibling(fixture, target)
    _assert_logged_temp_paths_absent(fixture)
    assert SECRET_SENTINEL not in stdout + stderr


def test_render_dir_publishes_exact_verified_artifacts_atomically(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    parent = tmp_path / "render-parent"
    make_directory(parent)
    target = parent / "rendered"

    result = _run_installer(fixture, "--render-dir", str(target))

    assert result.returncode == 0, result.stderr
    assert target.is_dir()
    assert target.stat().st_mode & 0o777 == 0o700
    assert {item.name for item in target.iterdir()} == EXPECTED_UNITS
    assert all(item.is_file() and not item.is_symlink() for item in target.iterdir())
    assert {artifact.name: artifact.read_text() for artifact in target.iterdir()} == (
        _expected_rendered_artifacts(fixture)
    )
    for artifact in target.iterdir():
        content = artifact.read_text()
        assert "__REPO_ROOT__" not in content
        assert "__MCP_PORT__" not in content
        assert SECRET_SENTINEL not in content
    assert not fixture.user_unit_dir.exists()
    _assert_no_systemctl(fixture)
    _assert_host_preflights_ran(fixture)
    _assert_exact_verification(fixture)
    _assert_hermetic_analyzer_environment(fixture)
    _assert_render_staging_is_sibling(fixture, target)
    _assert_render_is_published_by_one_atomic_rename(fixture, target)
    _assert_published_bytes_are_the_verified_bytes(fixture, target)
    _assert_logged_temp_paths_absent(fixture)
    _assert_secret_redacted(result)


@pytest.mark.parametrize(
    "target_kind",
    [
        "relative",
        "existing-directory",
        "existing-file",
        "existing-symlink",
        "dangling-symlink",
        "inside-unit-dir",
        "canonical-inside-unit-dir",
        "missing-parent",
        "parent-file",
        "non-owned-or-unwritable-parent",
        "simulated-foreign-owned-parent",
        "owned-unwritable-parent",
        "shared-writable-parent",
    ],
)
def test_render_dir_rejects_dangerous_targets(tmp_path: Path, target_kind: str) -> None:
    fixture = _make_fixture(tmp_path)
    if target_kind == "relative":
        target = Path("relative-render-output")
    elif target_kind == "existing-directory":
        target = tmp_path / "existing-output"
        target.mkdir()
        (target / "preserve.txt").write_text("preserve", encoding="utf-8")
        target.chmod(0o750)
    elif target_kind == "existing-file":
        target = tmp_path / "existing-output"
        target.write_text("preserve", encoding="utf-8")
    elif target_kind in {"existing-symlink", "dangling-symlink"}:
        destination = tmp_path / "symlink-destination"
        if target_kind == "existing-symlink":
            make_directory(destination)
        target = tmp_path / "output-symlink"
        target.symlink_to(destination, target_is_directory=True)
    elif target_kind == "inside-unit-dir":
        make_directory(fixture.user_unit_dir, parents=True)
        target = fixture.user_unit_dir / "rendered"
    elif target_kind == "canonical-inside-unit-dir":
        make_directory(fixture.user_unit_dir, parents=True)
        detour = fixture.user_unit_dir.parent / "detour"
        make_directory(detour)
        target = detour / ".." / fixture.user_unit_dir.name / "rendered"
    elif target_kind == "missing-parent":
        target = tmp_path / "missing-parent" / "rendered"
    elif target_kind == "parent-file":
        parent_file = tmp_path / "parent-file"
        parent_file.write_text("not a directory", encoding="utf-8")
        target = parent_file / "rendered"
    elif target_kind == "non-owned-or-unwritable-parent":
        target = Path("/proc") / f"brain-v42-render-{os.getpid()}"
    elif target_kind == "simulated-foreign-owned-parent":
        foreign_parent = tmp_path / "simulated-foreign-parent"
        foreign_parent.mkdir(mode=0o700)
        (tmp_path / "stat-foreign-path").write_text(
            str(foreign_parent),
            encoding="utf-8",
        )
        target = foreign_parent / "rendered"
    elif target_kind == "owned-unwritable-parent":
        read_only_parent = tmp_path / "read-only-parent"
        read_only_parent.mkdir(mode=0o500)
        read_only_parent.chmod(0o500)
        target = read_only_parent / "rendered"
    else:
        shared_parent = tmp_path / "shared-parent"
        shared_parent.mkdir(mode=0o777)
        shared_parent.chmod(0o777)
        target = shared_parent / "rendered"

    target_was_symlink = target.is_symlink()
    original_link = os.readlink(target) if target_was_symlink else None
    target_was_file = target.is_file() and not target_was_symlink
    original_content = target.read_text() if target_was_file else None
    original_file_inode = target.stat().st_ino if target_was_file else None
    original_file_mode = target.stat().st_mode & 0o777 if target_was_file else None
    target_was_directory = target.is_dir() and not target_was_symlink
    original_directory_inode = target.stat().st_ino if target_was_directory else None
    original_directory_mode = target.stat().st_mode & 0o777 if target_was_directory else None
    original_directory_entries = (
        {item.name: item.read_bytes() for item in target.iterdir()}
        if target_was_directory
        else None
    )

    result = _run_installer(fixture, "--render-dir", str(target))

    assert result.returncode != 0
    assert "render-dir" in result.stderr
    assert not fixture.logs["analyze"].exists()
    assert not fixture.logs["mktemp"].exists()
    _assert_no_systemctl(fixture)
    _assert_secret_redacted(result)
    if target_was_symlink:
        assert target.is_symlink()
        assert os.readlink(target) == original_link
    elif target_was_file:
        assert target.stat().st_ino == original_file_inode
        assert target.stat().st_mode & 0o777 == original_file_mode
        assert target.read_text() == original_content
    elif target_was_directory:
        assert target.is_dir()
        assert target.stat().st_ino == original_directory_inode
        assert target.stat().st_mode & 0o777 == original_directory_mode
        assert {item.name: item.read_bytes() for item in target.iterdir()} == (
            original_directory_entries
        )
    else:
        assert not target.exists() and not target.is_symlink()


@pytest.mark.parametrize("points_to_live_units", [False, True])
def test_render_dir_rejects_symlinked_parent_component(
    tmp_path: Path,
    points_to_live_units: bool,
) -> None:
    fixture = _make_fixture(tmp_path)
    real_parent = fixture.user_unit_dir if points_to_live_units else tmp_path / "safe-real-parent"
    make_directory(real_parent / "nested", parents=True)
    linked_ancestor = tmp_path / "linked-ancestor"
    linked_ancestor.symlink_to(real_parent, target_is_directory=True)
    target = linked_ancestor / "nested" / "rendered"

    result = _run_installer(fixture, "--render-dir", str(target))

    assert result.returncode != 0
    assert result.stderr
    assert "Unknown arg:" not in result.stderr
    assert not target.exists()
    assert not fixture.logs["analyze"].exists()
    assert not fixture.logs["mktemp"].exists()
    _assert_no_systemctl(fixture)
    _assert_secret_redacted(result)


def test_render_dir_rejects_parent_below_foreign_owned_ancestor(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    foreign_ancestor = tmp_path / "foreign-ancestor"
    parent = foreign_ancestor / "owned-parent"
    make_directory(parent, parents=True, mode=0o700)
    (tmp_path / "stat-foreign-exact-path").write_text(
        str(foreign_ancestor),
        encoding="utf-8",
    )
    target = parent / "rendered"

    result = _run_installer(fixture, "--render-dir", str(target))

    assert result.returncode != 0
    assert "parent ancestry" in result.stderr
    assert not target.exists() and not target.is_symlink()
    assert not fixture.logs["analyze"].exists()
    assert not fixture.logs["mktemp"].exists()
    _assert_no_systemctl(fixture)
    _assert_secret_redacted(result)


def test_render_dir_rejects_symlinked_live_user_unit_path(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    real_systemd_root = tmp_path / "real-systemd"
    real_user_unit_dir = real_systemd_root / "user"
    make_directory(real_user_unit_dir, parents=True)
    make_directory(fixture.xdg)
    (fixture.xdg / "systemd").symlink_to(real_systemd_root, target_is_directory=True)
    target = real_user_unit_dir / "rendered"

    result = _run_installer(fixture, "--render-dir", str(target))

    assert result.returncode != 0
    assert "render-dir" in result.stderr
    assert not target.exists() and not target.is_symlink()
    assert not fixture.logs["analyze"].exists()
    assert not fixture.logs["mktemp"].exists()
    _assert_no_systemctl(fixture)
    _assert_secret_redacted(result)


@pytest.mark.parametrize("legacy_umask", ["0000", "0002", "0022"])
def test_dry_run_preserves_legacy_publication_without_systemctl(
    tmp_path: Path,
    legacy_umask: str,
) -> None:
    fixture = _make_fixture(tmp_path)

    result = _run_installer(
        fixture,
        "--dry-run",
        environment_updates={
            "ALLOW_LIVE_ACCESS": "1",
            "ANALYZE_REQUIRE_ISOLATION": "0",
        },
        process_umask=legacy_umask,
    )

    assert result.returncode == 0, result.stderr
    assert fixture.user_unit_dir.is_dir()
    assert fixture.user_unit_dir.stat().st_mode & 0o777 == 0o700
    assert {item.name for item in fixture.user_unit_dir.iterdir()} == EXPECTED_UNITS
    assert {item.stat().st_mode & 0o777 for item in fixture.user_unit_dir.iterdir()} == {0o600}
    _assert_no_systemctl(fixture)
    _assert_exact_verification(fixture)
    _assert_secret_redacted(result)


def test_dry_run_repairs_existing_permissive_user_unit_directory(
    tmp_path: Path,
) -> None:
    fixture = _make_fixture(tmp_path)
    make_directory(fixture.user_unit_dir, parents=True)
    fixture.user_unit_dir.chmod(0o777)

    result = _run_installer(
        fixture,
        "--dry-run",
        environment_updates={
            "ALLOW_LIVE_ACCESS": "1",
            "ANALYZE_REQUIRE_ISOLATION": "0",
        },
        process_umask="0000",
    )

    assert result.returncode == 0, result.stderr
    assert fixture.user_unit_dir.stat().st_mode & 0o777 == 0o700
    assert {item.name for item in fixture.user_unit_dir.iterdir()} == EXPECTED_UNITS
    assert {item.stat().st_mode & 0o777 for item in fixture.user_unit_dir.iterdir()} == {0o600}
    _assert_no_systemctl(fixture)
    _assert_secret_redacted(result)


def test_dry_run_rejects_permissive_user_unit_ancestor(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    permissive_ancestor = fixture.user_unit_dir.parent
    make_directory(fixture.user_unit_dir, parents=True)
    permissive_ancestor.chmod(0o777)

    result = _run_installer(
        fixture,
        "--dry-run",
        environment_updates={
            "ALLOW_LIVE_ACCESS": "1",
            "ANALYZE_REQUIRE_ISOLATION": "0",
        },
        process_umask="0000",
    )

    assert result.returncode != 0
    assert "user unit directory ancestry" in result.stderr
    assert not any(fixture.user_unit_dir.iterdir())
    assert not fixture.logs["analyze"].exists()
    _assert_no_systemctl(fixture)
    _assert_secret_redacted(result)
