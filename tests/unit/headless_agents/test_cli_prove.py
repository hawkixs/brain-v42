"""``ha prove``: re-prove a rail from the installed package (spec 0.5.2 §3.4, lot 4a Task A3).

``prove.prove`` is replaced by a recorder and the version probe by a fake: these
tests pin the command's own rules -- what it selects, what it refuses before
anything runs, what it announces before spending, and its exit code -- never a
provider run.
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from headless_agents import cli
from headless_agents import prove as prove_module
from headless_agents.config_paths import state_dir
from headless_agents.proofs import record_proof
from headless_agents.registry import Probe

VERSIONS = {
    "claude": "claude 1.0",
    "codex": "codex-cli 1.0",
    "agy": "agy 1.0",
    "opencode": "opencode 1.0",
}


class _Stderr(io.StringIO):
    """stderr that also logs each write into the shared event list, in order."""

    def __init__(self, events: list[tuple[str, ...]]) -> None:
        super().__init__()
        self.events = events

    def write(self, text: str) -> int:
        self.events.append(("stderr", text))
        return super().write(text)


@dataclass
class _World:
    home: Path
    environ: dict[str, str]
    events: list[tuple[str, ...]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    outcomes: dict[tuple[str, str], tuple[str, bool]] = field(default_factory=dict)
    roots: list[Path] = field(default_factory=list)

    @property
    def state(self) -> Path:
        return state_dir(self.environ, home=self.home)

    def run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), _Stderr(self.events)
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
    built = _World(home=home, environ={"PATH": "/usr/bin:/bin", "HOME": str(home)})

    def fake_probe(rail: str, **_: object) -> Probe:
        return Probe(available=True, detail=f"/fake/{rail}", version=VERSIONS.get(rail))

    def fake_prove(rail: str, kind: str, **kwargs: object) -> prove_module.Verdict:
        built.calls.append((rail, kind))
        built.events.append(("prove", rail, kind))
        root = kwargs["root"]
        assert isinstance(root, Path)
        built.roots.append(root)
        (root / f"{rail}-{kind}").mkdir(parents=True, exist_ok=True)
        outcome, recorded = built.outcomes.get((rail, kind), ("passed", True))
        return prove_module.Verdict(
            rail=rail,
            kind=kind,  # type: ignore[arg-type]
            version=VERSIONS[rail],
            outcome=outcome,  # type: ignore[arg-type]
            reason="fake",
            recorded=recorded,
            runs=prove_module.planned_runs(rail, kind),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(cli, "probe", fake_probe)
    monkeypatch.setattr(prove_module, "prove", fake_prove)
    monkeypatch.setattr(prove_module, "running_from_checkout", lambda: False)
    return built


def test_prove_announces_the_token_spend_before_the_first_provider_run(world: _World) -> None:
    code, _, err = world.run("prove", "codex", "--confinement")
    assert code == 0, err
    first = world.events[0]
    assert first[0] == "stderr"
    assert "prove codex confinement: 5 provider runs on codex-cli 1.0 (model m-codex)" in first[1]
    assert "5 provider runs in total; this spends provider tokens" in err
    assert world.events.index(("prove", "codex", "confinement")) > 1


def test_prove_announces_the_total_of_every_selected_proof(world: _World) -> None:
    code, _, err = world.run("prove", "codex", "opencode")
    assert code == 0, err
    total = sum(
        prove_module.planned_runs(rail, kind)  # type: ignore[arg-type]
        for rail in ("codex", "opencode")
        for kind in ("isolation", "confinement")
    )
    assert f"{total} provider runs in total" in err


def test_prove_stale_selects_exactly_the_rails_without_a_passing_proof(world: _World) -> None:
    state = world.state
    record_proof(state, "claude", version="claude 0.9", isolation=True)
    record_proof(state, "codex", version="codex-cli 1.0", isolation=True)
    record_proof(state, "agy", version="agy 1.0", isolation=True, confinement=True)
    record_proof(state, "opencode", version="opencode 1.0", isolation=True, confinement=True)
    code, _, err = world.run("prove", "--stale")
    assert code == 0, err
    assert world.calls == [("claude", "isolation"), ("codex", "confinement")]


def test_prove_stale_with_nothing_stale_runs_nothing_and_exits_0(world: _World) -> None:
    state = world.state
    record_proof(state, "claude", version="claude 1.0", isolation=True)
    for rail in ("codex", "agy", "opencode"):
        record_proof(state, rail, version=VERSIONS[rail], isolation=True, confinement=True)
    code, out, _ = world.run("prove", "--stale")
    assert code == 0
    assert "nothing to prove" in out
    assert world.calls == []


@pytest.mark.parametrize("name", ["openrouter", "nope"])
def test_prove_an_http_or_unknown_rail_is_refused(world: _World, name: str) -> None:
    code, _, err = world.run("prove", name)
    assert code == 2 and name in err
    assert world.calls == []


def test_prove_claude_confinement_named_explicitly_is_refused(world: _World) -> None:
    code, _, err = world.run("prove", "claude", "--confinement")
    assert code == 2 and "Q91=b" in err
    assert world.calls == []


def test_prove_claude_alone_proves_its_isolation_and_skips_its_confinement(
    world: _World,
) -> None:
    code, out, _ = world.run("prove", "claude")
    assert code == 0
    assert world.calls == [("claude", "isolation")]
    assert "claude confinement: skipped" in out


def test_prove_isolation_from_a_development_install_is_refused_before_any_run(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prove_module, "running_from_checkout", lambda: True)
    code, _, err = world.run("prove", "codex", "--isolation")
    assert code == 2 and "development install" in err
    assert world.calls == []
    assert "provider runs" not in err
    code, _, err = world.run("prove", "codex", "--confinement")
    assert code == 0, err
    assert world.calls == [("codex", "confinement")]
    world.environ["HA_PROVE_FROM_CHECKOUT"] = "1"
    code, _, err = world.run("prove", "codex", "--isolation")
    assert code == 0, err
    assert world.calls[-1] == ("codex", "isolation")


@pytest.mark.parametrize(
    ("outcome", "recorded"), [("failed", True), ("inconclusive", False), ("passed", False)]
)
def test_prove_exits_1_when_any_verdict_is_not_passed_and_recorded(
    world: _World, outcome: str, recorded: bool
) -> None:
    world.outcomes[("opencode", "confinement")] = (outcome, recorded)
    code, out, _ = world.run("prove", "opencode")
    assert code == 1
    assert f"opencode confinement: {outcome}" in out


def test_prove_json_has_one_verdict_per_rail_and_kind_and_the_modes(world: _World) -> None:
    code, out, _ = world.run("prove", "opencode", "codex", "--json")
    assert code == 0
    report = json.loads(out)
    assert report["schema"] == 1
    assert [(v["rail"], v["kind"]) for v in report["verdicts"]] == [
        ("opencode", "isolation"),
        ("opencode", "confinement"),
        ("codex", "isolation"),
        ("codex", "confinement"),
    ]
    assert set(report["modes"]) == {"opencode", "codex"}


def test_prove_needs_a_model_for_a_rail_without_a_default(world: _World) -> None:
    (world.home / ".config" / "ha" / "models.toml").write_text('claude = "m-claude"\n')
    code, _, err = world.run("prove", "codex")
    assert code == 2 and "models.toml" in err and "codex" in err
    assert world.calls == []
    code, _, err = world.run("prove", "agy")  # agy chooses its own model
    assert code == 0, err


def test_an_unavailable_rail_is_refused_when_named_and_skipped_otherwise(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def probe_without_agy(rail: str, **_: object) -> Probe:
        if rail == "agy":
            return Probe(available=False, detail="agy: not found or not executable")
        return Probe(available=True, detail=f"/fake/{rail}", version=VERSIONS[rail])

    monkeypatch.setattr(cli, "probe", probe_without_agy)
    code, _, err = world.run("prove", "agy")
    assert code == 2 and "not found" in err
    assert world.calls == []
    code, out, _ = world.run("prove")
    assert code == 0
    assert "agy isolation: skipped" in out
    assert ("agy", "isolation") not in world.calls
    assert ("codex", "isolation") in world.calls


def test_prove_stale_leaves_out_a_rail_that_is_not_installed(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def probe_without_agy(rail: str, **_: object) -> Probe:
        if rail == "agy":
            return Probe(available=False, detail="agy: not found or not executable")
        return Probe(available=True, detail=f"/fake/{rail}", version=VERSIONS[rail])

    monkeypatch.setattr(cli, "probe", probe_without_agy)
    code, out, _ = world.run("prove", "--stale")
    assert code == 0
    assert [rail for rail, _ in world.calls] == ["claude", "codex", "codex", "opencode", "opencode"]
    assert "agy" not in out


def test_with_no_rail_installed_every_proof_reads_skipped_and_nothing_is_announced(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def nothing_installed(rail: str, **_: object) -> Probe:
        return Probe(available=False, detail=f"{rail}: not found or not executable")

    monkeypatch.setattr(cli, "probe", nothing_installed)
    code, out, err = world.run("prove")
    assert code == 0
    assert world.calls == []
    assert "provider runs" not in err
    assert "nothing to prove" not in out
    assert "claude confinement: skipped (unprovable: claude's tool log names no path" in out
    assert "codex isolation: skipped (codex: not found or not executable)" in out


def test_prove_removes_its_root_unless_keep(world: _World) -> None:
    world.run("prove", "opencode", "--isolation")
    (root,) = set(world.roots)
    assert not root.exists()
    code, out, _ = world.run("prove", "opencode", "--isolation", "--keep")
    assert code == 0
    kept = world.roots[-1]
    assert kept.is_dir() and str(kept) in out
