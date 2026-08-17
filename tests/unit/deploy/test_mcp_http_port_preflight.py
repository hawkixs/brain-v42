"""Fail-closed contract for the production MCP HTTP systemd port."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from scripts.check_mcp_http_port import (
    McpHttpPortContractError,
    validate_mcp_http_port,
    validate_mcp_http_runtime_files,
)

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_mcp_http_port.py"


def test_missing_shared_environment_uses_the_default_contract(tmp_path: Path) -> None:
    validate_mcp_http_port(tmp_path / "missing.env", expected_port=8765)


def test_absent_port_assignment_uses_the_default_contract(tmp_path: Path) -> None:
    shared = tmp_path / ".env"
    shared.write_text("GRAPH_ENABLED=true\n")

    validate_mcp_http_port(shared, expected_port=8765)


@pytest.mark.parametrize(
    "assignment",
    (
        "MCP_HTTP_PORT=8765",
        "  MCP_HTTP_PORT = 8765  ",
        "MCP_HTTP_PORT='8765'",
        'MCP_HTTP_PORT="8765"',
        "export MCP_HTTP_PORT=8765",
    ),
)
def test_exact_production_port_is_accepted(tmp_path: Path, assignment: str) -> None:
    shared = tmp_path / ".env"
    shared.write_text(f"{assignment}\n")

    validate_mcp_http_port(shared, expected_port=8765)


@pytest.mark.parametrize(
    "content",
    (
        "MCP_HTTP_PORT=9000\n",
        "MCP_HTTP_PORT=invalid\n",
        "MCP_HTTP_PORT=0\n",
        "MCP_HTTP_PORT=65536\n",
        "MCP_HTTP_PORT=8765\nMCP_HTTP_PORT=8765\n",
        "export MCP_HTTP_PORT=9000\n",
    ),
)
def test_ambiguous_or_non_contract_port_is_rejected(tmp_path: Path, content: str) -> None:
    shared = tmp_path / ".env"
    shared.write_text(content)

    with pytest.raises(McpHttpPortContractError):
        validate_mcp_http_port(shared, expected_port=8765)


@pytest.mark.parametrize(
    ("overrides", "message"),
    (
        ({"MCP_HTTP_PORT": "9000"}, "effective MCP HTTP port differs"),
        ({"mcp_http_port": "9000"}, "effective MCP HTTP port differs"),
        ({"MCP_HTTP_PORT": '"8765"'}, "effective MCP HTTP port differs"),
        ({"MCP_HTTP_HOST": "::1"}, "effective MCP HTTP host differs"),
        ({"mcp_http_host": "localhost"}, "effective MCP HTTP host differs"),
        ({"MCP_HTTP_HOST": '"127.0.0.1"'}, "effective MCP HTTP host differs"),
        (
            {"MCP_HTTP_PORT": "8765", "mcp_http_port": "9000"},
            "effective MCP_HTTP_PORT is assigned more than once",
        ),
    ),
)
def test_cli_rejects_an_effective_environment_override(
    tmp_path: Path,
    overrides: dict[str, str],
    message: str,
) -> None:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.casefold() in {"mcp_http_host", "mcp_http_port"}:
            environment.pop(key)
    environment.update(overrides)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--shared",
            str(tmp_path / "missing.env"),
            "--expected",
            "8765",
            "--expected-host",
            "127.0.0.1",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert message in result.stderr
    assert not any(value in result.stderr for value in overrides.values())


@pytest.mark.parametrize(
    "flag",
    ("--require-effective-token", "--require-effective-runtime-settings"),
)
def test_effective_runtime_flags_require_a_token_file(
    tmp_path: Path,
    flag: str,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--shared",
            str(tmp_path / "missing.env"),
            "--expected",
            "8765",
            "--expected-host",
            "127.0.0.1",
            flag,
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--token-file is required" in result.stderr


def test_shared_environment_must_use_the_exact_production_host(tmp_path: Path) -> None:
    shared = tmp_path / ".env"
    shared.write_text("MCP_HTTP_HOST=localhost\n")

    with pytest.raises(McpHttpPortContractError, match="MCP_HTTP_HOST differs"):
        validate_mcp_http_port(
            shared,
            expected_port=8765,
            expected_host="127.0.0.1",
        )


def test_runtime_files_require_owned_regular_0600_files_and_nonempty_token(
    tmp_path: Path,
) -> None:
    shared = tmp_path / ".env"
    shared.write_text("POSTGRES_URL=postgresql+asyncpg://example\n")
    shared.chmod(0o600)
    token = tmp_path / "mcp-token.env"
    token.write_text("MCP_HTTP_TOKEN=test-only-token\n")
    token.chmod(0o600)

    validate_mcp_http_runtime_files(
        shared,
        token,
        expected_uid=os.getuid(),
    )

    token.write_text("MCP_HTTP_TOKEN=\n")
    with pytest.raises(McpHttpPortContractError, match="non-empty MCP_HTTP_TOKEN"):
        validate_mcp_http_runtime_files(
            shared,
            token,
            expected_uid=os.getuid(),
        )

    token.write_text("MCP_HTTP_TOKEN=test-only-token\n")
    token.chmod(0o640)
    with pytest.raises(McpHttpPortContractError, match="mode 0600"):
        validate_mcp_http_runtime_files(
            shared,
            token,
            expected_uid=os.getuid(),
        )


def test_runtime_files_reject_unexpected_private_assignments(tmp_path: Path) -> None:
    shared = tmp_path / ".env"
    shared.write_text("POSTGRES_URL=postgresql+asyncpg://shared\n")
    shared.chmod(0o600)
    token = tmp_path / "mcp-token.env"
    token.write_text("MCP_HTTP_TOKEN=test-only-token\nPOSTGRES_URL=private-override-canary\n")
    token.chmod(0o600)

    with pytest.raises(McpHttpPortContractError, match="contains unexpected keys") as exc:
        validate_mcp_http_runtime_files(
            shared,
            token,
            expected_uid=os.getuid(),
        )

    assert "private-override-canary" not in str(exc.value)


def test_runtime_files_reject_symlinks_and_effective_token_overrides(tmp_path: Path) -> None:
    shared = tmp_path / ".env"
    shared.write_text("POSTGRES_URL=postgresql+asyncpg://example\n")
    shared.chmod(0o600)
    token = tmp_path / "mcp-token.env"
    token.write_text("MCP_HTTP_TOKEN=test-only-token\n")
    token.chmod(0o600)
    token_link = tmp_path / "mcp-token-link.env"
    token_link.symlink_to(token)

    with pytest.raises(McpHttpPortContractError, match="regular file"):
        validate_mcp_http_runtime_files(
            shared,
            token_link,
            expected_uid=os.getuid(),
        )

    with pytest.raises(McpHttpPortContractError, match="effective MCP_HTTP_TOKEN differs"):
        validate_mcp_http_runtime_files(
            shared,
            token,
            expected_uid=os.getuid(),
            effective_environment={"mcp_http_token": "override-canary"},
            require_effective_token=True,
        )

    with pytest.raises(McpHttpPortContractError, match="effective MCP_HTTP_TOKEN is required"):
        validate_mcp_http_runtime_files(
            shared,
            token,
            expected_uid=os.getuid(),
            effective_environment={},
            require_effective_token=True,
        )

    token.write_text("export MCP_HTTP_TOKEN=test-only-token\n")
    with pytest.raises(McpHttpPortContractError, match="systemd assignment"):
        validate_mcp_http_runtime_files(
            shared,
            token,
            expected_uid=os.getuid(),
        )


def test_runtime_files_compare_effective_values_without_shell_unquoting(
    tmp_path: Path,
) -> None:
    shared = tmp_path / ".env"
    shared.write_text("POSTGRES_URL=postgresql+asyncpg://example\n")
    shared.chmod(0o600)
    token = tmp_path / "mcp-token.env"
    token.write_text("MCP_HTTP_TOKEN=test-only-token\n")
    token.chmod(0o600)

    with pytest.raises(McpHttpPortContractError, match="effective MCP_HTTP_TOKEN differs"):
        validate_mcp_http_runtime_files(
            shared,
            token,
            expected_uid=os.getuid(),
            effective_environment={"MCP_HTTP_TOKEN": '"test-only-token"'},
            require_effective_token=True,
        )


def test_runtime_files_attest_effective_dream_capability_settings(tmp_path: Path) -> None:
    shared = tmp_path / ".env"
    shared.write_text("BRAIN_DREAM_CAPABILITY_ENFORCEMENT=false\n")
    shared.chmod(0o600)
    token = tmp_path / "mcp-token.env"
    token.write_text(
        "MCP_HTTP_TOKEN=admin-token\n"
        "BRAIN_DREAM_CAPABILITY_ENFORCEMENT=true\n"
        "MCP_HTTP_DREAM_TOKENS='registry-canary'\n"
    )
    token.chmod(0o600)
    effective = {
        "MCP_HTTP_TOKEN": "admin-token",
        "BRAIN_DREAM_CAPABILITY_ENFORCEMENT": "true",
        "MCP_HTTP_DREAM_TOKENS": "registry-canary",
    }

    validate_mcp_http_runtime_files(
        shared,
        token,
        expected_uid=os.getuid(),
        effective_environment={"MCP_HTTP_TOKEN": "admin-token"},
        require_effective_token=True,
    )

    validate_mcp_http_runtime_files(
        shared,
        token,
        expected_uid=os.getuid(),
        effective_environment=effective,
        require_effective_runtime_settings=True,
    )

    for key, override in (
        ("BRAIN_DREAM_CAPABILITY_ENFORCEMENT", "false"),
        ("MCP_HTTP_DREAM_TOKENS", "override-canary"),
    ):
        overridden = effective | {key: override}
        with pytest.raises(McpHttpPortContractError, match=f"effective {key} differs") as exc:
            validate_mcp_http_runtime_files(
                shared,
                token,
                expected_uid=os.getuid(),
                effective_environment=overridden,
                require_effective_runtime_settings=True,
            )
        assert override not in str(exc.value)


def test_runtime_files_reject_unattested_effective_capability_settings(
    tmp_path: Path,
) -> None:
    shared = tmp_path / ".env"
    shared.write_text("POSTGRES_URL=postgresql+asyncpg://example\n")
    shared.chmod(0o600)
    token = tmp_path / "mcp-token.env"
    token.write_text("MCP_HTTP_TOKEN=admin-token\n")
    token.chmod(0o600)

    with pytest.raises(
        McpHttpPortContractError,
        match="effective BRAIN_DREAM_CAPABILITY_ENFORCEMENT is not attested",
    ):
        validate_mcp_http_runtime_files(
            shared,
            token,
            expected_uid=os.getuid(),
            effective_environment={
                "MCP_HTTP_TOKEN": "admin-token",
                "BRAIN_DREAM_CAPABILITY_ENFORCEMENT": "true",
            },
            require_effective_runtime_settings=True,
        )


@pytest.mark.parametrize("private_key", ("MCP_HTTP_TOKEN", "MCP_HTTP_DREAM_TOKENS"))
def test_runtime_files_reject_private_secrets_in_the_shared_environment(
    tmp_path: Path,
    private_key: str,
) -> None:
    shared = tmp_path / ".env"
    shared.write_text(f"{private_key}=shared-secret-canary\n")
    shared.chmod(0o600)
    token = tmp_path / "mcp-token.env"
    token.write_text("MCP_HTTP_TOKEN=test-only-token\n")
    token.chmod(0o600)

    with pytest.raises(McpHttpPortContractError, match="private MCP secrets") as exc_info:
        validate_mcp_http_runtime_files(
            shared,
            token,
            expected_uid=os.getuid(),
        )

    assert "shared-secret-canary" not in str(exc_info.value)
