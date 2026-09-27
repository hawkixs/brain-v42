from datetime import date
from pathlib import Path

from headless_agents.model_catalog import load_catalogue
from headless_agents.model_live import LiveList
from headless_agents.model_report import build_model_report
from headless_agents.roles import Link, Role, implicit_role


def _catalogue(tmp_path: Path):
    path = tmp_path / "catalog.toml"
    path.write_text(
        'schema = 1\n[codex."known"]\npurpose = "Review."\n'
        'tasks = [{kind = "code-review", effort = "high"}]\n'
        'cost = {kind = "subscription"}\n'
        'verified_at = 2026-08-27\nsource = "bench"\n'
        '[opencode."present"]\npurpose = "Build."\n'
        'tasks = [{kind = "build"}]\n'
        'cost = {kind = "window"}\n'
        'verified_at = 2026-09-27\nsource = "bench"\n'
    )
    return load_catalogue(path)


def test_refresh_reports_each_kind_without_rewriting_catalogue(
    tmp_path: Path,
) -> None:
    catalogue = _catalogue(tmp_path)
    role = implicit_role("codex")
    role = Role(
        **{
            **role.__dict__,
            "name": "reviewer",
            "implicit": False,
            "links": (Link("codex", "missing"),),
            "model": "known",
        }
    )
    before = (tmp_path / "catalog.toml").read_bytes()
    rows = build_model_report(
        catalogue,
        {"opencode": LiveList("available", ("new",), "live list read")},
        {"reviewer": role},
        {"codex": "default-missing"},
        today=date(2026, 9, 27),
    )
    kinds = {item["kind"] for row in rows for item in row["drift"]}
    assert kinds == {
        "live_uncatalogued",
        "catalogued_gone",
        "unknown_role_model",
        "stale_verification",
    }
    codex = next(row for row in rows if row["provider"] == "codex")
    assert any(item["model"] == "missing" for item in codex["drift"])
    assert not any(
        item["model"] == "known" and item["kind"] == "unknown_role_model" for item in codex["drift"]
    )
    assert (tmp_path / "catalog.toml").read_bytes() == before


def test_unavailable_list_never_marks_catalogue_entries_gone(
    tmp_path: Path,
) -> None:
    rows = build_model_report(
        _catalogue(tmp_path),
        {"opencode": LiveList("unavailable", (), "timeout")},
        {},
        {},
        today=date(2026, 9, 27),
    )
    opencode = next(row for row in rows if row["provider"] == "opencode")
    assert opencode["live_status"] == "unavailable"
    assert all(item["kind"] != "catalogued_gone" for item in opencode["drift"])


def test_exactly_thirty_days_is_not_stale(
    tmp_path: Path,
) -> None:
    rows = build_model_report(
        _catalogue(tmp_path),
        {},
        {},
        today=date(2026, 9, 26),
    )
    assert not any(item["kind"] == "stale_verification" for row in rows for item in row["drift"])


def test_report_preserves_validated_optional_effort(
    tmp_path: Path,
) -> None:
    rows = build_model_report(
        _catalogue(tmp_path),
        {},
        {},
        today=date(2026, 9, 27),
    )
    codex = next(row for row in rows if row["provider"] == "codex")
    known = next(model for model in codex["models"] if model["id"] == "known")
    assert known["catalogue"]["tasks"] == [{"kind": "code-review", "effort": "high"}]
    opencode = next(row for row in rows if row["provider"] == "opencode")
    present = next(model for model in opencode["models"] if model["id"] == "present")
    assert present["catalogue"]["tasks"] == [{"kind": "build"}]
