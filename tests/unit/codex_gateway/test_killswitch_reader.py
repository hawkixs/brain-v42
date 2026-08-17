"""The gateway reports the local drop-in, not inferred database history."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_killswitch_reader_parses_local_drop_in_with_stable_json_keys(tmp_path) -> None:
    from brain_v42.codex_gateway.killswitches import KillswitchReader

    drop_in = tmp_path / "killswitches.conf"
    drop_in.write_text(
        "\n".join(
            (
                "[Service]",
                "Environment=BRAIN_DREAM_PROMOTE_ENABLED=true",
                "Environment=BRAIN_DREAM_REORG_ENABLED=true BRAIN_DREAM_REORG_DRY_RUN=false",
                "Environment=BRAIN_DREAM_EXTRACT_ENABLED=true BRAIN_DREAM_EXTRACT_DRY_RUN=true",
                "Environment=BRAIN_DREAM_ROADMAP_ENABLED=false",
            )
        )
    )

    state = await KillswitchReader(drop_in).read()

    assert state == {
        "promote_enabled": True,
        "promote_dry": False,
        "reorg_enabled": True,
        "reorg_dry": False,
        "extract_enabled": True,
        "extract_dry": True,
        "roadmap_enabled": False,
        "roadmap_dry": True,
    }


@pytest.mark.asyncio
async def test_killswitch_reader_fails_closed_when_drop_in_is_missing(tmp_path) -> None:
    from brain_v42.codex_gateway.killswitches import KillswitchReader

    state = await KillswitchReader(tmp_path / "missing.conf").read()

    assert state == {
        "promote_enabled": False,
        "promote_dry": False,
        "reorg_enabled": False,
        "reorg_dry": False,
        "extract_enabled": False,
        "extract_dry": True,
        "roadmap_enabled": False,
        "roadmap_dry": True,
    }


@pytest.mark.asyncio
async def test_killswitch_reader_fails_closed_on_invalid_utf8(tmp_path) -> None:
    from brain_v42.codex_gateway.killswitches import KillswitchReader

    drop_in = tmp_path / "killswitches.conf"
    drop_in.write_bytes(b"\xff\xfe\x00")

    state = await KillswitchReader(drop_in).read()

    assert state["promote_enabled"] is False
    assert state["reorg_enabled"] is False
    assert state["extract_enabled"] is False
    assert state["roadmap_enabled"] is False
    assert state["extract_dry"] is True
    assert state["roadmap_dry"] is True
