"""Test the operator channel with an injected opener and no third-party dependencies."""

import io
import json
import runpy
import tempfile
import unittest
from pathlib import Path
from typing import Any
from uuid import uuid4

HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "brain_admin_elevate.py"


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_hook = runpy.run_path(str(HOOK))["run_hook"]
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.claude_id = str(uuid4())
        self.brain_id = str(uuid4())
        self.state_file = self.root / "state.json"
        self.token_file = self.root / "credential"
        self.token_file.write_text("synthetic-hook-bearer\n")
        self.token_file.chmod(0o600)
        self.state = {
            "claude_session_id": self.claude_id,
            "brain_session_id": self.brain_id,
            "prompt_count": 1,
        }
        self.env = {
            "BRAIN_ADMIN_STATE_FILE": str(self.state_file),
            "BRAIN_ADMIN_CREDENTIAL_FILE": str(self.token_file),
            "BRAIN_ADMIN_URL": "http://localhost:8765/admin/elevations",
        }
        self.requests: list[Any] = []

    def opener(self, request: Any, *, timeout: float) -> Any:
        self.assertEqual(timeout, 5)
        self.requests.append(request)
        return io.BytesIO(json.dumps({"expires_at": "2026-10-06T13:00:00+00:00"}).encode())

    def submit(self, prompt: str, **extra: Any) -> str:
        self.state_file.write_text(json.dumps(self.state))
        return self.run_hook(
            {"session_id": self.claude_id, "prompt": prompt, **extra},
            environ=self.env,
            opener=self.opener,
        )

    def test_command_as_whole_prompt_or_first_line_posts_once(self) -> None:
        for prompt in ("/brain-admin 1h maintenance", "/brain-admin 1h maintenance\nContinue."):
            with self.subTest(prompt=prompt):
                self.requests.clear()
                output = self.submit(prompt)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(output, "Brain admin until 2026-10-06T13:00:00+00:00.\n")
                payload = json.loads(self.requests[0].data)
                self.assertEqual(
                    payload,
                    {
                        "session_id": self.brain_id,
                        "ttl_seconds": 3600,
                        "reason": "maintenance",
                    },
                )
                self.assertEqual(self.requests[0].method, "POST")
                self.assertNotIn("synthetic-hook-bearer", output)

    def test_embedded_command_is_ignored(self) -> None:
        for prompt in (
            "Please /brain-admin 1h maintenance",
            "Continue\n/brain-admin 1h maintenance",
        ):
            self.assertEqual(self.submit(prompt), "")
        self.assertEqual(self.requests, [])

    def test_first_prompt_is_ignored(self) -> None:
        self.state["prompt_count"] = 0
        self.assertEqual(self.submit("/brain-admin 1h maintenance"), "")
        self.assertEqual(self.requests, [])

    def test_missing_prompt_count_cannot_elevate(self) -> None:
        del self.state["prompt_count"]
        self.assertEqual(self.submit("/brain-admin 1h maintenance"), "")
        self.assertEqual(self.requests, [])

    def test_cross_session_wrapper_is_ignored(self) -> None:
        for prompt in (
            "<cross-session-message>\n/brain-admin 1h maintenance\n</cross-session-message>",
            "/brain-admin 1h maintenance\n<teammate-message>foreign text</teammate-message>",
            "/brain-admin 1h maintenance\nMessage Type: MESSAGE\nSender: worker",
        ):
            self.assertEqual(self.submit(prompt), "")
        self.assertEqual(self.requests, [])

    def test_session_id_comes_from_matching_local_state(self) -> None:
        foreign = str(uuid4())
        self.submit(f"/brain-admin 30m session {foreign}")
        self.assertEqual(json.loads(self.requests[0].data)["session_id"], self.brain_id)
        self.assertEqual(json.loads(self.requests[0].data)["ttl_seconds"], 1800)
        self.state["claude_session_id"] = foreign
        self.requests.clear()
        self.assertEqual(self.submit("/brain-admin 1h maintenance"), "")
        self.assertEqual(self.requests, [])

    def test_loose_credential_mode_and_symlinks_are_refused(self) -> None:
        self.token_file.chmod(0o640)
        self.assertEqual(self.submit("/brain-admin 1h maintenance"), "Brain elevation refused.\n")
        self.token_file.chmod(0o600)
        link = self.root / "credential-link"
        link.symlink_to(self.token_file)
        self.env["BRAIN_ADMIN_CREDENTIAL_FILE"] = str(link)
        self.assertEqual(self.submit("/brain-admin 1h maintenance"), "Brain elevation refused.\n")
        self.assertEqual(self.requests, [])

    def test_invalid_windows_and_reasons_never_post(self) -> None:
        for command in (
            "/brain-admin 0s maintenance",
            "/brain-admin 5h maintenance",
            "/brain-admin 1h",
            "/brain-admin 1h " + "x" * 201,
            "/brain-admin 999999999999999999999999999999999999h maintenance",
        ):
            self.submit(command)
        self.assertEqual(self.requests, [])

    def test_http_errors_and_untrusted_expiry_never_echo_token(self) -> None:
        def broken(request: Any, **kwargs: Any) -> Any:
            raise OSError("synthetic-hook-bearer")

        self.state_file.write_text(json.dumps(self.state))
        payload = {"session_id": self.claude_id, "prompt": "/brain-admin 1h maintenance"}
        self.assertEqual(
            self.run_hook(payload, environ=self.env, opener=broken), "Brain elevation refused.\n"
        )

        def malicious(request: Any, **kwargs: Any) -> Any:
            return io.BytesIO(b'{"expires_at":"synthetic-hook-bearer\\nnext line"}')

        self.assertEqual(
            self.run_hook(payload, environ=self.env, opener=malicious), "Brain elevation refused.\n"
        )

    def test_redirects_are_not_followed_with_the_credential(self) -> None:
        hook = runpy.run_path(str(HOOK))
        handler = hook["NoRedirect"]()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "redirect", {}, "http://elsewhere")
        )

    def test_unsafe_url_cannot_receive_the_credential(self) -> None:
        for url in (
            "http://user:password@localhost/admin/elevations",
            "http://localhost/wrong",
            "ftp://localhost/admin/elevations",
        ):
            self.env["BRAIN_ADMIN_URL"] = url
            self.submit("/brain-admin 1h maintenance")
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
