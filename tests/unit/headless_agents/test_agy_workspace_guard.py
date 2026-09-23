import json
from pathlib import Path

import pytest

from headless_agents.guards.agy_workspace import decide


def _p(name, **args):
    return json.dumps({"toolCall": {"name": name, "args": args}, "stepIdx": 0})


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "in.txt").write_text("x")
    outside = tmp_path / "out.txt"
    outside.write_text("y")
    (root / "link.txt").symlink_to(outside)
    (tmp_path / "ws-evil").mkdir()
    return root


def cfg(ws, write=False, shell=False):
    return {"root": str(ws), "write": write, "shell": shell}


@pytest.mark.parametrize(
    "path, decision",
    [
        ("{ws}/in.txt", "allow"),
        ("{ws}", "allow"),
        ("{ws}/../out.txt", "deny"),
        ("{ws}/link.txt", "deny"),
        ("{ws}-evil/x", "deny"),
        ("in.txt", "deny"),
        ("", "deny"),
    ],
)
def test_view_file_confinement(ws, path, decision):
    assert decide(_p("view_file", AbsolutePath=path.format(ws=ws)), cfg(ws))["decision"] == decision


def test_writes_need_write(ws):
    assert decide(_p("write_to_file", TargetFile=f"{ws}/n.txt"), cfg(ws))["decision"] == "deny"
    assert (
        decide(_p("write_to_file", TargetFile=f"{ws}/n.txt"), cfg(ws, write=True))["decision"]
        == "allow"
    )
    assert (
        decide(_p("replace_file_content", TargetFile=f"{ws}/../o"), cfg(ws, write=True))["decision"]
        == "deny"
    )


def test_run_command_needs_shell(ws):
    assert (
        decide(_p("run_command", CommandLine="ls", Cwd=str(ws)), cfg(ws, write=True))["decision"]
        == "deny"
    )
    assert (
        decide(_p("run_command", CommandLine="ls", Cwd=str(ws)), cfg(ws, write=True, shell=True))[
            "decision"
        ]
        == "allow"
    )


@pytest.mark.parametrize("name", ["search_web", "schedule", "brand_new_tool"])
def test_everything_else_is_denied(ws, name):
    assert decide(_p(name), cfg(ws, write=True, shell=True))["decision"] == "deny"


@pytest.mark.parametrize("name", ["finish", "send_message", "call_mcp_tool"])
def test_answer_and_mcp_allowed(ws, name):
    assert decide(_p(name), cfg(ws))["decision"] == "allow"


@pytest.mark.parametrize("payload", ["", "not json", "[]", '{"toolCall": 3}'])
def test_unreadable_payload_is_denied(ws, payload):
    assert decide(payload, cfg(ws))["decision"] == "deny"


def test_bad_config_denies_everything(ws):
    assert (
        decide(_p("view_file", AbsolutePath=f"{ws}/in.txt"), {"root": "relative"})["decision"]
        == "deny"
    )
    assert decide(_p("finish"), {})["decision"] == "deny"


def test_main_reads_config_from_home(ws, tmp_path, monkeypatch, capsys):
    import io

    from headless_agents.guards import agy_workspace

    home = tmp_path / "home"
    (home / ".gemini" / "config").mkdir(parents=True)
    (home / ".gemini" / "config" / "workspace-guard.json").write_text(json.dumps(cfg(ws)))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin", io.StringIO(_p("view_file", AbsolutePath=f"{ws}/in.txt")))
    assert agy_workspace.main() == 0
    assert json.loads(capsys.readouterr().out) == {"decision": "allow"}


# `decide` is the fail-closed boundary: a config or a payload field of the
# wrong shape must fall through to a deny, never raise. These fuzz a few of
# the shapes a hand-edited config file or a future agy payload could take.
def test_decide_never_raises_on_args_not_a_dict(ws):
    payload = json.dumps({"toolCall": {"name": "view_file", "args": [1, 2]}})
    assert decide(payload, cfg(ws))["decision"] == "deny"


def test_decide_never_raises_on_absolute_path_as_int(ws):
    payload = json.dumps({"toolCall": {"name": "view_file", "args": {"AbsolutePath": 7}}})
    assert decide(payload, cfg(ws))["decision"] == "deny"


def test_decide_never_raises_on_root_as_int(ws):
    assert decide(_p("view_file", AbsolutePath=f"{ws}/in.txt"), {"root": 7})["decision"] == "deny"


def test_decide_never_raises_on_config_as_list(ws):
    assert decide(_p("finish"), [1, 2, 3])["decision"] == "deny"
    assert decide(_p("view_file", AbsolutePath=f"{ws}/in.txt"), [1, 2, 3])["decision"] == "deny"
