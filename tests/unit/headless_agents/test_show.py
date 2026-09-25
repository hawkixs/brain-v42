"""``ha show``: a run rebuilt from the state directory (spec 0.5.0 §3.8.1, §3.10; plan P6)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from headless_agents import lineage as lineages
from headless_agents import locks, provenance, show
from headless_agents.report import RUN_KEYS
from headless_agents.runs import Registry

RUN = "20260925T120000-ab12cd34"
OTHER = "20260925T130000-cd34ef56"
SHA_1 = "1" * 40
SHA_2 = "2" * 40
BASE = "b" * 40

_STEP: dict[str, object] = {
    "index": 1, "slot": "run", "role": "codex", "dir": "steps/01-run-codex",
    "provider": "codex", "model": "gpt-6-luna", "model_reported": None, "exit_code": 0,
    "duration_seconds": 65.4,
    "tokens": {"input": 12345, "output": 2100, "fresh": None, "cached": None, "thinking": None},
    "cost_usd": 0.031, "tools": {"command_execution": 4, "mcp_tool_call": 1}, "verdict": None,
}  # fmt: skip


class Home:
    def __init__(self, root: Path) -> None:
        self.root = root

    @property
    def state(self) -> Path:
        return self.root / ".local" / "state" / "ha"

    @property
    def runs(self) -> Path:
        return self.root / ".cache" / "ha" / "runs"

    def registry(self) -> Registry:
        return Registry(self.state, runs_root=self.runs)

    def rebuild(self, run_id: str = RUN) -> show.Shown:
        return show.rebuild(run_id, state=self.state, runs_root=self.runs)

    def report(self, **fields: object) -> dict[str, object]:
        document: dict[str, object] = dict.fromkeys(RUN_KEYS)
        document.update(
            schema=1,
            run_id=RUN,
            target={"kind": "provider", "name": "codex"},
            status="answered",
            exit_code=0,
            text="It is a persistent memory server.\n",
            repository=str(self.root / "repo"),
            steps=[dict(_STEP)],
            cost_usd=0.031,
            cost_complete=True,
            duration_seconds=65.4,
        )
        document.update(fields)
        return document

    def read_only(self, *, entry_status: str = "answered", **report: object) -> Path:
        """A read-only run: its registry entry says ``entry_status``, its run.json ``report``."""
        registry = self.registry()
        entry = registry.create(
            RUN,
            run_dir=None,
            target={"kind": "provider", "name": "codex"},
            repository=self.root / "repo",
            lineage=None,
        )
        entry.run_dir.mkdir(parents=True)
        (entry.run_dir / "prompt.md").write_text("Explain what this repository does.\nMore.\n")
        (entry.run_dir / "run.json").write_text(json.dumps(self.report(**report)))
        registry.set_status(RUN, entry_status)
        return entry.run_dir

    def write_run(
        self, *, member: str = "committed", compromised: str | None = None, **report: object
    ) -> Path:
        registry = self.registry()
        entry = registry.create(
            RUN,
            run_dir=None,
            target={"kind": "role", "name": "implementer"},
            repository=self.root / "repo",
            lineage=RUN,
        )
        entry.run_dir.mkdir(parents=True)
        lineages.create(
            self.state,
            lineages.LineageState(
                owner=RUN,
                repository=self.root / "repo",
                common_dir=self.root / "repo" / ".git",
                worktree=entry.run_dir / "wt",
                branch=f"ha/{RUN}",
                base=BASE,
                members={RUN: member},
                pending=None,
                compromised=compromised,
            ),
        )
        provenance.record(
            self.state, SHA_1, run_id=RUN, lineage=RUN, made_by="engine", providers=[]
        )
        provenance.record(
            self.state, SHA_2, run_id=OTHER, lineage=RUN, made_by="engine", providers=["codex"]
        )
        (entry.run_dir / "run.json").write_text(
            json.dumps(self.report(target={"kind": "role", "name": "implementer"}, **report))
        )
        return entry.run_dir


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return Home(tmp_path / "home")


def test_a_finished_run_is_shown_as_its_report_says(home: Home) -> None:
    home.read_only()
    shown = home.rebuild()
    assert list(shown.report) == list(RUN_KEYS)
    assert shown.report["status"] == "answered" and shown.report["exit_code"] == 0
    assert shown.report["steps"] == [_STEP]
    assert shown.task == "Explain what this repository does."
    assert shown.notes == () and shown.warnings == () and shown.unknown is False


def test_a_stale_report_is_rebuilt_from_the_registry(home: Home) -> None:
    home.read_only(entry_status="answered", status="running", exit_code=None)
    shown = home.rebuild()
    assert shown.report["status"] == "answered"
    assert any("run.json says running; the state says answered" in note for note in shown.notes)


def test_liveness_comes_from_the_lifecycle_lock(home: Home) -> None:
    home.read_only(entry_status="running", status="running")
    assert home.rebuild().report["status"] == "incomplete"
    lock = home.registry().lifecycle_lock(RUN)
    with locks.held(lock, rank=locks.Rank.LIFECYCLE, exclusive=True, wait=None, what="test"):
        assert home.rebuild().report["status"] == "running"


def test_a_cleaned_run_is_shown_from_its_entry(home: Home) -> None:
    run_dir = home.read_only()
    shutil.rmtree(run_dir)
    home.registry().set_cleaned(RUN, "2026-09-25T13:00:00Z")
    shown = home.rebuild()
    assert shown.report["status"] == "answered"
    assert shown.report["target"] == {"kind": "provider", "name": "codex"}
    assert shown.report["steps"] == [] and shown.report["text"] is None
    assert shown.task is None and shown.unknown is False
    assert any("removed by ha clean at 2026-09-25T13:00:00Z" in note for note in shown.notes)


def test_a_report_naming_another_run_is_ignored(home: Home) -> None:
    home.read_only(run_id=OTHER, text="forged")
    shown = home.rebuild()
    assert shown.report["run_id"] == RUN and shown.report["text"] is None
    assert any("names another run" in note for note in shown.notes)


def test_identity_comes_from_the_entry_not_the_report(home: Home) -> None:
    home.read_only(target={"kind": "role", "name": "forged"})
    assert home.rebuild().report["target"] == {"kind": "provider", "name": "codex"}


def test_a_report_never_supplies_what_a_later_lots_state_record_owns(home: Home) -> None:
    """Codex review of this plan (round 2): a review's verdict and vendor check live in its
    review result in the state (spec §3.8.1, §3.8.6) -- a forged run.json must not show them."""
    forged: dict[str, object] = {
        "verdict": "APPROVE",
        "vendor_check": {"commits": [], "authors": ["codex"], "reviewers": {}},
        "cleanup": {"status": "done"},
        "continues": OTHER,
        "findings_from": OTHER,
        "implement_providers": ["codex"],
    }
    assert set(forged) == set(show.LATER_AUTHORITIES)
    home.read_only(**forged)
    report = home.rebuild().report
    assert all(report[key] is None for key in forged)


def test_a_write_run_is_rebuilt_from_its_lineage_and_provenance(home: Home) -> None:
    """A crash after the lineage rename: run.json still says running, and holds no commit."""
    home.write_run(status="running", commits=None, branch=None, base=None)
    shown = home.rebuild()
    report = shown.report
    assert report["status"] == "committed"
    assert report["lineage"] == RUN and report["branch"] == f"ha/{RUN}" and report["base"] == BASE
    assert report["commits"] == [{"sha": SHA_1, "made_by": "engine"}]
    assert shown.unknown is False


def test_a_compromised_lineage_is_a_warning(home: Home) -> None:
    home.write_run(compromised="tripwire")
    assert home.rebuild().warnings == (f"lineage {RUN} is compromised: tripwire",)


def test_an_unreadable_lineage_is_unknown(home: Home) -> None:
    home.write_run()
    lineages.lineage_path(home.state, RUN).write_text("{not json")
    shown = home.rebuild()
    assert shown.report["status"] == "unknown" and shown.unknown is True


def test_a_lineage_silent_about_the_run_is_unknown(home: Home) -> None:
    """Codex review of this plan (round 1): no member status is no status, not a live run."""
    home.write_run()
    path = lineages.lineage_path(home.state, RUN)
    document = json.loads(path.read_text())
    document["members"] = {}
    path.write_text(json.dumps(document))
    lock = home.registry().lifecycle_lock(RUN)
    with locks.held(lock, rank=locks.Rank.LIFECYCLE, exclusive=True, wait=None, what="test"):
        shown = home.rebuild()
    assert shown.report["status"] == "unknown" and shown.unknown is True
    assert any(f"the lineage {RUN} does not list this run" in note for note in shown.notes)


def test_an_unreadable_provenance_record_makes_the_commits_unknown(home: Home) -> None:
    home.write_run()
    (home.state / "provenance" / f"{'3' * 40}.json").write_text("{not json")
    shown = home.rebuild()
    assert shown.unknown is True
    assert any("provenance" in note for note in shown.notes)


def test_an_unreadable_registry_entry_is_unknown(home: Home) -> None:
    home.read_only()
    (home.state / "runs" / f"{RUN}.json").write_text("{not json")
    shown = home.rebuild()
    assert shown.report["status"] == "unknown" and shown.unknown is True


def test_a_malformed_registry_entry_is_unknown(home: Home) -> None:
    """Codex review of this plan (round 3): valid JSON without its run_dir raised KeyError."""
    home.read_only()
    path = home.state / "runs" / f"{RUN}.json"
    document = json.loads(path.read_text())
    del document["run_dir"]
    path.write_text(json.dumps(document))
    shown = home.rebuild()
    assert shown.report["status"] == "unknown" and shown.unknown is True


@pytest.mark.parametrize(
    ("run_id", "needle"),
    [("nope", "not a run id"), ("20260925T000000-00000000", "no run 20260925T000000-00000000")],
)
def test_what_is_not_a_registered_run_is_not_shown(home: Home, run_id: str, needle: str) -> None:
    with pytest.raises(show.NotShown, match=needle):
        home.rebuild(run_id)


def test_a_legacy_run_is_named_as_such(home: Home) -> None:
    legacy = home.runs / "20260920T000000-aaaaaaaa"
    legacy.mkdir(parents=True)
    (legacy / "result.json").write_text(json.dumps({"provider": "codex", "exit_code": 0}))
    with pytest.raises(
        show.NotShown, match="is a 0.4.0 run: ha runs lists it, no command accepts it"
    ):
        home.rebuild("20260920T000000-aaaaaaaa")


def test_the_diffstat_counts_lines_inside_hunks_only(home: Home) -> None:
    run_dir = home.write_run()
    (run_dir / "change.patch").write_text(
        "diff --git a/README.md b/README.md\n"
        "index 3b18e51..a0f3c9e 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1,3 +1,5 @@\n"
        " # Title\n"
        "-teh typo\n"
        "+the typo\n"
        "+++counter stays a line\n"
        "+one more\n"
    )
    assert home.rebuild().diffstat == show.Diffstat(insertions=3, deletions=1, files=1)
