"""Test the per-session state companion of the admin elevation hook, stdlib only."""

import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "brain_session_state.py"
TOOL = "mcp__brain-v42__brain_session_"
BEARER = "synthetic-state-bearer"


class StateHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run = runpy.run_path(str(HOOK))["run"]
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.state_dir = self.root / "sessions"
        self.token_file = self.root / "credential"
        self.token_file.write_text(BEARER + "\n")
        self.token_file.chmod(0o600)
        self.env = {
            "BRAIN_SESSION_STATE_DIR": str(self.state_dir),
            "BRAIN_ADMIN_CREDENTIAL_FILE": str(self.token_file),
            "BRAIN_ADMIN_URL": "http://localhost:8765/admin/elevations",
        }
        self.requests: list[Any] = []

    def opener(self, request: Any, *, timeout: float) -> Any:
        self.requests.append(request)
        return io.BytesIO(json.dumps({"expires_at": "2026-10-06T13:00:00+00:00"}).encode())

    def prompt(self, claude_id: str, text: str = "hello") -> str:
        return self.run(
            {"hook_event_name": "UserPromptSubmit", "session_id": claude_id, "prompt": text},
            environ=self.env,
            opener=self.opener,
        )

    def tool(self, claude_id: str, name: str, response: Any) -> str:
        return self.run(
            {
                "hook_event_name": "PostToolUse",
                "session_id": claude_id,
                "tool_name": TOOL + name,
                "tool_response": response,
            },
            environ=self.env,
            opener=self.opener,
        )

    def state(self, claude_id: str) -> dict[str, Any]:
        return json.loads((self.state_dir / f"{claude_id}.json").read_text())

    @staticmethod
    def shapes(result: dict[str, Any]) -> list[Any]:
        text = json.dumps(result)
        return [result, text, [{"type": "text", "text": text}]]

    def test_first_prompt_is_refused_then_second_elevates_with_own_session_state(self) -> None:
        claude_a, claude_b = str(uuid4()), str(uuid4())
        brain_a, brain_b = str(uuid4()), str(uuid4())
        self.assertNotIn("Brain admin until", self.prompt(claude_a, "/brain-admin 1h x"))
        self.assertEqual(self.requests, [])
        self.assertEqual(self.state(claude_a)["prompt_count"], 1)

        self.tool(claude_a, "start", {"session": {"id": brain_a}})
        self.prompt(claude_b)
        self.tool(claude_b, "resume", {"session": {"id": brain_b}})
        output = self.prompt(claude_b, "/brain-admin 30m maintenance")

        self.assertEqual(output, "Brain admin until 2026-10-06T13:00:00+00:00.\n")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(json.loads(self.requests[0].data)["session_id"], brain_b)
        self.assertEqual(self.state(claude_a)["brain_session_id"], brain_a)
        self.assertEqual(self.state(claude_b)["prompt_count"], 2)
        self.assertNotIn(BEARER, output)

    def test_every_prompt_counts_including_ones_the_elevation_ignores(self) -> None:
        claude_id = str(uuid4())
        for text in ("a", "b\n/brain-admin 1h x", "c"):
            self.prompt(claude_id, text)
        self.assertEqual(self.state(claude_id)["prompt_count"], 3)
        self.assertEqual(self.state(claude_id)["claude_session_id"], claude_id)

    def test_start_resume_relay_record_the_returned_session_for_each_shape(self) -> None:
        claude_id = str(uuid4())
        self.prompt(claude_id)
        for name in ("start", "resume"):
            for shape in self.shapes({"session": {"id": (brain := str(uuid4()))}, "replayed": 0}):
                self.tool(claude_id, name, shape)
                self.assertEqual(self.state(claude_id)["brain_session_id"], brain)
        ended = str(uuid4())
        for shape in self.shapes(
            {"ended_session_id": ended, "session": {"id": (brain := str(uuid4()))}}
        ):
            self.tool(claude_id, "relay", shape)
            self.assertEqual(self.state(claude_id)["brain_session_id"], brain)
            self.assertNotEqual(brain, ended)
        self.assertEqual(self.state(claude_id)["prompt_count"], 1)

    def test_end_and_abandon_clear_only_the_matching_session(self) -> None:
        claude_id, brain = str(uuid4()), str(uuid4())
        for name in ("end", "abandon"):
            self.tool(claude_id, "start", {"session": {"id": brain}})
            self.tool(claude_id, name, {"session": {"id": str(uuid4())}})
            self.assertEqual(self.state(claude_id)["brain_session_id"], brain)
            self.tool(claude_id, name, {"session": {"id": brain}})
            self.assertNotIn("brain_session_id", self.state(claude_id))

    def test_failed_or_unparseable_tool_results_change_nothing(self) -> None:
        claude_id, brain = str(uuid4()), str(uuid4())
        self.tool(claude_id, "start", {"session": {"id": brain}})
        for response in (
            {"isError": True, "session": {"id": str(uuid4())}},
            "session_not_found",
            [{"type": "text", "text": "not json"}],
            {"session": {"id": "not-a-uuid"}},
            {"session": {"id": 7}},
            {"session": "x"},
            {"other": 1},
            None,
            12,
        ):
            self.tool(claude_id, "resume", response)
        self.assertEqual(self.state(claude_id)["brain_session_id"], brain)

    def test_oversized_response_is_ignored(self) -> None:
        claude_id = str(uuid4())
        padding = "x" * (1024 * 1024)
        self.tool(claude_id, "start", json.dumps({"session": {"id": str(uuid4())}, "p": padding}))
        self.assertFalse(self.state_dir.exists() and any(self.state_dir.iterdir()))

    def test_unsafe_state_directory_is_never_written(self) -> None:
        claude_id = str(uuid4())
        target = self.root / "elsewhere"
        target.mkdir(mode=0o700)
        self.state_dir.symlink_to(target)
        self.assertEqual(self.prompt(claude_id, "/brain-admin 1h x"), "")
        self.tool(claude_id, "start", {"session": {"id": str(uuid4())}})
        self.assertEqual(list(target.iterdir()), [])
        self.state_dir.unlink()
        self.state_dir.mkdir(mode=0o700)
        self.state_dir.chmod(0o750)
        self.prompt(claude_id)
        self.assertEqual(list(self.state_dir.iterdir()), [])
        self.assertEqual(self.requests, [])

    def test_missing_state_directory_is_created_private(self) -> None:
        self.prompt(str(uuid4()))
        self.assertEqual(self.state_dir.stat().st_mode & 0o777, 0o700)
        (state_file,) = self.state_dir.iterdir()
        self.assertEqual(state_file.stat().st_mode & 0o777, 0o600)

    def test_non_uuid_claude_session_ids_are_ignored(self) -> None:
        for session_id in (
            "../escape",
            "",
            None,
            5,
            str(uuid4()).upper(),
            "{" + str(uuid4()) + "}",
        ):
            self.prompt(session_id)  # type: ignore[arg-type]
            self.tool(session_id, "start", {"session": {"id": str(uuid4())}})  # type: ignore[arg-type]
        self.assertFalse(self.state_dir.exists() and any(self.state_dir.iterdir()))
        self.assertFalse((self.root / "escape.json").exists())

    def test_unknown_events_and_tools_do_nothing(self) -> None:
        claude_id = str(uuid4())
        session = {"session": {"id": str(uuid4())}}
        for payload in (
            {"hook_event_name": "Stop", "session_id": claude_id, "prompt": "x"},
            {"hook_event_name": "PostToolUse", "session_id": claude_id, "tool_name": "Bash"},
            {
                "hook_event_name": "PostToolUse",
                "session_id": claude_id,
                "tool_name": TOOL + "capture",
                "tool_response": session,
            },
            {"session_id": claude_id, "prompt": "x"},
            {"hook_event_name": "UserPromptSubmit", "session_id": claude_id},
        ):
            self.assertEqual(self.run(payload, environ=self.env, opener=self.opener), "")
        self.assertFalse(self.state_dir.exists() and any(self.state_dir.iterdir()))

    def test_command_line_entry_point_is_silent_on_garbage_and_works_on_a_prompt(self) -> None:
        env = {**os.environ, "BRAIN_SESSION_STATE_DIR": str(self.state_dir)}
        env.pop("BRAIN_ADMIN_URL", None)
        claude_id = str(uuid4())
        for stdin in ("not json", "[]", ""):
            done = subprocess.run(
                [sys.executable, "-I", str(HOOK)],
                input=stdin,
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        payload = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": claude_id,
            "prompt": "secret",
        }
        done = subprocess.run(
            [sys.executable, "-I", str(HOOK)],
            input=json.dumps(payload),
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual((done.returncode, done.stdout, done.stderr), (0, "", ""))
        self.assertEqual(self.state(claude_id)["prompt_count"], 1)
        self.assertNotIn("secret", (self.state_dir / f"{claude_id}.json").read_text())


if __name__ == "__main__":
    unittest.main()
