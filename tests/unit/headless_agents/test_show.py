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


def test_a_cleaned_run_takes_nothing_from_its_reused_directory(home: Home) -> None:
    """Final review of PR B: ha clean sets cleaned_at once the directory is gone, so a later
    run given the same --run-dir owns whatever stands there now."""
    run_dir = home.read_only()
    shutil.rmtree(run_dir)
    home.registry().set_cleaned(RUN, "2026-09-25T13:00:00Z")
    run_dir.mkdir()
    (run_dir / "prompt.md").write_text("Another run's task.\n")
    (run_dir / "run.json").write_text(json.dumps(home.report(run_id=OTHER, text="theirs")))
    shown = home.rebuild()
    assert shown.task is None and shown.report["text"] is None
    assert any("removed by ha clean at 2026-09-25T13:00:00Z" in note for note in shown.notes)


def test_a_report_naming_another_run_disowns_its_directory(home: Home) -> None:
    """Final review of PR B: prompt.md and change.patch name no run -- beside a report
    naming another run, they are that run's too."""
    run_dir = home.write_run(run_id=OTHER)
    (run_dir / "prompt.md").write_text("Another run's task.\n")
    (run_dir / "change.patch").write_text("diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b\n")
    shown = home.rebuild()
    assert shown.task is None and shown.diffstat is None


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


def test_a_run_outside_any_lineage_takes_no_write_field_from_its_report(home: Home) -> None:
    """Final review of PR B: the entry names no lineage, and the engine writes these fields
    for a write run only -- a report keeping its run_id but claiming a branch shows none."""
    home.read_only(
        lineage=RUN,
        branch=f"ha/{RUN}",
        base=BASE,
        head=SHA_1,
        commits=[{"sha": SHA_1, "made_by": "engine"}],
    )
    shown = home.rebuild()
    assert all(
        shown.report[key] is None for key in ("lineage", "branch", "base", "head", "commits")
    )
    assert not any(line.startswith("head") for line in show.render(shown).splitlines())


def test_an_unreadable_lineage_takes_no_branch_from_the_report(home: Home) -> None:
    """Final review of PR B: the lineage state is a write's one authority for its branch and
    base; when it cannot be read, the report does not stand in for it."""
    home.write_run(lineage="forged", branch="ha/forged", base="f" * 40)
    lineages.lineage_path(home.state, RUN).write_text("{not json")
    report = home.rebuild().report
    assert report["lineage"] == RUN and report["branch"] is None and report["base"] is None


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


def test_a_target_that_is_not_text_is_unknown(home: Home) -> None:
    """Codex review of PR B (round 1): a mistyped target showed as a run of target "None"."""
    home.read_only()
    path = home.state / "runs" / f"{RUN}.json"
    document = json.loads(path.read_text())
    document["target"] = {"kind": [], "name": None}
    path.write_text(json.dumps(document))
    shown = home.rebuild()
    assert shown.report["status"] == "unknown" and shown.report["target"] is None


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


# ── rendering (spec §3.10; plan P8: column widths per table) ───────────────

HEAD = "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b"


def _shown(
    report: dict[str, object],
    *,
    task: str | None = None,
    diffstat: show.Diffstat | None = None,
    warnings: tuple[str, ...] = (),
) -> show.Shown:
    return show.Shown(
        report=report, task=task, diffstat=diffstat, notes=(), warnings=warnings, unknown=False
    )


GOLDEN_READ_ONLY = """\
20260925T120000-ab12cd34  codex  exit 0  answered
task    Explain what this repository does.
  run  codex  codex  gpt-6-luna  0  1m05s  in 12k out 2k  $0.03  command_execution 4, mcp_tool_call 1
--- run ---
It is a persistent memory server.
"""


def test_a_read_only_run_renders_as_the_golden_text(home: Home) -> None:
    report = home.report()
    rendered = show.render(_shown(report, task="Explain what this repository does."))
    assert rendered == GOLDEN_READ_ONLY


GOLDEN_WRITE = """\
20260925T120000-ab12cd34  implementer  exit 0  committed
task    Fix the typo in README.
head    ha/20260925T120000-ab12cd34 @ 1a2b3c4  1 commit  +3 -1  1 file
  run  implementer  opencode  opencode-go/deepseek-v4.1-flash  0  3m10s  in 610k out 21k  $0.08  read 25, edit 11, bash 6
--- run ---
Fixed the typo.
"""


def test_a_write_run_renders_as_the_golden_text(home: Home) -> None:
    step = dict(
        _STEP,
        role="implementer",
        provider="opencode",
        model="opencode-go/deepseek-v4.1-flash",
        duration_seconds=190.0,
        tokens={"input": 610000, "output": 21000, "fresh": None, "cached": None, "thinking": None},
        cost_usd=0.08,
        tools={"read": 25, "edit": 11, "bash": 6},
    )
    report = home.report(
        target={"kind": "role", "name": "implementer"},
        status="committed",
        text="Fixed the typo.\n",
        branch=f"ha/{RUN}",
        head=HEAD,
        commits=[{"sha": HEAD, "made_by": "engine"}],
        steps=[step],
    )
    stat = show.Diffstat(insertions=3, deletions=1, files=1)
    rendered = show.render(_shown(report, task="Fix the typo in README.", diffstat=stat))
    assert rendered == GOLDEN_WRITE


GOLDEN_UNMEASURED = """\
20260925T120000-ab12cd34  claude  exit 1  failed
task    -
  run  claude  claude  sonnet  1  12s  -  -  -
"""


def test_what_was_not_measured_renders_as_a_dash(home: Home) -> None:
    step = dict(
        _STEP,
        role="claude",
        provider="claude",
        model="sonnet",
        exit_code=1,
        duration_seconds=12.0,
        tokens=None,
        cost_usd=None,
        tools=None,
    )
    report = home.report(
        target={"kind": "provider", "name": "claude"},
        status="failed",
        exit_code=1,
        text=None,
        steps=[step],
    )
    assert show.render(_shown(report)) == GOLDEN_UNMEASURED


GOLDEN_TWO_STEPS = """\
20260925T120000-ab12cd34  multi-review  exit 6  changes requested
task    Review this change.
  review  reviewer-agy  agy     (auto)  0  2m40s  -              -      view_file 9  CHANGES
  judge   judge         claude  opus    0  1m10s  in 90k out 3k  $0.40  -            CHANGES
--- judge ---
Rename the helper.
"""


def test_columns_align_across_steps(home: Home) -> None:
    """Pins the table layout lot 4's reviews will fill (plan P8)."""
    steps = [
        {
            "slot": "review",
            "role": "reviewer-agy",
            "provider": "agy",
            "model": None,
            "exit_code": 0,
            "duration_seconds": 160.0,
            "tokens": None,
            "cost_usd": None,
            "tools": {"view_file": 9},
            "verdict": "CHANGES",
        },
        {
            "slot": "judge",
            "role": "judge",
            "provider": "claude",
            "model": "opus",
            "exit_code": 0,
            "duration_seconds": 70.0,
            "tokens": {"input": 90000, "output": 3000},
            "cost_usd": 0.4,
            "tools": None,
            "verdict": "CHANGES",
        },
    ]
    report = home.report(
        target={"kind": "workflow", "name": "multi-review"},
        status="changes",
        exit_code=6,
        text="Rename the helper.\n",
        steps=steps,
    )
    assert show.render(_shown(report, task="Review this change.")) == GOLDEN_TWO_STEPS


def test_the_header_names_a_failure_reason_and_a_warning_line_follows(home: Home) -> None:
    report = home.report(
        status="failed",
        exit_code=1,
        failure_reason="agent_moved_head",
        branch=f"ha/{RUN}",
        head=None,
        commits=[],
        steps=[],
        text=None,
    )
    warning = f"lineage {RUN} is compromised: agent_moved_head"
    text = show.render(_shown(report, warnings=(warning,)))
    assert text.splitlines() == [
        f"{RUN}  codex  exit 1  failed (agent_moved_head)",
        "task    -",
        f"head    ha/{RUN} @ -  0 commits",
        f"warning lineage {RUN} is compromised: agent_moved_head",
    ]


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(None, "-"), (0.4, "0s"), (59.4, "59s"), (65.4, "1m05s"), (3725, "1h02m")],
)
def test_format_duration(seconds: object, text: str) -> None:
    assert show.format_duration(seconds) == text


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), 10**400],
    ids=["nan", "inf", "-inf", "int-too-large-for-a-float"],
)
def test_a_measure_that_is_not_a_finite_number_renders_as_a_dash(value: object) -> None:
    """Final review of PR B: ``round`` raises on NaN and infinity, ``:.2f`` on an int too
    large for a float -- one such number in a report broke the whole ``ha runs`` listing."""
    assert show.format_duration(value) == "-" and show.format_cost(value) == "-"


def test_a_token_count_too_large_for_a_float_renders_as_a_dash() -> None:
    """Codex review of PR B (round 2): _thousands divided it as a float and raised."""
    assert show.format_tokens({"input": 10**400, "output": 3000}) == "in - out 3k"


def test_a_report_nested_too_deep_to_parse_cannot_be_read(home: Home) -> None:
    """Codex review of PR B (round 2), the same input class: json.loads raises
    RecursionError on a document nested too deep, and it escaped as a crash of ha show."""
    run_dir = home.read_only()
    (run_dir / "run.json").write_text("[" * 100_000 + "]" * 100_000)
    shown = home.rebuild()
    assert shown.report["status"] == "answered" and shown.task is not None
    assert any("cannot be read" in note for note in shown.notes)


def test_a_number_that_is_not_finite_in_a_report_is_not_measured(home: Home) -> None:
    """Final review of PR B: ``json.loads`` reads NaN and Infinity, and ``1e999`` as
    infinity; ``--json`` wrote them back out as ``NaN``, which a strict parser refuses."""
    run_dir = home.read_only()
    path = run_dir / "run.json"
    text = path.read_text().replace('"duration_seconds": 65.4', '"duration_seconds": NaN')
    path.write_text(text.replace('"cost_usd": 0.031', '"cost_usd": 1e999'))
    report = home.rebuild().report
    (step,) = report["steps"] if isinstance(report["steps"], list) else [{}]
    assert report["duration_seconds"] is None and report["cost_usd"] is None
    assert step["duration_seconds"] is None and step["cost_usd"] is None


def test_format_tools_and_tokens() -> None:
    assert show.format_tools(None) == "-" and show.format_tools({}) == "none"
    assert show.format_tools({"edit": 11, "read": 25, "bash": 11}) == "read 25, bash 11, edit 11"
    assert show.format_tokens(None) == "-"
    assert show.format_tokens({"input": 999, "output": None}) == "in 999 out -"
    assert show.format_tokens({"input": 1_500_000, "output": 3000}) == "in 1.5M out 3k"
