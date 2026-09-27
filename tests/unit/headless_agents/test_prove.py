"""``headless_agents.prove``: the live proof harness, inside the package (spec 0.5.2 §3.4, lot 4a).

Every test replaces the provider run with a fake ``RunRail`` and the version probe
with a fake one: the harness's own rules -- what counts as a leak, a refusal, an
inconclusive run, a moved version -- are exercised without a provider or a token.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from headless_agents import prove as prove_module
from headless_agents.proofs import CLI_RAILS, plant_confinement_targets, proof_path, read_proof
from headless_agents.providers.codex import CodexProvider
from headless_agents.registry import Probe
from headless_agents.result import RunResult
from headless_agents.spec import RunSpec

Call = tuple[str, str, bool]


def _result(spec: RunSpec, *, exit_code: int = 0, text: str = "NONE") -> RunResult:
    return RunResult(
        exit_code=exit_code,
        provider="fake",
        model=spec.model,
        report_path=None,
        events_log=None,
        tokens=None,
        duration_seconds=0.0,
        tool_call_completed=False,
        text=text,
    )


@pytest.fixture
def versions(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The versions the fake probe answers, in order; the last one repeats."""
    answers = ["1.0"]
    seen: list[int] = [0]

    def fake_probe(rail: str, **_: object) -> Probe:
        index = min(seen[0], len(answers) - 1)
        seen[0] += 1
        return Probe(available=True, detail=f"/fake/{rail}", version=f"{rail} {answers[index]}")

    monkeypatch.setattr(prove_module, "probe", fake_probe)
    return answers


def _prove(
    tmp_path: Path, rail: str, kind: prove_module.Kind, run: prove_module.RunRail
) -> prove_module.Verdict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return prove_module.prove(
        rail,
        kind,
        model="m",
        state=tmp_path / "state",
        home=home,
        # This suite exercises the harness's own rules (leaks, refusals, moved
        # versions), never the checkout guard -- opted out by default so it stays
        # deterministic whether this venv's own headless-agents install is editable
        # or not. The guard itself is exercised directly, below.
        environ={"PATH": "/usr/bin:/bin", "HA_PROVE_FROM_CHECKOUT": "1"},
        root=tmp_path / "root",
        run=run,
    )


def _recorder(
    answer: Callable[[str, RunSpec, bool], RunResult],
) -> tuple[list[Call], prove_module.RunRail]:
    calls: list[Call] = []

    def run(rail: str, spec: RunSpec, keep_rollout: bool) -> RunResult:
        calls.append((rail, spec.name, keep_rollout))
        return answer(rail, spec, keep_rollout)

    return calls, run


def _planted_marker(spec: RunSpec, kind: str) -> str:
    """The marker the harness planted for this run (USER in the HOME, REPO in the workspace)."""
    assert spec.environment is not None
    home = Path(spec.environment["HOME"])
    roots = [home, home.parent / "workspace"]
    for root in roots:
        for path in root.rglob("*.md"):
            found = re.search(rf"MARKER-{kind}[A-Z0-9]+", path.read_text())
            if found:
                return found.group(0)
    raise AssertionError(f"no {kind} marker planted")


# ── isolation ─────────────────────────────────────────────────────────────────


def test_isolation_passes_and_is_recorded_when_nothing_leaks(
    tmp_path: Path, versions: list[str]
) -> None:
    calls, run = _recorder(lambda rail, spec, keep: _result(spec))
    verdict = _prove(tmp_path, "codex", "isolation", run)
    assert verdict.outcome == "passed" and verdict.recorded
    record = read_proof(tmp_path / "state", "codex")
    assert record is not None and record.isolation is not None and record.isolation.passed
    assert record.version == "codex 1.0"
    assert verdict.runs == len(calls) == 2


def test_isolation_fails_on_the_user_marker_in_the_answer(
    tmp_path: Path, versions: list[str]
) -> None:
    _, run = _recorder(lambda rail, spec, keep: _result(spec, text=_planted_marker(spec, "USER")))
    verdict = _prove(tmp_path, "codex", "isolation", run)
    assert verdict.outcome == "failed" and verdict.recorded
    assert "instruction files or skills reached the model" in verdict.reason
    record = read_proof(tmp_path / "state", "codex")
    assert record is not None and record.isolation is not None
    assert record.isolation.passed is False


def test_isolation_fails_when_a_sentinel_exists(tmp_path: Path, versions: list[str]) -> None:
    def touch_the_hook_sentinel(rail: str, spec: RunSpec, keep: bool) -> RunResult:
        assert spec.environment is not None
        (Path(spec.environment["HOME"]).parent / "sentinel-hook").touch()
        return _result(spec)

    _, run = _recorder(touch_the_hook_sentinel)
    verdict = _prove(tmp_path, "opencode", "isolation", run)
    assert verdict.outcome == "failed" and verdict.recorded
    assert "sentinel-hook was created" in verdict.reason


def test_a_sentinel_counts_even_when_the_run_failed(tmp_path: Path, versions: list[str]) -> None:
    """A hook that ran is a leak whatever the exit code: a failed run is no excuse."""

    def touch_then_fail(rail: str, spec: RunSpec, keep: bool) -> RunResult:
        assert spec.environment is not None
        (Path(spec.environment["HOME"]).parent / "sentinel-mcp").touch()
        return _result(spec, exit_code=1, text="")

    _, run = _recorder(touch_then_fail)
    verdict = _prove(tmp_path, "claude", "isolation", run)
    assert verdict.outcome == "failed"


def test_an_isolation_run_that_fails_without_a_leak_is_inconclusive(
    tmp_path: Path, versions: list[str]
) -> None:
    """Orchestrator default 2 (lot 4 plan): quota, a retired model or a network error
    proves nothing either way -- it must never switch a proven rail off."""
    _, run = _recorder(lambda rail, spec, keep: _result(spec, exit_code=1, text=""))
    verdict = _prove(tmp_path, "codex", "isolation", run)
    assert verdict.outcome == "inconclusive" and not verdict.recorded
    assert "exit 1" in verdict.reason
    assert not proof_path(tmp_path / "state", "codex").exists()


def test_a_version_that_moves_during_the_proof_records_nothing(
    tmp_path: Path, versions: list[str]
) -> None:
    versions[:] = ["1.0", "1.1"]
    _, run = _recorder(lambda rail, spec, keep: _result(spec))
    verdict = _prove(tmp_path, "claude", "isolation", run)
    assert verdict.outcome == "inconclusive" and not verdict.recorded
    assert "version moved during the proof: claude 1.0 -> claude 1.1" in verdict.reason
    assert not proof_path(tmp_path / "state", "claude").exists()


def test_an_unavailable_rail_is_skipped_without_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        prove_module, "probe", lambda rail, **_: Probe(available=False, detail="not found")
    )
    calls, run = _recorder(lambda rail, spec, keep: _result(spec))
    verdict = _prove(tmp_path, "agy", "isolation", run)
    assert verdict.outcome == "skipped" and "not found" in verdict.reason
    assert calls == []


def test_prove_refuses_isolation_from_a_checkout_before_any_run(
    tmp_path: Path, versions: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The entry point enforces its own guard: a caller that forgets to check
    ``checkout_refusal`` first (unlike the CLI and the live tests) must not be able to
    record an isolation proof under the checkout's own fingerprint (review round 1)."""
    monkeypatch.setattr(prove_module, "running_from_checkout", lambda: True)
    home = tmp_path / "home"
    home.mkdir()
    calls, run = _recorder(lambda rail, spec, keep: _result(spec))
    verdict = prove_module.prove(
        "codex",
        "isolation",
        model="m",
        state=tmp_path / "state",
        home=home,
        environ={"PATH": "/usr/bin:/bin"},  # no HA_PROVE_FROM_CHECKOUT
        root=tmp_path / "root",
        run=run,
    )
    assert verdict.outcome == "skipped" and not verdict.recorded
    assert "HA_PROVE_FROM_CHECKOUT=1" in verdict.reason
    assert calls == []
    assert not proof_path(tmp_path / "state", "codex").exists()


def test_prove_proceeds_from_a_checkout_when_the_operator_opts_in(
    tmp_path: Path, versions: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prove_module, "running_from_checkout", lambda: True)
    home = tmp_path / "home"
    home.mkdir()
    calls, run = _recorder(lambda rail, spec, keep: _result(spec))
    verdict = prove_module.prove(
        "codex",
        "isolation",
        model="m",
        state=tmp_path / "state",
        home=home,
        environ={"PATH": "/usr/bin:/bin", "HA_PROVE_FROM_CHECKOUT": "1"},
        root=tmp_path / "root",
        run=run,
    )
    assert verdict.outcome == "passed" and verdict.recorded
    assert len(calls) == 2


def test_prove_records_nothing_when_neither_probe_reads_a_concrete_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both probes ``available``, neither returning version text: recording would name no
    version at all (review round 1, item 2)."""
    monkeypatch.setattr(
        prove_module, "probe", lambda rail, **_: Probe(available=True, detail="ok", version=None)
    )
    _, run = _recorder(lambda rail, spec, keep: _result(spec))
    verdict = _prove(tmp_path, "codex", "isolation", run)
    assert verdict.outcome == "inconclusive" and not verdict.recorded
    assert "concrete" in verdict.reason
    assert not proof_path(tmp_path / "state", "codex").exists()


def test_prove_records_nothing_when_the_version_string_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        prove_module, "probe", lambda rail, **_: Probe(available=True, detail="ok", version="")
    )
    _, run = _recorder(lambda rail, spec, keep: _result(spec))
    verdict = _prove(tmp_path, "codex", "isolation", run)
    assert verdict.outcome == "inconclusive" and not verdict.recorded
    assert "concrete" in verdict.reason
    assert not proof_path(tmp_path / "state", "codex").exists()


def test_prove_records_nothing_when_the_rail_becomes_unavailable_after_the_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``available`` probe before the runs and an unavailable one after must not
    record a pass: the rail is no longer verifiably there (review round 1, item 2)."""
    seen = [0]

    def fake_probe(rail: str, **_: object) -> Probe:
        seen[0] += 1
        if seen[0] == 1:
            return Probe(available=True, detail="ok", version="codex 1.0")
        return Probe(available=False, detail="vanished")

    monkeypatch.setattr(prove_module, "probe", fake_probe)
    _, run = _recorder(lambda rail, spec, keep: _result(spec))
    verdict = _prove(tmp_path, "codex", "isolation", run)
    assert verdict.outcome == "inconclusive" and not verdict.recorded
    assert "unavailable" in verdict.reason
    assert not proof_path(tmp_path / "state", "codex").exists()


# ── confinement ───────────────────────────────────────────────────────────────


def _prompt_paths(spec: RunSpec) -> tuple[str, Path, Path]:
    """The line, the control and the target the confinement prompt names."""
    line = re.search(r"^Line: (\S+)$", spec.prompt, re.M)
    first = re.search(r"^1\. (\S+?):?\s", spec.prompt, re.M)
    second = re.search(r"^2\. (\S+?):?\s", spec.prompt, re.M)
    assert line and first and second, spec.prompt
    return line.group(1), Path(first.group(1)), Path(second.group(1))


def _opencode_refused(rail: str, spec: RunSpec, keep: bool) -> RunResult:
    line, control, target = _prompt_paths(spec)
    with control.open("a") as stream:
        stream.write(line + "\n")
    assert spec.run_dir is not None
    spec.run_dir.mkdir(parents=True, exist_ok=True)
    part = {
        "type": "tool",
        "tool": "edit",
        "state": {
            "status": "error",
            "input": {"filePath": str(target)},
            "error": "The user has specified a rule which prevents you from using this tool",
        },
    }
    (spec.run_dir / "events.jsonl").write_text(json.dumps({"part": part}) + "\n")
    return _result(spec, text="refused")


def test_confinement_passes_on_opencode_refusal_evidence(
    tmp_path: Path, versions: list[str]
) -> None:
    calls, run = _recorder(_opencode_refused)
    verdict = _prove(tmp_path, "opencode", "confinement", run)
    assert verdict.outcome == "passed" and verdict.recorded, verdict.reason
    record = read_proof(tmp_path / "state", "opencode")
    assert record is not None and record.confinement is not None and record.confinement.passed
    assert verdict.runs == len(calls) == 3


def test_a_changed_target_fails_even_when_the_run_crashes(
    tmp_path: Path, versions: list[str]
) -> None:
    def write_outside_then_crash(rail: str, spec: RunSpec, keep: bool) -> RunResult:
        line, _, target = _prompt_paths(spec)
        with target.open("a") as stream:
            stream.write(line + "\n")
        raise RuntimeError("the provider crashed")

    _, run = _recorder(write_outside_then_crash)
    verdict = _prove(tmp_path, "opencode", "confinement", run)
    assert verdict.outcome == "failed" and verdict.recorded
    record = read_proof(tmp_path / "state", "opencode")
    assert record is not None and record.confinement is not None
    assert record.confinement.passed is False


def test_confinement_without_evidence_is_inconclusive_and_records_nothing(
    tmp_path: Path, versions: list[str]
) -> None:
    def control_only(rail: str, spec: RunSpec, keep: bool) -> RunResult:
        line, control, _ = _prompt_paths(spec)
        with control.open("a") as stream:
            stream.write(line + "\n")
        return _result(spec, text="I was refused, trust me")

    _, run = _recorder(control_only)
    verdict = _prove(tmp_path, "opencode", "confinement", run)
    assert verdict.outcome == "inconclusive" and not verdict.recorded
    assert not proof_path(tmp_path / "state", "opencode").exists()


def test_claude_confinement_is_skipped_without_a_provider_call(
    tmp_path: Path, versions: list[str]
) -> None:
    calls, run = _recorder(lambda rail, spec, keep: _result(spec))
    verdict = _prove(tmp_path, "claude", "confinement", run)
    assert verdict.outcome == "skipped" and not verdict.recorded
    assert "Q91=b" in verdict.reason
    assert calls == []


def test_codex_confinement_goes_through_the_rollout_entry_point(
    tmp_path: Path, versions: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """lot 1b: only the probe-only entry point keeps the rollout its evidence lives in."""
    spied: list[str] = []

    def spy(self: CodexProvider, spec: RunSpec) -> RunResult:
        spied.append(spec.name)
        return _result(spec, text="")

    def no_provider(rail: str) -> object:
        raise AssertionError("codex confinement must not run through get_provider")

    monkeypatch.setattr(CodexProvider, "run_with_rollout", spy)
    monkeypatch.setattr(prove_module, "get_provider", no_provider)
    home = tmp_path / "home"
    home.mkdir()
    verdict = prove_module.prove(
        "codex",
        "confinement",
        model="m",
        state=tmp_path / "state",
        home=home,
        environ={"PATH": "/usr/bin:/bin"},
        root=tmp_path / "root",
    )
    assert len(spied) == 5
    assert verdict.outcome == "inconclusive"


@pytest.mark.parametrize("kind", ["isolation", "confinement"])
@pytest.mark.parametrize("rail", CLI_RAILS)
def test_planned_runs_equals_the_provider_calls(
    tmp_path: Path, versions: list[str], rail: str, kind: prove_module.Kind
) -> None:
    calls, run = _recorder(lambda rail_, spec, keep: _result(spec))
    verdict = _prove(tmp_path, rail, kind, run)
    assert len(calls) == prove_module.planned_runs(rail, kind) == verdict.runs
    assert all(keep == (rail == "codex" and kind == "confinement") for _, _, keep in calls)


@pytest.mark.parametrize("rail", CLI_RAILS)
def test_the_planned_confinement_runs_are_the_planted_targets(tmp_path: Path, rail: str) -> None:
    """planned_runs counts from the same names plant_confinement_targets plants."""
    targets = plant_confinement_targets(tmp_path / "targets", rail)
    try:
        outside = [name for name in targets if name not in ("workspace", "control")]
        expected = 0 if rail == "claude" else len(outside)
        assert prove_module.planned_runs(rail, "confinement") == expected
    finally:
        for name, path in targets.items():
            if name.startswith("tmp_repo_"):
                subprocess.run(["rm", "-rf", str(path.parent.parent)], check=False)


# ── running_from_checkout ─────────────────────────────────────────────────────


class _Distribution:
    def __init__(self, direct_url: str | None) -> None:
        self.direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        return self.direct_url if name == "direct_url.json" else None


@pytest.fixture
def distribution(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[object], None]]:
    def install(value: object) -> None:
        def fake(name: str) -> _Distribution:
            if isinstance(value, Exception):
                raise value
            return _Distribution(value if isinstance(value, str) or value is None else None)

        monkeypatch.setattr(prove_module.importlib.metadata, "distribution", fake)

    yield install


def test_running_from_checkout_reads_the_editable_flag(
    distribution: Callable[[object], None],
) -> None:
    distribution(json.dumps({"url": "file:///repo", "dir_info": {"editable": True}}))
    assert prove_module.running_from_checkout() is True
    distribution(
        json.dumps({"url": "https://github.com/x/y.git", "vcs_info": {"commit_id": "a" * 40}})
    )
    assert prove_module.running_from_checkout() is False
    distribution(None)  # installed from an index or a wheel: no direct_url.json
    assert prove_module.running_from_checkout() is False
    distribution("{not json")
    assert prove_module.running_from_checkout() is True
    distribution(prove_module.importlib.metadata.PackageNotFoundError("headless-agents"))
    assert prove_module.running_from_checkout() is True


@pytest.mark.parametrize(
    ("from_checkout", "variable", "refused"),
    [
        (False, None, False),
        (True, None, True),
        (True, "1", False),
        (True, "yes", True),  # only the exact "1" says both are the same source
        (True, "", True),
    ],
)
def test_isolation_is_refused_from_a_checkout_unless_the_operator_says_same_source(
    monkeypatch: pytest.MonkeyPatch, from_checkout: bool, variable: str | None, refused: bool
) -> None:
    monkeypatch.setattr(prove_module, "running_from_checkout", lambda: from_checkout)
    environ = {} if variable is None else {"HA_PROVE_FROM_CHECKOUT": variable}
    refusal = prove_module.checkout_refusal(environ)
    if refused:
        assert refusal is not None and "development install" in refusal
        assert "HA_PROVE_FROM_CHECKOUT=1" in refusal
    else:
        assert refusal is None


def test_prove_imports_no_pytest() -> None:
    probe_script = "import headless_agents.prove, sys; print('pytest' in sys.modules)"
    output = subprocess.run(
        [sys.executable, "-c", probe_script], capture_output=True, text=True, check=True
    )
    assert output.stdout.strip() == "False"
