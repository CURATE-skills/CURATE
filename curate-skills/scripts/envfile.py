"""The deploy settings file, ~/.config/curate/.env, read by deploy.py, monitor.py and doctor.py.

Copied from the plugin's .env.template and filled in by the user. Only the deploy names below are
read from it, and only when the environment doesn't already set them: environment variables
always win. The file is parsed as plain KEY=VALUE lines (optional `export `, optional matching
single or double quotes, `#` comments); nothing is evaluated by a shell. Values are never
printed: every report holds names and sources only.

Standard library only; runs on Python 3.9+ (doctor uses the system python3).
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

# Every variable the template documents. Case-insensitive groups: GitHub's spelling of the S3
# keys is S3_ACCESSKEY, and deploy accepts either.
DEPLOY_NAMES = ("FAASR_GH_USERNAME", "FAASR_ACTION_REPO", "FAASR_ACTION_BRANCH", "FAASR_S3_ENDPOINT",
                "FAASR_S3_BUCKET", "FAASR_S3_REGION", "GH_PAT", "S3_AccessKey", "S3_SecretKey",
                "CURATE_DEPLOY_NAME_SUFFIX", "CURATE_MONITOR_MINUTES")
_WANTED = {n.upper() for n in DEPLOY_NAMES}
LINE_RX = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def default_path() -> Path:
    return Path.home() / ".config" / "curate" / ".env"


def parse(text: str) -> tuple[dict, list]:
    """{KEY: value} and problems ("line N (KEY): …", never a value)."""
    vals, problems = {}, []
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = LINE_RX.match(line)
        if not m:
            problems.append(f"line {n}: not KEY=value")
            continue
        key, raw = m.group(1), m.group(2).strip()
        if raw[:1] in ("'", '"'):
            end = raw.find(raw[0], 1)
            if end < 0:
                problems.append(f"line {n} ({key}): unclosed quote")
                continue
            val, rest = raw[1:end], raw[end + 1:].strip()
        else:
            parts = raw.split(None, 1)
            val, rest = (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")
        if rest and not rest.startswith("#"):
            problems.append(f"line {n} ({key}): text after the value (no spaces in a value, or quote it)")
            continue
        vals[key] = val
    return vals, problems


def file_mode(path: Path) -> str | None:
    try:
        return oct(stat.S_IMODE(path.stat().st_mode))[2:]
    except OSError:
        return None


def load(environ=None, path: Path | None = None) -> dict:
    """Fill unset deploy variables in `environ` (default os.environ) from the file.

    A variable counts as set when any spelling of it (S3_AccessKey / S3_ACCESSKEY) is non-empty in
    the environment; then nothing of that name is taken from the file. Empty values in the file
    (the template's blanks) are skipped. Returns {path, exists, mode, sources: {NAME: "env"|"file"},
    problems}; names only."""
    environ = os.environ if environ is None else environ
    path = Path(path) if path else default_path()
    report = {"path": str(path), "exists": path.is_file(), "mode": None, "sources": {}, "problems": []}
    set_in_env = {k.upper() for k, v in environ.items() if k.upper() in _WANTED and str(v).strip()}
    for k in environ:
        if k.upper() in set_in_env and k.upper() in _WANTED:
            report["sources"][k] = "env"
    if not report["exists"]:
        return report
    report["mode"] = file_mode(path)
    try:
        vals, report["problems"] = parse(path.read_text())
    except (OSError, UnicodeDecodeError) as e:
        report["problems"] = [f"could not read it ({type(e).__name__})"]
        return report
    taken = set()
    for key, val in vals.items():
        up = key.upper()
        if up not in _WANTED or up in set_in_env or up in taken or not val:
            continue
        environ[key] = val
        report["sources"][key] = "file"
        taken.add(up)
    return report
