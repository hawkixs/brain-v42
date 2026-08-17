"""Fail-closed loading of the gateway bearer from a private file."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brain_v42.codex_gateway.launcher import load_gateway_token_file

_TOKEN = "a" * 64


def _write(path: Path, content: str, mode: int = 0o600) -> Path:
    path.write_text(content)
    path.chmod(mode)
    return path


def test_private_token_file_requires_one_exact_key_and_returns_secret(tmp_path: Path) -> None:
    token_file = _write(
        tmp_path / "codex-gateway.env",
        f"# private\nBRAIN_CODEX_GATEWAY_TOKEN={_TOKEN}\n",
    )

    secret = load_gateway_token_file(token_file)

    assert secret.get_secret_value() == _TOKEN
    assert _TOKEN not in repr(secret)


@pytest.mark.parametrize(
    "content",
    [
        "BRAIN_CODEX_GATEWAY_TOKEN=x\n",
        "BRAIN_CODEX_GATEWAY_TOKEN=REPLACE_WITH_RANDOM_TOKEN\n",
        f"BRAIN_CODEX_GATEWAY_TOKEN={_TOKEN}\nEXTRA=value\n",
        f"BRAIN_CODEX_GATEWAY_TOKEN={_TOKEN}\nBRAIN_CODEX_GATEWAY_TOKEN={_TOKEN}\n",
    ],
)
def test_private_token_file_rejects_weak_placeholder_extra_or_duplicate_entries(
    tmp_path: Path,
    content: str,
) -> None:
    token_file = _write(tmp_path / "codex-gateway.env", content)

    with pytest.raises(RuntimeError):
        load_gateway_token_file(token_file)


def test_private_token_file_requires_exact_0600_mode(tmp_path: Path) -> None:
    token_file = _write(
        tmp_path / "codex-gateway.env",
        f"BRAIN_CODEX_GATEWAY_TOKEN={_TOKEN}\n",
        0o640,
    )

    with pytest.raises(RuntimeError, match="0600"):
        load_gateway_token_file(token_file)


def test_private_token_file_rejects_symlinks(tmp_path: Path) -> None:
    target = _write(
        tmp_path / "target.env",
        f"BRAIN_CODEX_GATEWAY_TOKEN={_TOKEN}\n",
    )
    link = tmp_path / "codex-gateway.env"
    link.symlink_to(target)

    with pytest.raises(RuntimeError, match="regular"):
        load_gateway_token_file(link)


def test_private_token_file_requires_current_process_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = _write(
        tmp_path / "codex-gateway.env",
        f"BRAIN_CODEX_GATEWAY_TOKEN={_TOKEN}\n",
    )
    monkeypatch.setattr(os, "getuid", lambda: token_file.stat().st_uid + 1)

    with pytest.raises(RuntimeError, match="owned"):
        load_gateway_token_file(token_file)
