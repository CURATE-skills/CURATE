"""Commands on the Pegasus host: this machine, or an ssh host (no password prompt).

Shared by pegasus_deploy.py and pegasus_monitor.py. Standard library only. Commands run here get
an environment without the virtual environments `uv run` puts on PATH, so `python3` and
pegasus-plan (`#!/usr/bin/env python3`) are the system's Python, the one with Pegasus's own
modules, as in a terminal.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SSH_OPTS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")


def _is_venv_bin(p: str) -> bool:
    return (Path(p).parent / "pyvenv.cfg").is_file()


def uv_python_dir(env) -> Path:
    """Where uv keeps the Pythons it manages (`uv python dir`)."""
    if env.get("UV_PYTHON_INSTALL_DIR"):
        return Path(env["UV_PYTHON_INSTALL_DIR"])
    data = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else Path(env.get("HOME", "~")) / ".local" / "share"
    return data / "uv" / "python"


def host_env(environ=None) -> dict:
    """The environment without uv's Pythons: VIRTUAL_ENV and PYTHONHOME are dropped, and every
    PATH entry that is a virtual environment's bin folder (its parent holds pyvenv.cfg; `uv run
    --with` stacks several) or inside uv's managed-Python folder (`uv run --python X` puts that
    interpreter's bin first)."""
    env = dict(os.environ if environ is None else environ)
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONHOME", None)
    managed = str(uv_python_dir(env)).rstrip("/") + "/"
    env["PATH"] = os.pathsep.join(p for p in env.get("PATH", "").split(os.pathsep)
                                  if p and not _is_venv_bin(p) and not (p.rstrip("/") + "/").startswith(managed))
    return env


def run(cmd, timeout=600):
    """(returncode or None, stdout, stderr); never raises for a failed or missing command."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=host_env())
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout}s"
    except OSError as e:
        return None, "", f"{type(e).__name__}: {e}"


def on(host, command: str, runner=run, timeout=600):
    """A shell command here (host None), or on `host` over ssh."""
    if host is None:
        return runner(["sh", "-c", command], timeout=timeout)
    return runner(["ssh", *SSH_OPTS, host, command], timeout=timeout)


def tail(text, n=8) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return "\n".join(lines[-n:]) or "no output"
