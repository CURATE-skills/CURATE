#!/usr/bin/env python3
"""curate-setup helper: the deploy settings file (default ~/.config/curate/.env).

  python3 setup_env.py keys                       (what curate-setup asks, with hints and defaults)
  python3 setup_env.py status   [--env-file F]
  python3 setup_env.py validate KEY VALUE
  python3 setup_env.py write    [--env-file F] --set KEY=VALUE ... (--new | --add-missing | --replace --confirmed)
  python3 setup_env.py alias    --plugin-root DIR [--env-file F]
  python3 setup_env.py doctor   --plugin-root DIR [--env-file F]

Rules (curate-setup relies on them):
- Only the six NON-secret keys can be given a value here. GH_PAT, S3_AccessKey and S3_SecretKey are
  always written as empty lines under a "# paste here" comment, for the user to fill in an editor.
  A value that looks like a credential is refused for every key, and nothing is echoed back.
- The file is created with mode 600 (O_EXCL: never over an existing file). An existing file is
  changed only with --add-missing (appends keys it lacks; existing lines untouched) or
  --replace --confirmed (the old file is kept as <file>.bak-<timestamp>, mode 600).
- `status` prints non-secret values and, for secrets, only "set" or "empty" — never a value.
- `doctor` runs doctor.py --deploy --online with the file's values added to its environment (the
  file is parsed here; nothing is sourced in a shell), so the user's own shell stays untouched.
Standard library only; runs on Python 3.9+. Prints one JSON object.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import leakcheck  # noqa: E402

DEFAULT_FILE = Path.home() / ".config" / "curate" / ".env"
FOREGROUND = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1"

# Non-secret keys, in the order curate-setup asks for them: (key, hint, validator, default).
NON_SECRET = (
    ("FAASR_GH_USERNAME", "your GitHub user or organisation name: the owner in github.com/<owner>/<repo>",
     r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", None),
    ("FAASR_ACTION_REPO", "just the repo name (not owner/repo) of your own action repo",
     r"[A-Za-z0-9._-]{1,100}", None),
    ("FAASR_ACTION_BRANCH", "the repo's default branch (GitHub: the branch shown on the repo page)",
     r"[A-Za-z0-9._/-]{1,100}", "main"),
    ("FAASR_S3_ENDPOINT", "AWS: https://s3.<region>.amazonaws.com (or https://s3.amazonaws.com); "
     "MinIO or another S3-compatible service: its https URL", r"https?://[A-Za-z0-9.:\-\[\]/]+",
     "https://s3.amazonaws.com"),
    ("FAASR_S3_BUCKET", "the bucket name only (AWS console: S3 → Buckets), no s3:// and no folder",
     r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", None),
    ("FAASR_S3_REGION", "the bucket's region, e.g. us-east-1 (AWS console: the bucket's 'AWS Region'); "
     "MinIO: often us-east-1", r"[a-z0-9-]{2,30}", "us-east-1"),
)
NON_SECRET_KEYS = tuple(k for k, *_ in NON_SECRET)
SECRETS = (
    ("GH_PAT", "GitHub token. Classic: scopes repo and workflow. Fine-grained: this action repo, "
               "Contents, Workflows and Actions read and write, Secrets read"),
    ("S3_AccessKey", "S3 access key ID for the bucket"),
    ("S3_SecretKey", "S3 secret access key for the bucket"),
)
SECRET_KEYS = tuple(k for k, _ in SECRETS)
ALL_KEYS = NON_SECRET_KEYS + SECRET_KEYS
LINE_RX = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


class InputError(Exception):
    pass


# ── reading (a strict subset of shell syntax, so `. file` and this parser agree) ──

def parse(text: str) -> tuple[dict, list[str]]:
    """KEY=value lines → ({KEY: value}, problems). Values: bare (no spaces), or in single/double
    quotes. Comments and blank lines are skipped. Problems never contain a value."""
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
            problems.append(f"line {n} ({key}): text after the value; remove spaces or wrap the value in "
                            "single quotes")
            continue
        vals[key] = val
    return vals, problems


def _mode(path: Path) -> str:
    return oct(stat.S_IMODE(path.stat().st_mode))[2:]


def status(path: Path) -> dict:
    out = {"ok": True, "file": str(path), "exists": path.exists()}
    if not path.exists():
        out.update(missing=list(ALL_KEYS), values={}, secrets={k: "missing" for k in SECRET_KEYS})
        return out
    vals, problems = parse(path.read_text())
    out.update(mode=_mode(path), mode_ok=_mode(path) == "600", problems=problems,
               values={k: vals[k] for k in NON_SECRET_KEYS if k in vals},  # non-secret values only
               empty=[k for k in NON_SECRET_KEYS if k in vals and not vals[k]],
               secrets={k: ("missing" if k not in vals else "set" if vals[k] else "empty") for k in SECRET_KEYS},
               missing=[k for k in ALL_KEYS if k not in vals])
    return out


# ── validating and writing ──

def validate(key: str, value: str) -> str:
    if key in SECRET_KEYS:
        raise InputError(f"{key} is a secret: it is never set here. Paste it into the file yourself.")
    spec = {k: (h, rx) for k, h, rx, _ in NON_SECRET}.get(key)
    if spec is None:
        raise InputError(f"unknown key {key!r}; allowed: {', '.join(NON_SECRET_KEYS)}")
    value = value.strip()
    if leakcheck.find_leaks(value):
        raise InputError(f"the value for {key} looks like a credential, so it was not used (not shown). If it "
                         "is one, revoke it and create a new one: it is now in this chat.")
    if key == "FAASR_ACTION_REPO" and "/" in value:
        raise InputError("FAASR_ACTION_REPO is the repo name only, without the owner (e.g. my-faasr-actions)")
    if key == "FAASR_S3_BUCKET" and value.startswith("s3://"):
        raise InputError("FAASR_S3_BUCKET is the bucket name only, without s3:// or a folder")
    if not re.fullmatch(spec[1], value):
        # The value is never echoed: a pasted secret need not match a leak pattern.
        raise InputError(f"that doesn't look like a valid {key} (value not shown): {spec[0]}")
    return value


def render(values: dict, keys) -> str:
    """Lines for `keys` (in canonical order): non-secrets with their values, secrets as empty
    lines under a '# paste here' comment."""
    out = []
    for key, hint, _, _ in NON_SECRET:
        if key in keys:
            out += [f"# {hint}", f"{key}={values[key]}"]
    sec = [(k, h) for k, h in SECRETS if k in keys]
    if sec:
        out.append("# Secrets: paste each value right after the = (no quotes, no spaces). Never paste them in a chat.")
        for key, hint in sec:
            out += [f"# paste here: {hint}", f"{key}="]
    return "\n".join(out) + "\n"


HEADER = ("# curate-skills deploy settings (written by /curate-skills:curate-setup).\n"
          "# Loaded only by the deploy alias, in a subshell: never `source` this in your login shell.\n")


def _create_600(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def write(path: Path, sets: list[str], mode: str, confirmed: bool) -> dict:
    values = {}
    for item in sets:
        key, sep, val = item.partition("=")
        if not sep:
            raise InputError(f"--set needs KEY=VALUE, got {key!r}")
        values[key] = validate(key, val)
    if mode == "new":
        if path.exists():
            raise InputError(f"{path} already exists; ask the user, then use --add-missing or --replace --confirmed")
        missing_ns = [k for k in NON_SECRET_KEYS if k not in values]
        if missing_ns:
            raise InputError(f"values needed for: {', '.join(missing_ns)}")
        _create_600(path, HEADER + "\n" + render(values, ALL_KEYS))
        return {"ok": True, "action": "created", "file": str(path), "mode": "600", "wrote": list(ALL_KEYS)}
    if not path.exists():
        raise InputError(f"{path} does not exist; use --new")
    if mode == "add-missing":
        have, problems = parse(path.read_text())
        if problems:
            raise InputError("the file has lines this helper can't read: " + "; ".join(problems))
        add = [k for k in ALL_KEYS if k not in have]
        stray = sorted(set(values) - set(add))
        if stray:
            raise InputError(f"{', '.join(stray)} already in the file; --add-missing never changes existing "
                             "lines (edit the file, or --replace)")
        lacking = [k for k in add if k in NON_SECRET_KEYS and k not in values]
        if lacking:
            raise InputError(f"values needed for the missing keys: {', '.join(lacking)}")
        if add:
            old = path.read_text()
            with open(path, "a") as f:
                f.write(("" if old.endswith("\n") or not old else "\n") + "\n# Added by curate-setup:\n"
                        + render(values, add))
        changed_mode = _mode(path) != "600"
        os.chmod(path, 0o600)
        return {"ok": True, "action": "added" if add else "nothing to add", "file": str(path), "added": add,
                "mode": "600", "mode_fixed": changed_mode}
    if mode == "replace":
        if not confirmed:
            raise InputError("--replace needs --confirmed (the user's yes to replacing the file)")
        missing_ns = [k for k in NON_SECRET_KEYS if k not in values]
        if missing_ns:
            raise InputError(f"values needed for: {', '.join(missing_ns)}")
        backup = path.with_name(path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        os.replace(path, backup)
        os.chmod(backup, 0o600)
        _create_600(path, HEADER + "\n" + render(values, ALL_KEYS))
        return {"ok": True, "action": "replaced", "file": str(path), "backup": str(backup), "mode": "600",
                "wrote": list(ALL_KEYS)}
    raise InputError(f"unknown mode {mode}")


# ── the deploy alias and the final check ──

def deploy_alias(plugin_root: str, path: Path) -> dict:
    root = Path(plugin_root).expanduser().resolve()
    env_ref = str(path).replace(str(Path.home()), "$HOME", 1) if str(path).startswith(str(Path.home())) else str(path)
    alias = (f"alias curate-deploy='(set -a; . \"{env_ref}\"; set +a; "
             f"{FOREGROUND} exec claude --plugin-dir \"{root}\")'")
    return {"ok": True, "alias": alias,
            "explain": "The parentheses run it in a subshell: the variables exist only for that Claude Code "
                       "session and are gone when it exits, so your own shell never holds them.",
            "where": "Add the line to ~/.zshrc (zsh, the macOS default) or ~/.bashrc, then open a new terminal."}


def run_doctor(plugin_root: str, path: Path) -> dict:
    if not path.exists():
        raise InputError(f"{path} does not exist; run the setup first")
    vals, problems = parse(path.read_text())
    env = dict(os.environ)
    env.update({k: v for k, v in vals.items() if v})
    proc = subprocess.run([sys.executable, str(SCRIPT_DIR / "doctor.py"), "--deploy", "--online",
                           "--plugin-root", plugin_root], capture_output=True, text=True, env=env, timeout=900)
    secrets = {k: v for k, v in vals.items() if k in SECRET_KEYS and v}
    out = leakcheck.redact(proc.stdout, extra_values=secrets)  # doctor redacts too; belt and braces
    return {"ok": proc.returncode == 0, "exit": proc.returncode, "file_problems": problems, "report": out,
            "empty_secrets": [k for k in SECRET_KEYS if not vals.get(k)]}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="curate-setup: the deploy settings file")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("status", "write", "alias", "doctor"):
        q = sub.add_parser(name)
        q.add_argument("--env-file", default=str(DEFAULT_FILE))
        if name in ("alias", "doctor"):
            q.add_argument("--plugin-root", required=True)
        if name == "write":
            q.add_argument("--set", action="append", default=[])
            g = q.add_mutually_exclusive_group(required=True)
            g.add_argument("--new", action="store_const", const="new", dest="mode")
            g.add_argument("--add-missing", action="store_const", const="add-missing", dest="mode")
            g.add_argument("--replace", action="store_const", const="replace", dest="mode")
            q.add_argument("--confirmed", action="store_true")
    v = sub.add_parser("validate")
    v.add_argument("key")
    v.add_argument("value")
    sub.add_parser("keys")
    args = p.parse_args(argv)
    try:
        if args.cmd == "keys":
            out = {"ok": True, "ask": [{"key": k, "hint": h, "default": d} for k, h, _, d in NON_SECRET],
                   "secrets": [{"key": k, "hint": h} for k, h in SECRETS]}
        elif args.cmd == "validate":
            out = {"ok": True, "key": args.key, "value": validate(args.key, args.value)}
        else:
            path = Path(args.env_file).expanduser()
            if args.cmd == "status":
                out = status(path)
            elif args.cmd == "write":
                out = write(path, args.set, args.mode, args.confirmed)
            elif args.cmd == "alias":
                out = deploy_alias(args.plugin_root, path)
            else:
                out = run_doctor(args.plugin_root, path)
    except InputError as e:
        out = {"ok": False, "error": str(e)}
        print(json.dumps(out, indent=2))
        return 2
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
