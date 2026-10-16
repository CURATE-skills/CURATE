#!/usr/bin/env python3
"""PreToolUse write-scope guard for curate-mga subagents (curate-skills).

Only tool calls made by the curate-mga subagent are checked; everything else
passes through untouched. For curate-mga, writes must land inside the one
folder named in <project>/.curate/mga.lock.json ("allowed_dir"), which /curate
writes right before spawning the subagent and removes afterwards.

  Write / Edit / MultiEdit / NotebookEdit   target path must resolve inside allowed_dir
  Bash                                      write-like commands (redirections, cp, mv, rm,
                                            mkdir, touch, tee, sed -i, git, pip install, ...)
                                            must target paths inside allowed_dir

The Bash check is a heuristic; /curate's snapshot diff (scratch.py verify-writes)
is the backstop. Any error while checking a curate-mga call denies it.
Standard library only; runs on Python 3.9+.
"""
import json
import os
import re
import shlex
import sys
from datetime import datetime

AGENT_NAME = "curate-mga"
FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}
# Commands whose every non-option argument is a path they create, change or delete.
ALL_ARGS_WRITE = {"rm", "rmdir", "mkdir", "touch", "chmod", "chown", "chgrp", "truncate",
                  "tee", "unlink", "shred"}
# Commands whose last non-option argument is the destination.
DEST_WRITE = {"cp", "mv", "ln", "install", "rsync", "scp"}
SAFE_SINKS = {"/dev/null", "/dev/stdout", "/dev/stderr"}
GIT_READ_ONLY = {"status", "diff", "log", "show", "blame", "grep", "ls-files", "rev-parse"}
SHELL_SEPARATORS = {";", "&&", "||", "|", "&", "\n", "(", ")"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}


class Deny(Exception):
    pass


def is_mga(data):
    agent = str(data.get("agent_type") or "")
    return agent == AGENT_NAME or agent.endswith(":" + AGENT_NAME)


def load_lock(project):
    path = os.path.join(project, ".curate", "mga.lock.json")
    try:
        with open(path) as f:
            lock = json.load(f)
    except (OSError, ValueError):
        raise Deny("no active curate-mga lock (.curate/mga.lock.json); "
                   "curate-mga may only run when /curate spawns it")
    allowed = lock.get("allowed_dir")
    if not allowed:
        raise Deny("the curate-mga lock has no allowed_dir")
    return os.path.realpath(allowed), lock


def resolve(path, cwd):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.realpath(os.path.normpath(path))


def inside(path, allowed):
    return path == allowed or path.startswith(allowed.rstrip(os.sep) + os.sep)


def check_path(raw, cwd, allowed, what):
    if raw in SAFE_SINKS:
        return
    if any(c in raw for c in "$`*?[{"):
        raise Deny("{} target {!r} uses variables, globs or substitution; "
                   "use an absolute path inside {}".format(what, raw, allowed))
    target = resolve(raw, cwd)
    if not inside(target, allowed):
        raise Deny("{} would write {} — curate-mga may only write inside {}".format(what, target, allowed))


def split_segments(command):
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    segment = []
    for tok in tokens:
        if tok in SHELL_SEPARATORS or set(tok) <= set(";&|()") and tok not in REDIRECTS:
            if segment:
                yield segment
            segment = []
        else:
            segment.append(tok)
    if segment:
        yield segment


def non_options(args):
    return [a for a in args if not a.startswith("-")]


def check_segment(tokens, cwd, allowed):
    """Return the cwd after this segment (to follow `cd`)."""
    # Redirections anywhere in the segment.
    args = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        m = re.match(r"^(\d*)(>>?|>\||&>>?)$", tok)
        if m:
            if i + 1 >= len(tokens):
                raise Deny("dangling redirection")
            target = tokens[i + 1]
            if not target.startswith("&"):  # 2>&1 style duplication writes nothing
                check_path(target, cwd, allowed, "redirection")
            i += 2
            continue
        if tok in ("<", "<<", "<<<"):
            i += 2
            continue
        args.append(tok)
        i += 1
    # Leading env assignments (FOO=bar cmd ...).
    while args and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", args[0]):
        args = args[1:]
    if not args:
        return cwd
    cmd, rest = os.path.basename(args[0]), args[1:]

    if cmd == "cd":
        dest = non_options(rest)
        if not dest:
            return os.path.expanduser("~")
        if any(c in dest[0] for c in "$`"):
            raise Deny("cd target {!r} uses variables or substitution; use an absolute path".format(dest[0]))
        return resolve(dest[0], cwd)
    if cmd in ("sudo", "doas"):
        raise Deny("curate-mga may not use {}".format(cmd))
    if cmd in ALL_ARGS_WRITE:
        for a in non_options(rest):
            check_path(a, cwd, allowed, cmd)
    elif cmd in DEST_WRITE:
        paths = non_options(rest)
        if "-t" in rest:
            idx = rest.index("-t")
            if idx + 1 < len(rest):
                check_path(rest[idx + 1], cwd, allowed, cmd)
        elif paths:
            check_path(paths[-1], cwd, allowed, cmd)
    elif cmd in ("sed", "perl") and any(a == "-i" or a.startswith("-i") or a.startswith("--in-place")
                                         for a in rest):
        files = non_options(rest)
        if cmd == "sed" and not any(a in ("-e", "-f") or a.startswith("--expression") for a in rest):
            files = files[1:]  # first non-option is the script
        for a in files:
            check_path(a, cwd, allowed, cmd + " -i")
    elif cmd == "dd":
        for a in rest:
            if a.startswith("of="):
                check_path(a[3:], cwd, allowed, "dd")
    elif cmd == "git":
        sub = non_options(rest)
        if sub and sub[0] not in GIT_READ_ONLY:
            raise Deny("curate-mga may not run `git {}`".format(sub[0]))
    elif cmd in ("pip", "pip3") or (cmd in ("python", "python3") and rest[:2] == ["-m", "pip"]) \
            or (cmd == "uv" and rest[:2] == ["pip", "install"]):
        if "install" in rest or "uninstall" in rest:
            raise Deny("curate-mga may not install packages; list them in <name>.deps.txt "
                       "(the stub test installs them in an isolated environment)")
    elif cmd in ("curl", "wget"):
        for flag in ("-o", "--output", "-O", "--output-document"):
            if flag in rest:
                idx = rest.index(flag)
                if cmd == "curl" and flag == "-O":
                    check_path(".", cwd, allowed, "curl -O")
                elif idx + 1 < len(rest):
                    check_path(rest[idx + 1], cwd, allowed, cmd)
        if cmd == "wget" and not any(f in rest for f in ("-O", "--output-document", "-q", "--spider")):
            check_path(".", cwd, allowed, "wget")
    return cwd


def check_bash(command, cwd, allowed):
    for segment in split_segments(command):
        cwd = check_segment(segment, cwd, allowed)


def decide(data, env):
    """Return None to allow (no opinion), or a deny reason."""
    if not is_mga(data):
        return None
    project = env.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    try:
        allowed, _ = load_lock(project)
        tool = data.get("tool_name")
        tool_input = data.get("tool_input") or {}
        cwd = data.get("cwd") or project
        if tool in FILE_TOOLS:
            raw = tool_input.get(FILE_TOOLS[tool])
            if not raw:
                raise Deny("{} call has no {}".format(tool, FILE_TOOLS[tool]))
            check_path(raw, cwd, allowed, tool)
        elif tool == "Bash":
            check_bash(str(tool_input.get("command") or ""), cwd, allowed)
        return None
    except Deny as e:
        return str(e)
    except Exception as e:  # fail closed for curate-mga
        return "write guard error ({}: {}); denied to be safe".format(type(e).__name__, e)


def log(project, entry):
    try:
        path = os.path.join(project, ".curate", "mga_guard.log")
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0  # not our business if the payload is unreadable and the agent is unknown
    reason = decide(data, os.environ)
    project = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    if os.environ.get("CURATE_GUARD_DEBUG") and data.get("agent_type"):
        # Smoke-test aid: record what Claude Code reports for subagent calls.
        try:
            with open(os.path.join(project, ".curate", "mga.lock.json")) as f:
                lock_node = json.load(f).get("node")
        except (OSError, ValueError):
            lock_node = None
        log(project, {"ts": datetime.now().isoformat(timespec="milliseconds"), "debug": True,
                      "agent_type": data.get("agent_type"), "agent_id": data.get("agent_id"),
                      "lock_node": lock_node, "tool": data.get("tool_name"),
                      "decision": "deny" if reason else "allow"})
    if reason is None:
        return 0
    log(project, {"ts": datetime.now().isoformat(timespec="seconds"), "agent_type": data.get("agent_type"),
                  "agent_id": data.get("agent_id"), "tool": data.get("tool_name"), "reason": reason})
    json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": reason}}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
