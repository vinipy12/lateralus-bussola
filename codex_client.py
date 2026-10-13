"""One `codex exec` call with a JSON output schema, run read-only in an empty temporary directory under an isolated CODEX_HOME."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
TIMEOUT = 120


class CodexError(RuntimeError):
    """A Codex call that produced no usable JSON object."""


def real_auth_file() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json"


@contextlib.contextmanager
def isolated_codex_env():
    """Env for a Codex subprocess whose CODEX_HOME holds only a login and an empty config.

    The user's own ~/.codex defines MCP servers, connectors, memories and an AGENTS.md; none of that must be
    reachable from a prompt that carries untrusted job-ad text. auth.json is a symlink, not a copy, so token
    refreshes land in the real file (tradeoff: Codex could in principle write to it; it cannot reach anything else).
    """
    auth = real_auth_file()
    if not auth.is_file():
        raise CodexError(f"codex is not logged in: {auth} is missing")
    with tempfile.TemporaryDirectory(prefix="bussola-codex-home-") as home:
        (Path(home) / "auth.json").symlink_to(auth.resolve())
        (Path(home) / "config.toml").write_text("# bussola: intentionally empty; no MCP servers, apps or memories\n")
        yield {**os.environ, "CODEX_HOME": home}


def codex_available() -> str | None:
    """None when Codex can run; otherwise a one-line reason to skip the whole run."""
    if shutil.which(CODEX_BIN) is None:
        return f"{CODEX_BIN} binary not found on PATH"
    try:
        with isolated_codex_env() as env:
            result = subprocess.run([CODEX_BIN, "login", "status"], capture_output=True, text=True, timeout=30, env=env)
    except CodexError:
        return "codex is not logged in (run `codex login`)"
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"`codex login status` failed: {exc}"
    status = (result.stdout + result.stderr).lower()
    if result.returncode != 0 or "not logged in" in status or "logged in" not in status:
        return "codex is not logged in (run `codex login`)"
    return None


def run_codex(prompt: str, schema_path: Path, timeout: int = TIMEOUT) -> dict:
    # Three layers: an empty working directory, the read-only sandbox (files only; MCP calls escape it), and an
    # isolated CODEX_HOME with no MCP servers or connectors. Together a hostile job ad can at most distort its own answer.
    with tempfile.TemporaryDirectory(prefix="bussola-codex-") as work, isolated_codex_env() as env:
        output = Path(work) / "out.json"
        command = [CODEX_BIN, "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
                   "--output-schema", str(Path(schema_path).resolve()), "-o", str(output), "-"]
        try:
            result = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=work, env=env)
        except subprocess.TimeoutExpired:
            raise CodexError(f"timed out after {timeout}s") from None
        except OSError as exc:
            raise CodexError(f"could not start codex: {exc}") from None
        if result.returncode != 0:
            raise CodexError(f"exit {result.returncode}: {result.stderr.strip()[-300:]}")
        try:
            data = json.loads(output.read_text())
        except (OSError, ValueError) as exc:
            raise CodexError(f"no JSON output: {exc}") from None
    if not isinstance(data, dict):
        raise CodexError("output is not a JSON object")
    return data
