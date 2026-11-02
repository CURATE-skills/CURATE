import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "hooks" / "mga_guard.py"
HOOKS_JSON = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"
SYSTEM_PY = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else shutil.which("python3")


@pytest.fixture
def project(tmp_path):
    proj = tmp_path / "proj"
    allowed = proj / ".curate" / "runs" / "r1" / "functions" / "work"
    allowed.mkdir(parents=True)
    (proj / ".curate" / "runs" / "r1" / "functions" / "other").mkdir()
    (proj / ".curate" / "mga.lock.json").write_text(json.dumps(
        {"run_id": "r1", "run_dir": str(proj / ".curate" / "runs" / "r1"), "node": "work",
         "allowed_dir": str(allowed)}))
    return proj, allowed.resolve()


def guard(proj, tool, tool_input, agent="curate-skills:curate-mga", cwd=None, env=None):
    payload = {"session_id": "s", "hook_event_name": "PreToolUse", "cwd": str(cwd or proj),
               "tool_name": tool, "tool_input": tool_input}
    if agent is not None:
        payload["agent_type"] = agent
        payload["agent_id"] = "a1"
    proc = subprocess.run([SYSTEM_PY, str(GUARD)], input=json.dumps(payload), capture_output=True,
                          text=True, env={"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(proj),
                                          **(env or {})}, timeout=10)
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


def test_hooks_json_shape():
    cfg = json.loads(HOOKS_JSON.read_text())
    (entry,) = cfg["hooks"]["PreToolUse"]
    assert set(entry["matcher"].split("|")) == {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"}
    (hook,) = entry["hooks"]
    assert hook["onFailure"] == "block" and "${CLAUDE_PLUGIN_ROOT}" in hook["command"]


def test_runs_on_system_python():
    out = subprocess.run([SYSTEM_PY, "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                         capture_output=True, text=True).stdout.split()
    assert tuple(map(int, out)) >= (3, 9)  # the hook's interpreter is new enough


@pytest.mark.parametrize("agent", [None, "Explore", "general-purpose", "other-plugin:helper"])
def test_other_agents_pass_through(project, agent):
    proj, _ = project
    assert guard(proj, "Write", {"file_path": "/etc/passwd"}, agent=agent) is None
    assert guard(proj, "Bash", {"command": "rm -rf /"}, agent=agent) is None


@pytest.mark.parametrize("agent", ["curate-mga", "curate-skills:curate-mga"])
def test_both_agent_name_forms_are_guarded(project, agent):
    proj, _ = project
    assert guard(proj, "Write", {"file_path": str(proj / "x.py")}, agent=agent)


def test_file_tools(project):
    proj, allowed = project
    assert guard(proj, "Write", {"file_path": str(allowed / "work.py")}) is None
    assert guard(proj, "Edit", {"file_path": str(allowed / "test_data" / "in.csv")}) is None
    assert guard(proj, "NotebookEdit", {"notebook_path": str(allowed / "n.ipynb")}) is None
    for target in (allowed.parent / "other" / "other.py", allowed.parent.parent / "state.json",
                   allowed.parent.parent / "CONTEXT.md", proj / ".curate" / "mga.lock.json"):
        reason = guard(proj, "Write", {"file_path": str(target)})
        assert reason and str(allowed) in reason
    # traversal and relative paths are resolved
    assert guard(proj, "Write", {"file_path": str(allowed / ".." / "other" / "x.py")})
    assert guard(proj, "Write", {"file_path": "work.py"}, cwd=allowed) is None
    assert guard(proj, "Write", {"file_path": "work.py"}, cwd=proj)
    # a prefix-sharing sibling is not inside
    sibling = allowed.parent / "work2"
    sibling.mkdir()
    assert guard(proj, "Write", {"file_path": str(sibling / "a.py")})


def test_symlink_escape(project):
    proj, allowed = project
    (allowed / "link").symlink_to(proj)
    assert guard(proj, "Write", {"file_path": str(allowed / "link" / "evil.py")})


def test_missing_lock_denies_mga(project):
    proj, allowed = project
    (proj / ".curate" / "mga.lock.json").unlink()
    reason = guard(proj, "Write", {"file_path": str(allowed / "work.py")})
    assert "no active curate-mga lock" in reason
    assert guard(proj, "Write", {"file_path": str(allowed / "work.py")}, agent=None) is None


@pytest.mark.parametrize("cmd", [
    "echo hi > {a}/out.txt",
    "cd {a} && python3 work.py > log.txt 2>&1",
    "mkdir -p {a}/test_data && cp {p}/data.csv {a}/test_data/",
    "uv run \"/plugin/scripts/stubtest.py\" run --run {r} --node work",
    "cat {p}/CONTEXT.md | head -5",
    "python3 -c 'print(1)' > /dev/null",
    "ls {p}; git status",
    "sed -n 1,5p {p}/x.py",
    "rm {a}/test_output/old.csv",
])
def test_bash_allowed(project, cmd):
    proj, allowed = project
    run = allowed.parent.parent
    assert guard(proj, "Bash", {"command": cmd.format(a=allowed, p=proj, r=run)}) is None


@pytest.mark.parametrize("cmd", [
    "echo hi > {p}/out.txt",
    "echo hi >> ../other/other.py",
    "cp {a}/work.py {p}/stolen.py",
    "mv {a}/work.py {a}/../other/other.py",
    "rm -rf {p}/.curate",
    "touch {r}/state.json",
    "mkdir {p}/newdir",
    "tee {r}/CONTEXT.md < /dev/null",
    "sed -i 's/a/b/' {r}/functions/other/other.py",
    "git commit -am x",
    "pip install numpy",
    "python3 -m pip install numpy",
    "uv pip install numpy",
    "curl -o {p}/x.bin https://example.com",
    "cd {p} && echo x > y.txt",
    "echo x > $HOME/y.txt",
    "sudo ls",
])
def test_bash_denied(project, cmd):
    proj, allowed = project
    run = allowed.parent.parent
    assert guard(proj, "Bash", {"command": cmd.format(a=allowed, p=proj, r=run)}, cwd=allowed)


def test_relative_bash_uses_cwd(project):
    proj, allowed = project
    assert guard(proj, "Bash", {"command": "echo x > out.txt"}, cwd=allowed) is None
    assert guard(proj, "Bash", {"command": "echo x > out.txt"}, cwd=proj)


def test_unparseable_command_fails_closed(project):
    proj, _ = project
    reason = guard(proj, "Bash", {"command": "echo 'unterminated"})
    assert reason and "denied to be safe" in reason


def test_denials_are_logged_and_debug_log(project):
    proj, allowed = project
    guard(proj, "Write", {"file_path": str(proj / "x")})
    guard(proj, "Write", {"file_path": str(allowed / "ok.py")}, env={"CURATE_GUARD_DEBUG": "1"})
    lines = [json.loads(l) for l in (proj / ".curate" / "mga_guard.log").read_text().splitlines()]
    assert lines[0]["agent_type"] == "curate-skills:curate-mga" and "x" in lines[0]["reason"]
    assert lines[1] == {**lines[1], "debug": True, "decision": "allow", "tool": "Write"}


# ── deploying: never by curate-mga; credentials never printed by the main session ──

@pytest.mark.parametrize("cmd", [
    "uv run --quiet /p/scripts/deploy.py publish --run r1 --confirmed",
    "python3 scripts/monitor.py poll --run r1",
    "cd /p/scripts && uv run deploy.py preflight --run r1",
])
def test_mga_may_not_deploy(project, cmd):
    proj, _ = project
    assert "may not run deploy.py or monitor.py" in guard(proj, "Bash", {"command": cmd})


@pytest.mark.parametrize("cmd", [
    "gh auth token", "gh auth status --show-token", "printenv", "printenv GH_PAT", "env", "env | grep S3",
    "set", "export", "export -p", "declare -x", "echo $GH_PAT", "echo \"${S3_SecretKey}\"",
    "printf '%s' $S3_ACCESSKEY", "cat ~/.config/curate/.env", "head -3 .env", "grep S3 prod.env",
    "ls && cat .env", "cat .env.local", "less config/.env.production", "tail .envrc",
])
def test_main_session_may_not_print_credentials(project, cmd):
    proj, _ = project
    assert guard(proj, "Bash", {"command": cmd}, agent=None)


@pytest.mark.parametrize("cmd", [
    "uv run --quiet /p/scripts/deploy.py publish --run r1 --confirmed",
    "env -u CLAUDE_CODE_DISABLE_BACKGROUND_TASKS uv run pytest", "set -a", "export FOO=1",
    "echo $HOME", "cat README.md", "ls .env", "git status", "gh auth status", "cat env.md", "cat my.environment",
    "(set -a; . ~/.config/curate/.env; set +a; uv run deploy.py preflight --run r)",
])
def test_main_session_ordinary_commands_pass(project, cmd):
    proj, _ = project
    assert guard(proj, "Bash", {"command": cmd}, agent=None) is None
