import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_client
from codex_client import CodexError, codex_available, run_codex
from helpers import REPO, codex_subprocess

SCHEMA = REPO / "profiles" / "example" / "profile.json"  # any existing file; the fake never reads it


class FakeRealHome(unittest.TestCase):
    """Points CODEX_HOME at a temp dir holding a fake auth.json, so no test touches the real ~/.codex."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.real = Path(tmp.name)
        (self.real / "auth.json").write_text("fake-auth")
        (self.real / "config.toml").write_text("[mcp_servers.evil]\ncommand = 'x'\n")
        patcher = patch.dict(os.environ, {"CODEX_HOME": str(self.real)})
        patcher.start()
        self.addCleanup(patcher.stop)


class RunCodexTests(FakeRealHome):
    def test_flags_stdin_and_empty_working_directory(self):
        fake, calls = codex_subprocess(lambda prompt: {"echo": prompt})
        with patch("codex_client.subprocess.run", fake):
            self.assertEqual(run_codex("hello", SCHEMA), {"echo": "hello"})
        [call] = calls
        cmd = call["cmd"]
        self.assertEqual(cmd[1:6], ["exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only"])
        self.assertEqual(cmd[cmd.index("--output-schema") + 1], str(SCHEMA.resolve()))
        self.assertEqual(cmd[-1], "-")
        self.assertEqual(Path(cmd[cmd.index("-o") + 1]).parent, call["cwd"])
        self.assertEqual(call["cwd_listing"], [])
        self.assertEqual(call["timeout"], 120)
        self.assertFalse(call["cwd"].exists())

    def test_timeout_nonzero_exit_and_bad_output_raise(self):
        cases = [subprocess.TimeoutExpired("codex", 120), 1]
        for answer in cases:
            fake, _ = codex_subprocess(lambda prompt, a=answer: a)
            with patch("codex_client.subprocess.run", fake), self.assertRaises(CodexError):
                run_codex("x", SCHEMA)
        fake, _ = codex_subprocess(lambda prompt: ["not", "an", "object"])
        with patch("codex_client.subprocess.run", fake), self.assertRaises(CodexError):
            run_codex("x", SCHEMA)

    def test_missing_output_file_raises(self):
        def run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, "", "")
        with patch("codex_client.subprocess.run", run), self.assertRaises(CodexError):
            run_codex("x", SCHEMA)


class IsolationTests(FakeRealHome):
    def test_exec_runs_under_minimal_codex_home(self):
        seen = {}
        base, calls = codex_subprocess(lambda prompt: {"ok": 1})

        def run(cmd, **kwargs):
            home = Path(kwargs["env"]["CODEX_HOME"])
            seen["home"] = home
            seen["listing"] = sorted(p.name for p in home.iterdir())
            seen["config"] = (home / "config.toml").read_text()
            seen["link"] = (home / "auth.json").is_symlink() and (home / "auth.json").resolve()
            return base(cmd, **kwargs)

        with patch("codex_client.subprocess.run", run):
            run_codex("x", SCHEMA)
        self.assertEqual(seen["listing"], ["auth.json", "config.toml"])
        self.assertNotIn("mcp_servers", seen["config"])
        self.assertNotEqual(seen["home"], self.real)
        self.assertEqual(seen["link"], (self.real / "auth.json").resolve())
        self.assertFalse(seen["home"].exists())
        self.assertEqual(calls[0]["cwd_listing"], [])
        self.assertNotEqual(seen["home"], calls[0]["cwd"])
        self.assertNotIn(str(seen["home"]), str(calls[0]["cwd"]))

    def test_missing_auth_raises_and_reports_not_logged_in(self):
        (self.real / "auth.json").unlink()
        with self.assertRaises(CodexError):
            run_codex("x", SCHEMA)
        with patch("codex_client.shutil.which", return_value="/bin/codex"):
            self.assertIn("not logged in", codex_available())

    def test_login_status_uses_isolated_home(self):
        envs = []

        def run(cmd, **kwargs):
            envs.append((kwargs["env"]["CODEX_HOME"], sorted(p.name for p in Path(kwargs["env"]["CODEX_HOME"]).iterdir())))
            return subprocess.CompletedProcess(cmd, 0, "Logged in using ChatGPT\n", "")

        with patch("codex_client.shutil.which", return_value="/bin/codex"), patch("codex_client.subprocess.run", run):
            self.assertIsNone(codex_available())
        [(home, listing)] = envs
        self.assertNotEqual(home, str(self.real))
        self.assertEqual(listing, ["auth.json", "config.toml"])


class AvailabilityTests(FakeRealHome):
    def status(self, code, out):
        return lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, code, out, "")

    def test_missing_binary(self):
        with patch("codex_client.shutil.which", return_value=None):
            self.assertIn("not found", codex_available())

    def test_logged_in_and_logged_out(self):
        with patch("codex_client.shutil.which", return_value="/bin/codex"):
            with patch("codex_client.subprocess.run", self.status(0, "Logged in using ChatGPT\n")):
                self.assertIsNone(codex_available())
            with patch("codex_client.subprocess.run", self.status(0, "Not logged in\n")):
                self.assertIn("not logged in", codex_available())
            with patch("codex_client.subprocess.run", self.status(1, "")):
                self.assertIn("not logged in", codex_available())


if __name__ == "__main__":
    unittest.main()
