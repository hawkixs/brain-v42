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
    # A symlink pointing out of the workspace is a write target too.
    assert (
        decide(_p("write_to_file", TargetFile=f"{ws}/link.txt"), cfg(ws, write=True))["decision"]
        == "deny"
    )


@pytest.mark.parametrize(
    "path",
    [
        "{ws}/.git",
        "{ws}/.git/hooks/pre-commit",
        "{ws}/.GIT/config",
        "{ws}/sub/.git/config",
        "{ws}/gitlink",
    ],
)
def test_writes_under_git_are_denied(ws, path):
    """A write run must not plant a hook or rewrite a ``.git`` FILE: git runs
    them later, outside any sandbox. ``gitlink`` resolves into ``.git``."""
    (ws / ".git").mkdir()
    (ws / "gitlink").symlink_to(ws / ".git" / "config")
    for tool in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
        verdict = decide(_p(tool, TargetFile=path.format(ws=ws)), cfg(ws, write=True))
        assert verdict["decision"] == "deny", (tool, path)


def test_a_git_lookalike_name_stays_writable(ws):
    assert (
        decide(_p("write_to_file", TargetFile=f"{ws}/.gitignore"), cfg(ws, write=True))["decision"]
        == "allow"
    )


@pytest.mark.parametrize(
    "path, decision",
    [
        ("{ws}/n.txt", "allow"),
        ("{ws}/../o", "deny"),
    ],
)
def test_multi_replace_file_content_confinement(ws, path, decision):
    assert (
        decide(
            _p("multi_replace_file_content", TargetFile=path.format(ws=ws)), cfg(ws, write=True)
        )["decision"]
        == decision
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


@pytest.mark.parametrize(
    "cwd, decision",
    [
        (None, "allow"),
        ("{ws}", "allow"),
        ("{ws}/../out.txt", "deny"),
    ],
)
def test_run_command_cwd_confinement(ws, cwd, decision):
    kwargs = {"CommandLine": "ls"}
    if cwd is not None:
        kwargs["Cwd"] = cwd.format(ws=ws)
    assert (
        decide(_p("run_command", **kwargs), cfg(ws, write=True, shell=True))["decision"] == decision
    )


@pytest.mark.parametrize("name", ["search_web", "schedule", "brand_new_tool"])
def test_everything_else_is_denied(ws, name):
    assert decide(_p(name), cfg(ws, write=True, shell=True))["decision"] == "deny"


@pytest.mark.parametrize(
    "name", ["finish", "send_message", "call_mcp_tool", "list_resources", "read_resource"]
)
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


# Round-1 review findings (CRITICAL/IMPORTANT/MINOR), fixed below.


def test_decide_denies_on_deeply_recursive_payload(ws):
    # Measured: 9999 levels of nested array as an argument value fits inside
    # Go's encoding/json 10000-level limit, so agy itself can forward a
    # payload that blows CPython's default recursion limit while parsing.
    # `json.loads` raises RecursionError here, not a JSON error -- `decide`
    # must catch that too, not just JSONDecodeError.
    nested = "[" * 9999 + "]" * 9999
    payload = f'{{"toolCall": {{"name": "view_file", "args": {{"AbsolutePath": {nested}}}}}}}'
    assert decide(payload, cfg(ws))["decision"] == "deny"


def test_main_denies_and_returns_0_on_non_utf8_stdin(ws, tmp_path, monkeypatch, capsys):
    import io

    from headless_agents.guards import agy_workspace

    home = tmp_path / "home"
    (home / ".gemini" / "config").mkdir(parents=True)
    (home / ".gemini" / "config" / "workspace-guard.json").write_text(json.dumps(cfg(ws)))
    monkeypatch.setenv("HOME", str(home))
    # 0xFF is not a valid UTF-8 start byte: reading this raises
    # UnicodeDecodeError from inside sys.stdin.read() itself, before `decide`
    # is ever reached.
    bad_stdin = io.TextIOWrapper(io.BytesIO(b"\xff\xfe\x00bad"), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", bad_stdin)
    assert agy_workspace.main() == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "deny"


@pytest.mark.parametrize(
    "config, tool_name, kwargs",
    [
        ({"write": "false", "shell": "false"}, "write_to_file", {"TargetFile": "{ws}/n.txt"}),
        ({"write": True, "shell": 1}, "run_command", {"CommandLine": "ls"}),
    ],
)
def test_flag_requires_the_literal_boolean_true(ws, config, tool_name, kwargs):
    # A truthy non-bool ("false" the string, or 1) must NOT arm write/shell:
    # only the JSON boolean `true` (Python `True`) does.
    full_config = {"root": str(ws), **config}
    formatted = {key: value.format(ws=ws) for key, value in kwargs.items()}
    assert decide(_p(tool_name, **formatted), full_config)["decision"] == "deny"


def test_case_variant_argument_key_is_denied(ws):
    # A key that differs from the one this guard reads only by case is either
    # an adversarial probe for a parser differential or a silent rename this
    # guard cannot tell apart from an attack: both must deny.
    payload = json.dumps(
        {
            "toolCall": {
                "name": "view_file",
                "args": {"AbsolutePath": f"{ws}/in.txt", "absolutePath": "ignored"},
            }
        }
    )
    assert decide(payload, cfg(ws))["decision"] == "deny"


def test_case_variant_cwd_key_is_denied(ws):
    payload = json.dumps(
        {
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "ls", "cwd": str(ws)},
            }
        }
    )
    assert decide(payload, cfg(ws, shell=True))["decision"] == "deny"


# Round-2 review findings (task-6-findings-r2.md), fixed below.


def test_case_variant_unicode_fold_is_denied(ws):
    # Go's encoding/json matches JSON object keys against struct fields via
    # bytes.EqualFold, which folds U+017F (LATIN SMALL LETTER LONG S, "ſ") to
    # "s" -- a plain str.lower() comparison does NOT fold it (measured), so a
    # key differing only by this Unicode fold used to slip the check.
    payload = json.dumps(
        {
            "toolCall": {
                "name": "view_file",
                "args": {"AbsolutePath": f"{ws}/in.txt", "AbſolutePath": "/etc/passwd"},
            }
        }
    )
    assert decide(payload, cfg(ws))["decision"] == "deny"


@pytest.mark.parametrize(
    "payload",
    [
        # top-level "toolCall" next to a case-variant "ToolCall"
        json.dumps({"toolCall": {"name": "finish", "args": {}}, "ToolCall": {}}),
        # "name" next to a case-variant "Name"
        json.dumps({"toolCall": {"name": "finish", "args": {}, "Name": "x"}}),
        # "args" next to a case-variant "aRgs"
        json.dumps({"toolCall": {"name": "finish", "args": {}, "aRgs": {}}}),
    ],
)
def test_envelope_case_variant_key_is_denied(ws, payload):
    # The same parser-differential class applies one level up: the envelope
    # keys `toolCall`, `name` and `args` are read exactly, just like a tool's
    # argument keys are.
    assert decide(payload, cfg(ws))["decision"] == "deny"


def test_main_returns_0_even_if_stdout_write_fails(ws, tmp_path, monkeypatch):
    import io

    from headless_agents.guards import agy_workspace

    home = tmp_path / "home"
    (home / ".gemini" / "config").mkdir(parents=True)
    (home / ".gemini" / "config" / "workspace-guard.json").write_text(json.dumps(cfg(ws)))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("sys.stdin", io.StringIO(_p("finish")))

    class _BrokenStdout:
        def write(self, data: str) -> int:
            raise BrokenPipeError("no reader")

        def flush(self) -> None:
            raise BrokenPipeError("no reader")

    monkeypatch.setattr("sys.stdout", _BrokenStdout())
    # main() must ALWAYS return 0, per its own contract, even when writing
    # the decision itself fails (e.g. the reader hung up early).
    assert agy_workspace.main() == 0
