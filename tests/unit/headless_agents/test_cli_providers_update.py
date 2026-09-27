"""``ha providers --update`` and ``--check``: the CLI surface (spec 0.5.2 §3.4, lot 4b Task B3).

The update machinery is ``test_updaters.py``'s; these tests pin what the command
refuses before anything runs, what it hands the machinery, what it prints and its
exit code -- and that plain ``ha providers`` is byte for byte what lot 2 printed.
No test here runs an updater: ``updaters.run_updates`` is replaced by a recorder,
except under ``--check``, which runs nothing by construction.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from headless_agents import cli, prove, updaters
from headless_agents.config_paths import state_dir
from headless_agents.proofs import record_proof
from headless_agents.registry import Probe

FIXTURES = Path(__file__).parent / "fixtures" / "providers"

VERSIONS = {
    "claude": "2.1.283 (Claude Code)",
    "codex": "codex-cli 0.156.0",
    "agy": "1.2.12",
    "opencode": "1.18.30",
}


def _fake_probe(name: str, **_: object) -> Probe:
    if name in VERSIONS:
        return Probe(available=True, detail=f"/fake/bin/{name}", version=VERSIONS[name])
    return Probe(available=False, detail=f"{name.upper()}_API_KEY not set")


@dataclass
class _World:
    home: Path
    environ: dict[str, str]
    calls: list[dict[str, Any]] = field(default_factory=list)
    rows: list[updaters.UpdateRow] = field(default_factory=list)

    @property
    def state(self) -> Path:
        return state_dir(self.environ, home=self.home)

    def run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(
            list(argv),
            environ=self.environ,
            stdin=io.StringIO(),
            stdout=out,
            stderr=err,
            cwd=self.home,
            home=self.home,
        )
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
    home = tmp_path / "home"
    (home / ".config" / "ha").mkdir(parents=True)
    (home / ".config" / "ha" / "models.toml").write_text(
        'claude = "m-claude"\ncodex = "m-codex"\nopencode = "m-opencode"\n'
    )
    built = _World(home=home, environ={"HOME": str(home), "PATH": "/usr/bin"})

    def recorder(rails: Any, **kwargs: Any) -> list[updaters.UpdateRow]:
        built.calls.append({"rails": list(rails), **kwargs})
        return list(built.rows)

    monkeypatch.setattr(cli, "probe", _fake_probe)
    monkeypatch.setattr(updaters, "run_updates", recorder)
    return built


def _verdict(rail: str, kind: str, outcome: str = "passed", recorded: bool = True) -> prove.Verdict:
    return prove.Verdict(rail, kind, "v", outcome, f"{outcome} reason", recorded, 1)  # type: ignore[arg-type]


def _row(rail: str, **fields: Any) -> updaters.UpdateRow:
    values: dict[str, Any] = {
        "rail": rail,
        "old_version": "1.0.0",
        "new_version": "2.0.0",
        "updater": (f"/fake/bin/{rail}", *updaters.UPDATERS[rail].args),
        "exit_code": 0,
        "log": Path(f"/state/updates/stamp/{rail}.log"),
        "verdicts": (_verdict(rail, "isolation"),),
        "mode": "writes serialised",
        "rollback_path": None,
        "rollback_command": None,
        "note": None,
        "status": "updated",
    }
    values.update(fields)
    return updaters.UpdateRow(**values)


# ── refused before anything runs ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "argv",
    [("codex",), ("--check",), ("--no-prove",), ("--wait", "5")],
)
def test_update_flags_without_update_are_refused(world: _World, argv: tuple[str, ...]) -> None:
    code, _, err = world.run("providers", *argv)
    assert code == 2 and "--update" in err
    assert world.calls == []


def test_wait_with_check_is_refused(world: _World) -> None:
    code, _, err = world.run("providers", "--update", "--check", "--wait", "5")
    assert code == 2 and "--check" in err and "wait" in err
    assert world.calls == []


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf"])
def test_wait_must_be_finite_and_positive(world: _World, value: str) -> None:
    code, _, err = world.run("providers", "--update", "--wait", value)
    assert code == 2
    assert "--wait needs a finite number of seconds greater than zero" in err
    assert world.calls == []


@pytest.mark.parametrize("name", ["openrouter", "nope"])
def test_update_http_rail_is_refused(world: _World, name: str) -> None:
    code, _, err = world.run("providers", name, "--update")
    assert code == 2 and name in err
    assert world.calls == []


def test_update_needs_a_model_for_its_proofs_unless_no_prove(world: _World) -> None:
    (world.home / ".config" / "ha" / "models.toml").write_text('claude = "m-claude"\n')
    code, _, err = world.run("providers", "codex", "--update")
    assert code == 2 and "codex" in err and "models.toml" in err
    assert world.calls == []
    code, _, err = world.run("providers", "codex", "--update", "--no-prove")
    assert code == 0, err
    assert len(world.calls) == 1


# ── what the machinery is handed ─────────────────────────────────────────────


def test_update_hands_the_selection_and_the_flags_through(world: _World) -> None:
    code, _, err = world.run("providers", "opencode", "codex", "--update", "--wait", "2.5")
    assert code == 0, err
    (call,) = world.calls
    assert call["rails"] == ["opencode", "codex"]
    assert call["check"] is False and call["prove_after"] is True
    assert call["wait"].seconds == 2.5
    assert call["models"] == {"opencode": "m-opencode", "codex": "m-codex"}
    assert call["state"] == world.state and call["home"] == world.home
    world.calls.clear()
    code, _, _ = world.run("providers", "--update", "--no-prove")
    assert code == 0
    (call,) = world.calls
    assert call["rails"] == ["claude", "codex", "agy", "opencode"]
    assert call["prove_after"] is False and call["wait"].seconds is None


def test_update_check_prints_unknown_per_rail_and_exits_0(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under --check the real machinery runs: it runs nothing and takes no lock."""
    monkeypatch.undo()
    monkeypatch.setattr(cli, "probe", _fake_probe)
    monkeypatch.setattr(updaters, "probe", _fake_probe)
    code, out, _ = world.run("providers", "--update", "--check")
    assert code == 0
    lines = out.splitlines()
    assert [line.split(":", 1)[0] for line in lines] == ["claude", "codex", "agy", "opencode"]
    assert all("update available: unknown (no vendor dry run)" in line for line in lines)
    assert "updater: /fake/bin/opencode upgrade" in lines[3]
    assert "rollback: opencode upgrade 1.18.30" in lines[3]
    assert not (world.state / "updates").exists(), "an updater ran"


# ── the report and the exit code ─────────────────────────────────────────────


def test_update_text_names_versions_updater_verdicts_mode_and_rollback(world: _World) -> None:
    kept = Path("/home/x/.codex/packages/standalone/releases/1.0.0-x/bin/codex")
    world.rows = [
        _row(
            "codex",
            exit_code=1,
            status="failed",
            verdicts=(
                _verdict("codex", "isolation"),
                _verdict("codex", "confinement", "inconclusive", False),
            ),
            rollback_path=kept,
        ),
        _row(
            "agy",
            new_version=None,
            exit_code=None,
            log=None,
            verdicts=(),
            status="not installed",
            note="not installed: agy: not found",
        ),
    ]
    code, out, _ = world.run("providers", "--update")
    codex, agy = out.splitlines()
    assert codex.startswith("codex: 1.0.0 -> 2.0.0 (failed); updater exit 1 (log ")
    assert "isolation passed, recorded" in codex
    assert "confinement inconclusive, not recorded (inconclusive reason)" in codex
    assert "mode writes serialised" in codex
    assert codex.endswith(f"rollback: {kept}")
    assert agy == "agy: not installed: agy: not found"
    assert code == 1


def test_update_json_report_shape(world: _World) -> None:
    world.rows = [_row("claude", rollback_command="claude install 1.0.0")]
    code, out, _ = world.run("providers", "--update", "--json")
    assert code == 0
    report = json.loads(out)
    assert report["schema"] == 1
    (row,) = report["rails"]
    assert row == updaters.UpdateRow.to_dict(world.rows[0])
    assert row["status"] == "updated" and row["log"] == "/state/updates/stamp/claude.log"
    assert row["verdicts"][0]["outcome"] == "passed"


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, 0),
        ({"status": "unchanged", "new_version": "1.0.0", "verdicts": ()}, 0),
        (
            {
                "status": "not installed",
                "new_version": None,
                "exit_code": None,
                "log": None,
                "verdicts": (),
            },
            0,
        ),
        ({"verdicts": (_verdict("claude", "isolation", "failed", True),)}, 1),
        ({"verdicts": (_verdict("claude", "isolation", "inconclusive", False),)}, 1),
        ({"verdicts": (_verdict("claude", "isolation", "passed", False),)}, 1),
        ({"status": "failed", "exit_code": 1}, 1),
        ({"status": "timed out", "exit_code": None}, 1),
        ({"status": "not updated", "exit_code": None, "log": None, "verdicts": ()}, 1),
        # A changed version reported with no verdict at all -- a dev-install
        # refusal, say -- must not read as success: the note explains the skip,
        # the exit code must not hide it. See
        # test_update_exit_code_is_1_when_a_changed_rail_was_never_proven below
        # for the named, single-purpose version of this case.
        ({"verdicts": ()}, 1),
    ],
)
def test_update_exit_code_is_1_on_a_failed_proof_or_updater(
    world: _World, fields: dict[str, Any], expected: int
) -> None:
    world.rows = [_row("claude", **fields)]
    code, _, _ = world.run("providers", "--update")
    assert code == expected


def test_update_exit_code_is_1_when_a_changed_rail_was_never_proven(world: _World) -> None:
    """A dev-install refusal (or any other reason proving never ran) leaves a
    changed rail with no verdict at all; the note explains why, but the exit
    code must still say 1, not 0 -- the note is not read by a caller checking
    only ``$?``."""
    world.rows = [
        _row(
            "claude",
            verdicts=(),
            note="not proven: development install (...)",
        )
    ]
    code, _, err = world.run("providers", "--update")
    assert code == 1, err


def test_update_exit_code_is_0_when_every_required_proof_passed(world: _World) -> None:
    world.rows = [_row("claude")]  # default: updated, one passed+recorded verdict
    code, _, err = world.run("providers", "--update")
    assert code == 0, err


def test_update_no_prove_exits_0_without_proving(world: _World) -> None:
    """``--no-prove``: no verdict is produced BY DESIGN, not by a skip -- exit 0."""
    world.rows = [_row("claude", verdicts=())]
    code, _, err = world.run("providers", "--update", "--no-prove")
    assert code == 0, err


# ── plain ha providers: byte for byte what lot 2 printed ─────────────────────


def test_plain_providers_is_unchanged_by_the_new_flags(world: _World) -> None:
    """The fixtures were generated by lot 2's ``ha providers``, before this lot
    touched the parser, in the world built here."""
    state = world.state
    record_proof(
        state, "claude", version="2.1.282 (Claude Code)", isolation=True, today="2026-09-25"
    )
    record_proof(
        state,
        "codex",
        version="codex-cli 0.156.0",
        isolation=True,
        confinement=True,
        today="2026-09-27",
    )
    record_proof(state, "agy", version="1.2.12", isolation=True, today="2026-09-26")
    code, out, _ = world.run("providers")
    assert code == 0
    assert out == (FIXTURES / "lot2-providers.txt").read_text()
    code, out, _ = world.run("providers", "--json")
    assert code == 0
    assert out == (FIXTURES / "lot2-providers.json").read_text()
    assert world.calls == []
