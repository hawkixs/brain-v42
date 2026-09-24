"""roles.toml (spec 0.5.0 §3.1): every refusal names the file, the entry and the rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from headless_agents.roles import Link, RolesError, implicit_role, load_roles, resolve_role


def _load(tmp_path: Path, text: str, profiles: dict[str, object] | None = None):
    path = tmp_path / "roles.toml"
    path.write_text(text)
    return load_roles(path, mcp_profiles=profiles or {})


def test_a_provider_role_takes_every_default(tmp_path: Path) -> None:
    role = _load(tmp_path, '[rev]\nprovider = "codex"\n')["rev"]
    assert (role.effort, role.timeout, role.context, role.write, role.shell) == (
        "medium",
        300.0,
        "global",
        False,
        False,
    )
    assert role.links == (Link("codex", ""),)
    assert (role.model, role.mcp, role.instructions, role.implicit) == ("", None, None, False)


def test_a_write_role_defaults_to_the_full_context(tmp_path: Path) -> None:
    role = _load(tmp_path, '[impl]\nprovider = "codex"\nwrite = true\n')["impl"]
    assert role.context == "full"


def test_an_explicit_context_wins_over_the_write_default(tmp_path: Path) -> None:
    text = '[impl]\nprovider = "codex"\nwrite = true\ncontext = "none"\n'
    assert _load(tmp_path, text)["impl"].context == "none"


def test_a_chain_role_keeps_its_links_in_order(tmp_path: Path) -> None:
    role = _load(tmp_path, '[impl]\nchain = ["opencode:m1", "codex"]\n')["impl"]
    assert role.links == (Link("opencode", "m1"), Link("codex", ""))


def test_a_chain_model_is_everything_after_the_first_colon(tmp_path: Path) -> None:
    role = _load(tmp_path, '[r]\nchain = ["openrouter:meta/llama:free"]\n')["r"]
    assert role.links == (Link("openrouter", "meta/llama:free"),)


def test_every_declared_field_is_kept(tmp_path: Path) -> None:
    text = (
        '[r]\nprovider = "claude"\nmodel = "opus"\neffort = "high"\ntimeout = 90\n'
        'context = "full"\ncontext_parents = true\nmcp = "b"\nwrite = true\nshell = true\n'
        'instructions = "be terse"\n'
    )
    role = _load(tmp_path, text, profiles={"b": object()})["r"]
    assert (role.model, role.effort, role.timeout, role.context, role.context_parents) == (
        "opus",
        "high",
        90.0,
        "full",
        True,
    )
    assert (role.mcp, role.write, role.shell, role.instructions) == ("b", True, True, "be terse")


def test_an_openai_compat_role_keeps_its_endpoint(tmp_path: Path) -> None:
    text = '[r]\nprovider = "openai-compat"\nbase_url = "http://x/v1"\nkey_env = "K"\n'
    role = _load(tmp_path, text)["r"]
    assert (role.base_url, role.key_env) == ("http://x/v1", "K")


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ('[a]\nprovider = "codex"\nbogus = 1\n', "unknown field 'bogus'"),
        ("[a]\nprovider = 3\n", "provider must be a string"),
        ('[a]\neffort = "high"\n', "exactly one of provider and chain"),
        ('[a]\nprovider = "codex"\nchain = ["claude"]\n', "exactly one of provider and chain"),
        ('[a]\nprovider = "nope"\n', "unknown provider 'nope'"),
        ('[a]\nchain = ["codex", "codex:x"]\n', "codex appears twice in chain"),
        ('[a]\nchain = ["codex"]\nmodel = "m"\n', "model is refused on a chain role"),
        ('[a]\nprovider = "codex"\nshell = true\n', "shell requires write"),
        ('[a]\nprovider = "openrouter"\nwrite = true\n', "write needs a CLI rail"),
        ('[a]\nchain = ["codex", "openrouter"]\nmcp = "b"\n', "mcp needs a CLI rail"),
        ('[a]\nprovider = "codex"\nmcp = "missing"\n', "mcp profile 'missing' is not in mcp.toml"),
        ('[a]\nprovider = "openai-compat"\n', "openai-compat needs base_url and key_env"),
        (
            '[a]\nprovider = "codex"\nbase_url = "http://x"\n',
            "base_url and key_env apply to openai-compat only",
        ),
        ('["Bad_Name"]\nprovider = "codex"\n', "invalid role name"),
        ('[codex]\nprovider = "claude"\n', "collides with a provider name"),
        # wrong types and invalid values, one per field (spec §4: every refusal)
        ('[a]\nchain = "codex"\n', "chain must be a list of strings"),
        ('[a]\nchain = ["codex", 3]\n', "chain must be a list of strings"),
        ("[a]\nchain = []\n", "chain must name at least one provider"),
        ('[a]\nprovider = "codex"\nmodel = 3\n', "model must be a non-empty string"),
        ('[a]\nprovider = "codex"\nmodel = ""\n', "model must be a non-empty string"),
        ('[a]\nprovider = "codex"\neffort = 1\n', "effort must be a string"),
        ('[a]\nprovider = "codex"\neffort = "loud"\n', "effort 'loud' is not a codex effort"),
        ('[a]\nprovider = "codex"\ntimeout = true\n', "timeout must be a positive number"),
        ('[a]\nprovider = "codex"\ntimeout = "60"\n', "timeout must be a positive number"),
        ('[a]\nprovider = "codex"\ntimeout = 0\n', "timeout must be a positive number"),
        ('[a]\nprovider = "codex"\ncontext = "all"\n', "context must be one of full, global, none"),
        (
            '[a]\nprovider = "codex"\ncontext_parents = "yes"\n',
            "context_parents must be a boolean",
        ),
        ('[a]\nprovider = "codex"\nmcp = 1\n', "mcp must be a profile name"),
        ('[a]\nprovider = "codex"\nwrite = 1\n', "write must be a boolean"),
        ('[a]\nprovider = "codex"\nwrite = true\nshell = "no"\n', "shell must be a boolean"),
        (
            '[a]\nprovider = "openai-compat"\nbase_url = 1\nkey_env = "K"\n',
            "base_url must be a string",
        ),
        (
            '[a]\nprovider = "openai-compat"\nbase_url = "http://x"\nkey_env = 2\n',
            "key_env must be a string",
        ),
        ('[a]\nprovider = "codex"\ninstructions = 5\n', "instructions must be a string"),
        ("a = 1\n", "a role must be a table"),
    ],
)
def test_every_refusal_names_file_entry_and_rule(tmp_path: Path, text: str, rule: str) -> None:
    with pytest.raises(RolesError) as caught:
        _load(tmp_path, text, profiles={"b": object()})
    message = str(caught.value)
    assert str(tmp_path / "roles.toml") in message
    assert rule in message


def test_the_refusal_names_the_entry(tmp_path: Path) -> None:
    with pytest.raises(RolesError, match=r"\[reviewer-x\] shell requires write"):
        _load(tmp_path, '[reviewer-x]\nprovider = "codex"\nshell = true\n')


def test_every_provider_is_an_implicit_role() -> None:
    role = implicit_role("codex")
    assert role.links == (Link("codex", ""),)
    assert role.instructions is None and role.implicit and role.name == "codex"


def test_resolve_prefers_a_declared_role_then_a_provider(tmp_path: Path) -> None:
    declared = _load(tmp_path, '[rev]\nprovider = "claude"\n')
    assert resolve_role("rev", declared).name == "rev"
    assert resolve_role("codex", declared).implicit
    with pytest.raises(RolesError, match="unknown target 'zzz'"):
        resolve_role("zzz", declared)


def test_an_absent_file_declares_nothing() -> None:
    assert load_roles(None, mcp_profiles={}) == {}


def test_invalid_toml_is_a_roles_error(tmp_path: Path) -> None:
    with pytest.raises(RolesError, match="roles.toml"):
        _load(tmp_path, "[a\n")


def test_a_deeply_nested_file_is_a_roles_error(tmp_path: Path) -> None:
    with pytest.raises(RolesError, match="nested too deeply"):
        _load(tmp_path, "a = " + "[" * 2000 + "]" * 2000 + "\n")
