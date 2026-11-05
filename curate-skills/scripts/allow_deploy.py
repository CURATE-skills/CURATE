# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""allow_deploy: add the two allow rules deploy needs in later turns (deploy.py, monitor.py).

Run it yourself (in Claude Code: `! uv run --quiet <plugin>/scripts/allow_deploy.py`); Claude never
runs it. It merges into `permissions.allow` of ~/.claude/settings.json (or, with --project, this
project's .claude/settings.local.json): it only adds, never removes or reorders anything, backs the
file up first and prints the diff. Running it again changes nothing.

`--check` only reads: it reports which rules are missing (JSON), as doctor --deploy does.
Standard library only.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(os.path.abspath(__file__)).parent
sys.path.insert(0, str(SCRIPT_DIR))
import doctor  # noqa: E402  (standard library only)

TAKES_EFFECT = ("Claude Code reloads settings files when they change, so this applies to the running "
                "session; no restart needed.")


def plugin_root(given: str | None = None) -> str:
    """The plugin folder as Claude Code names it (`${CLAUDE_PLUGIN_ROOT}`): this script's folder's
    parent, symlinks kept, unless given."""
    return (given or str(SCRIPT_DIR.parent)).rstrip("/")


def user_settings() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "settings.json"


def project_settings(cwd: Path) -> Path:
    """.claude/settings.local.json where Claude Code keeps it: the git repository root, except
    outside a repository or when that root is the home directory (then the folder itself)."""
    try:
        top = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True,
                             text=True, timeout=10)
        root = Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        root = None
    if root is None or root.resolve() == Path.home().resolve():
        root = cwd
    return root / ".claude" / "settings.local.json"


def missing_rules(allow: list, root: str) -> list[str]:
    roots = list(dict.fromkeys([root, str(Path(root).resolve())]))
    return [doctor.auto_mode_rule(root, s) for s in doctor.AUTO_MODE_SCRIPTS
            if not any(isinstance(x, str) and doctor._bash_rule_covers(x, roots, s) for x in allow)]


def check(root: str, claude_dir: Path, project: Path) -> dict:
    """Read-only: the same check as doctor --deploy (user, project and local settings)."""
    allow: list = []
    files = [f for _, f in doctor._settings_files(claude_dir, project)] + [project_settings(project)]
    for f in dict.fromkeys(files):
        try:
            allow += (json.loads(f.read_text()).get("permissions") or {}).get("allow") or []
        except (OSError, ValueError, AttributeError):
            pass
    missing = missing_rules(allow, root)
    return {"ok": not missing, "missing": missing,
            "command": f"! uv run --quiet {root}/scripts/allow_deploy.py",
            "takes_effect": TAKES_EFFECT}


def _indent_of(text: str):
    """The file's own indentation (so the diff shows only the added lines); 2 for a new file."""
    for line in text.splitlines()[1:]:
        if line[:1] == "\t":
            return "\t"
        if line[:1] == " ":
            return len(line) - len(line.lstrip(" "))
    return 2


def add_rules(path: Path, root: str, now: datetime | None = None) -> dict:
    existed = path.is_file()
    before = path.read_text() if existed else ""
    try:
        data = json.loads(before) if before.strip() else {}
    except ValueError as e:
        return {"ok": False, "changed": False, "path": str(path),
                "error": f"{path} is not valid JSON ({e}); nothing was changed. Fix it, then run this again."}
    if not isinstance(data, dict):
        return {"ok": False, "changed": False, "path": str(path), "error": f"{path} is not a JSON object; nothing changed"}
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict) or not isinstance(perms.setdefault("allow", []), list):
        return {"ok": False, "changed": False, "path": str(path),
                "error": f"{path}: permissions.allow is not a list; nothing was changed"}
    add = missing_rules(perms["allow"], root)
    if not add:
        return {"ok": True, "changed": False, "path": str(path), "added": [],
                "message": f"Both rules are already in {path}; nothing changed."}
    perms["allow"] += add
    after = json.dumps(data, indent=_indent_of(before), ensure_ascii=False)
    after += "\n" if before.endswith("\n") or not before else ""  # as the file had it
    backup = None
    if existed:
        backup = path.with_name(path.name + ".bak-" + (now or datetime.now()).strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(after)
    if existed:
        shutil.copymode(path, tmp)
    tmp.replace(path)
    diff = "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        str(backup or "(new file)"), str(path)))
    return {"ok": True, "changed": True, "path": str(path), "added": add, "backup": str(backup) if backup else None,
            "diff": diff, "message": f"Added {len(add)} rule(s) to {path}. {TAKES_EFFECT}"}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="allow_deploy.py", description=__doc__.split("\n")[0])
    p.add_argument("--project", action="store_true",
                   help="write this project's .claude/settings.local.json instead of ~/.claude/settings.json")
    p.add_argument("--check", action="store_true", help="only report which rules are missing (JSON)")
    p.add_argument("--plugin-root", help="the plugin folder (default: this script's)")
    args = p.parse_args(argv)
    root = plugin_root(args.plugin_root)
    cwd = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if args.check:
        print(json.dumps(check(root, user_settings().parent, cwd), indent=2))
        return 0
    out = add_rules(project_settings(cwd) if args.project else user_settings(), root)
    if not out["ok"]:
        print(out["error"])
        return 1
    if out["changed"]:
        print(out["diff"].rstrip("\n"))
        if out["backup"]:
            print(f"\nBackup: {out['backup']}")
    print(out["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
