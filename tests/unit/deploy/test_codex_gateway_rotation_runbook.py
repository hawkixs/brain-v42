"""Documentation contract for the coordinated Codex gateway credential cutover."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[3]
RUNBOOK = ROOT / "deploy" / "CODEX_GATEWAY.md"
DESIGN = (
    ROOT
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-08-01-codex-gateway-credential-cutover-design.md"
)


def test_gateway_runbook_uses_the_resumable_rotation_coordinator() -> None:
    content = RUNBOOK.read_text(encoding="utf-8")

    assert "scripts/rotate_codex_gateway_credentials.py" in content
    assert "dry-run" in content
    assert "awaiting_consumer_recreation" in content
    assert "--resume" in content
    assert "--rollback" in content
    assert "--consumers-stopped-confirmed" in content
    assert "--rollback-preflight-confirmed" in content
    assert "--consumers-recreated-confirmed" in content
    assert "red-data" in content
    assert "red-shrik" in content
    assert "red-codex" in content
    assert "--expected-alembic-revision" in content
    # PAS `assert "037" in content` : il restait vert après la correction, satisfait
    # par la phrase sans rapport « La migration 037 descend de 036 ». Une sonde
    # positive qui ne peut pas tomber ne garde rien. On épingle la phrase visée.
    assert "Migration 037 descends from 036" in content
    assert ":9211" in content
    assert "ALTER ROLE codex_ro" not in content
    assert "openssl rand -hex 32" not in content


def test_gateway_rotation_quiesces_and_restores_the_mcp_watchdog() -> None:
    content = RUNBOOK.read_text(encoding="utf-8")

    assert "brain-mcp-http-watchdog.timer" in content
    assert "brain-mcp-http-watchdog.service" in content


def test_cutover_design_distinguishes_repository_039_from_production_037() -> None:
    content = " ".join(DESIGN.read_text(encoding="utf-8").split())

    assert "The mechanism never migrates Alembic" in content
    assert (
        "the observed production stays at `037` for this ticket, even though the repo is at `039`."
        in content
    )
    assert "even though the repo is at `038`" not in content
