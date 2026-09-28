import io
import json
from pathlib import Path

import pytest

from headless_agents import cli
from headless_agents.model_live import LiveList


def _invoke(
    home: Path,
    *args: str,
    xdg_config_home: str | None = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    environ = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin",
    }
    if xdg_config_home is not None:
        environ["XDG_CONFIG_HOME"] = xdg_config_home
    code = cli.main(
        list(args),
        environ=environ,
        stdin=io.StringIO(),
        stdout=out,
        stderr=err,
        home=home,
        cwd=home,
    )
    return code, out.getvalue(), err.getvalue()


def _catalogue(home: Path, *, config_home: Path | None = None) -> Path:
    root = config_home if config_home is not None else home / ".config"
    path = root / "ha" / "catalog.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'schema = 1\n[codex."known"]\npurpose = "Review."\n'
        'tasks = [{kind = "code-review", effort = "high"}]\n'
        'cost = {kind = "subscription"}\n'
        'verified_at = 2026-09-27\nsource = "bench"\n'
    )
    return path


def test_json_reports_catalogue_and_unavailable_live_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalogue(tmp_path)
    monkeypatch.setattr(
        cli,
        "live_models",
        lambda provider, **kwargs: LiveList("unavailable", (), "timeout"),
    )
    code, out, err = _invoke(tmp_path, "models", "--json", "--provider", "codex")
    report = json.loads(out)
    assert code == 0 and err == ""
    assert report["schema"] == 1
    assert [row["provider"] for row in report["providers"]] == ["codex"]
    assert report["providers"][0]["models"][0]["id"] == "known"


def test_missing_catalogue_returns_usage_error(
    tmp_path: Path,
) -> None:
    code, out, err = _invoke(tmp_path, "models")
    assert code == 2 and out == ""
    assert "catalog.toml" in err
    assert "ha-delegate skill (red-skills) installs a template" in err


def test_absolute_xdg_config_home_selects_its_ha_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg = tmp_path / "operator-config"
    path = _catalogue(tmp_path, config_home=xdg)
    before = path.read_bytes()
    monkeypatch.setattr(
        cli,
        "live_models",
        lambda provider, **kwargs: LiveList("catalogue-only", (), "no live-list query specified"),
    )
    code, out, err = _invoke(
        tmp_path,
        "models",
        "--provider",
        "codex",
        "--json",
        xdg_config_home=str(xdg),
    )
    assert code == 0 and err == ""
    assert json.loads(out)["providers"][0]["models"][0]["id"] == "known"
    assert path.read_bytes() == before


def test_relative_xdg_config_home_uses_default_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _catalogue(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(
        cli,
        "live_models",
        lambda provider, **kwargs: LiveList("catalogue-only", (), "no live-list query specified"),
    )
    code, out, err = _invoke(
        tmp_path,
        "models",
        "--provider",
        "codex",
        "--json",
        xdg_config_home="relative-config",
    )
    assert code == 0 and err == ""
    assert json.loads(out)["providers"][0]["models"][0]["id"] == "known"
    assert path.read_bytes() == before


def test_text_report_prints_role_and_default_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role link and a ``models.toml`` default are drift a human reads in the
    text report too, not only in ``--json`` (review finding #2)."""
    _catalogue(tmp_path)
    roles_path = tmp_path / ".config" / "ha" / "roles.toml"
    roles_path.write_text('[reviewer]\nprovider = "codex"\nmodel = "known"\n')
    models_path = tmp_path / ".config" / "ha" / "models.toml"
    models_path.write_text('codex = "known"\n')
    monkeypatch.setattr(
        cli,
        "live_models",
        lambda provider, **kwargs: LiveList("catalogue-only", (), "no live-list query specified"),
    )
    code, out, err = _invoke(tmp_path, "models", "--provider", "codex")
    assert code == 0 and err == ""
    assert "reviewer" in out
    assert "models.toml" in out


def test_refresh_prints_drift_without_changing_catalogue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _catalogue(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(
        cli,
        "live_models",
        lambda provider, **kwargs: (
            LiveList("available", ("other",), "live list read")
            if provider == "opencode"
            else LiveList("catalogue-only", (), "no live-list query specified")
        ),
    )
    code, out, _ = _invoke(tmp_path, "models", "--refresh", "--provider", "opencode")
    assert code == 0
    assert "live_uncatalogued" in out and "other" in out
    assert path.read_bytes() == before
