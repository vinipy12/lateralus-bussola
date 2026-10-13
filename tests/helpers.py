"""Shared fixtures: profiles built from the committed example, and a fake Codex subprocess."""

from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from profiles import Profile, load_profile

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "profiles" / "example" / "profile.json"


def example() -> Profile:
    return load_profile(REPO / "profiles" / "example")


def write_profile(profiles_dir: Path, name: str = "example", **overrides) -> Path:
    raw = {**json.loads(EXAMPLE.read_text()), "name": name, **overrides}
    root = Path(profiles_dir) / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "profile.json").write_text(json.dumps(raw))
    return root


def make_profile(profiles_dir: Path, name: str = "example", **overrides) -> Profile:
    return load_profile(write_profile(profiles_dir, name, **overrides))


def codex_subprocess(responder):
    """A stand-in for subprocess.run inside codex_client.

    responder(prompt) returns a dict (written to the -o file, exit 0), an int (that exit code, no output),
    or an exception instance (raised). Each call records cmd, input, cwd, timeout, env and the cwd's contents at call time.
    """
    calls = []
    lock = threading.Lock()

    def run(cmd, **kwargs):
        cwd = Path(kwargs["cwd"])
        with lock:
            calls.append({"cmd": cmd, "input": kwargs.get("input"), "cwd": cwd,
                          "timeout": kwargs.get("timeout"), "env": kwargs.get("env"), "cwd_listing": sorted(p.name for p in cwd.iterdir())})
        answer = responder(kwargs.get("input") or "")
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, int):
            return subprocess.CompletedProcess(cmd, answer, "", "simulated failure")
        Path(cmd[cmd.index("-o") + 1]).write_text(json.dumps(answer))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return run, calls
