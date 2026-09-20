"""Contracts for the host fact that publishes the Dream drop-in verbatim."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from brain_v42.facts import FactRegistry, FactTarget, HostIdentity, Measured, Unreadable
from brain_v42.facts.probes.dream_killswitches_declared import DreamKillswitchesDeclaredProbe
from brain_v42.facts.sources import HostSourceFactory, HostSourceSession

_RELATIVE = "brain-v42-dream.service.d/killswitches.conf"


def _write_drop_in(root: Path, content: str) -> Path:
    path = root / _RELATIVE
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    return path


def _all_settings(*, reorg_enabled: str = "true") -> str:
    return "\n".join(
        [
            "[Service]",
            "Environment=BRAIN_DREAM_PROMOTE_ENABLED=true",
            f"Environment=BRAIN_DREAM_REORG_ENABLED={reorg_enabled}",
            "Environment=BRAIN_DREAM_REORG_DRY_RUN=false",
            "Environment=BRAIN_DREAM_EXTRACT_ENABLED=true",
            "Environment=BRAIN_DREAM_EXTRACT_DRY_RUN=false",
            "Environment=BRAIN_DREAM_ROADMAP_ENABLED=false",
            "Environment=BRAIN_DREAM_ROADMAP_DRY_RUN=true",
            "Environment=BRAIN_DREAM_SWEEP_ENABLED=true",
            "Environment=BRAIN_DREAM_SWEEP_DRY_RUN=false",
            "",
        ]
    )


async def test_probe_declares_the_host_contract_and_publishes_raw_values(tmp_path: Path) -> None:
    """A boolean conversion would conceal a malformed systemd declaration."""
    path = _write_drop_in(tmp_path, _all_settings(reorg_enabled="True"))
    probe = DreamKillswitchesDeclaredProbe()

    value = await probe.measure(HostSourceSession(tmp_path, lambda: "h"))

    assert probe.name == "dream_killswitches_declared"
    assert probe.definition_version == 1
    assert probe.target is FactTarget.HOST
    assert probe.ttl.total_seconds() == 60
    assert probe.timeout.total_seconds() == 1
    assert probe.briefing is True
    assert probe.policies == {}
    assert probe.value_schema == {
        "promote": "string",
        "reorg": "string",
        "reorg_dry": "string",
        "extract": "string",
        "extract_dry": "string",
        "roadmap": "string",
        "roadmap_dry": "string",
        "sweep": "string",
        "sweep_dry": "string",
        "file_mtime_epoch": "int",
    }
    assert value == {
        "promote": "true",
        "reorg": "True",
        "reorg_dry": "false",
        "extract": "true",
        "extract_dry": "false",
        "roadmap": "false",
        "roadmap_dry": "true",
        "sweep": "true",
        "sweep_dry": "false",
        "file_mtime_epoch": int(os.stat(path).st_mtime),
    }


async def test_probe_uses_the_last_assignment_and_keeps_absent_keys_empty(tmp_path: Path) -> None:
    """Systemd's later assignment wins, while an absent key is not invented."""
    _write_drop_in(
        tmp_path,
        "[Service]\n"
        "Environment=BRAIN_DREAM_PROMOTE_ENABLED=false BRAIN_DREAM_REORG_ENABLED=true\n"
        "Environment=BRAIN_DREAM_PROMOTE_ENABLED=true\n",
    )

    value = await DreamKillswitchesDeclaredProbe().measure(HostSourceSession(tmp_path, lambda: "h"))

    assert value["promote"] == "true"
    assert value["reorg"] == "true"
    assert value["reorg_dry"] == ""
    assert value["sweep_dry"] == ""


async def test_probe_propagates_missing_and_refused_host_files(tmp_path: Path) -> None:
    """A missing or symlinked drop-in is unreadable evidence, never disabled switches."""
    probe = DreamKillswitchesDeclaredProbe()
    source = HostSourceSession(tmp_path, lambda: "h")
    with pytest.raises(FileNotFoundError):
        await probe.measure(source)

    target = tmp_path.parent / "killswitches-target.conf"
    target.write_text(_all_settings(), encoding="utf-8")
    link = tmp_path / _RELATIVE
    link.parent.mkdir()
    os.symlink(target, link)
    with pytest.raises(ValueError):
        await probe.measure(source)


async def test_registry_measures_caches_and_rejects_an_unexpected_host(tmp_path: Path) -> None:
    """The raw drop-in is usable only when it came from the declared host identity."""
    _write_drop_in(tmp_path, _all_settings())

    registry = FactRegistry(
        sources={FactTarget.HOST: HostSourceFactory(tmp_path, hostname=lambda: "h")},
        expected={FactTarget.HOST: HostIdentity("h")},
    )
    registry.register(DreamKillswitchesDeclaredProbe())
    first = await registry.measure("dream_killswitches_declared")
    cached = await registry.measure("dream_killswitches_declared")

    assert isinstance(first, Measured)
    assert isinstance(cached, Measured)
    assert cached.source_kind == "cache"

    mismatched = FactRegistry(
        sources={FactTarget.HOST: HostSourceFactory(tmp_path, hostname=lambda: "h")},
        expected={FactTarget.HOST: HostIdentity("another-host")},
    )
    mismatched.register(DreamKillswitchesDeclaredProbe())
    result = await mismatched.measure("dream_killswitches_declared")
    assert isinstance(result, Unreadable)
    assert result.error_code == "target_mismatch"


async def test_registry_renders_a_missing_drop_in_as_a_probe_error(tmp_path: Path) -> None:
    """Reader exceptions retain their type and probe frame for the operator."""
    registry = FactRegistry(
        sources={FactTarget.HOST: HostSourceFactory(tmp_path, hostname=lambda: "h")},
        expected={FactTarget.HOST: HostIdentity("h")},
    )
    registry.register(DreamKillswitchesDeclaredProbe())

    result = await registry.measure("dream_killswitches_declared")

    assert isinstance(result, Unreadable)
    assert result.error_code == "probe_error"
    assert result.where == "FileNotFoundError in DreamKillswitchesDeclaredProbe.measure"
