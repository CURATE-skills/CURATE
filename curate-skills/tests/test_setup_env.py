"""setup_env.py (curate-setup's helper): the deploy settings file. No network, no real home."""
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import setup_env as S

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = PLUGIN / "scripts" / "setup_env.py"
SYSTEM_PY = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else shutil.which("python3")
GOOD = {"FAASR_GH_USERNAME": "alice", "FAASR_ACTION_REPO": "faasr-actions", "FAASR_ACTION_BRANCH": "main",
        "FAASR_S3_ENDPOINT": "https://s3.us-west-2.amazonaws.com", "FAASR_S3_BUCKET": "my-bucket.data",
        "FAASR_S3_REGION": "us-west-2"}
SETS = [f"{k}={v}" for k, v in GOOD.items()]
FAKE_PAT = "ghp_" + "Z" * 36
FAKE_AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # AWS's documented example, no leak pattern


def run(*argv):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = S.main(list(argv))
    return code, json.loads(buf.getvalue())


def mode(p):
    return oct(stat.S_IMODE(p.stat().st_mode))[2:]


@pytest.fixture
def envfile(tmp_path):
    return tmp_path / "cfg" / "curate" / ".env"


def test_runs_on_system_python():
    proc = subprocess.run([SYSTEM_PY, str(SCRIPT), "keys"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert [a["key"] for a in out["ask"]] == ["FAASR_GH_USERNAME", "FAASR_ACTION_REPO", "FAASR_ACTION_BRANCH",
                                              "FAASR_S3_ENDPOINT", "FAASR_S3_BUCKET", "FAASR_S3_REGION"]
    assert all(a["hint"] for a in out["ask"])
    assert [s["key"] for s in out["secrets"]] == ["GH_PAT", "S3_AccessKey", "S3_SecretKey"]


# ── validate ──

@pytest.mark.parametrize("key,value", list(GOOD.items()) + [
    ("FAASR_S3_ENDPOINT", "http://minio.local:9000"), ("FAASR_ACTION_BRANCH", "release/1.x"),
    ("FAASR_GH_USERNAME", "my-org"), ("FAASR_S3_REGION", "auto")])
def test_validate_accepts(key, value):
    assert S.validate(key, f"  {value} ") == value


@pytest.mark.parametrize("key,value,text", [
    ("FAASR_ACTION_REPO", "alice/faasr-actions", "repo name only"),
    ("FAASR_S3_BUCKET", "s3://my-bucket", "bucket name only"),
    ("FAASR_S3_BUCKET", "My_Bucket", "doesn't look like a valid"),
    ("FAASR_S3_ENDPOINT", "minio-host-xyz:9000", "doesn't look like a valid"),
    ("FAASR_GH_USERNAME", "alice smith", "doesn't look like a valid"),
    ("FAASR_S3_REGION", "us-east-1'; rm -rf ~; '", "doesn't look like a valid"),
    ("GH_PAT", "anything", "is a secret"),
    ("S3_SecretKey", "anything", "is a secret"),
    ("NOT_A_KEY", "x", "unknown key"),
])
def test_validate_refuses(key, value, text):
    with pytest.raises(S.InputError) as e:
        S.validate(key, value)
    assert text in str(e.value)
    if key not in ("FAASR_ACTION_REPO",):
        assert value.strip() not in str(e.value) or value in ("x", "anything")  # values are not echoed


@pytest.mark.parametrize("value", [FAKE_PAT, "github_pat_" + "a1" * 20, "AKIA" + "ABCDEFGHIJKLMNOP", FAKE_AWS_SECRET])
@pytest.mark.parametrize("key", ["FAASR_GH_USERNAME", "FAASR_S3_BUCKET", "FAASR_S3_REGION"])
def test_pasted_secret_is_refused_and_never_echoed(key, value):
    code, out = run("validate", key, value)
    assert code == 2 and out["ok"] is False
    assert value not in json.dumps(out)
    if value != FAKE_AWS_SECRET:  # matches a leak pattern: told to revoke it
        assert "looks like a credential" in out["error"] and "revoke it" in out["error"]


# ── write: new ──

def test_new_file_is_600_with_empty_secret_lines(envfile):
    code, out = run("write", "--env-file", str(envfile), "--new", *[x for s in SETS for x in ("--set", s)])
    assert code == 0 and out["action"] == "created" and out["mode"] == "600"
    assert mode(envfile) == "600"
    text = envfile.read_text()
    vals, problems = S.parse(text)
    assert problems == [] and {k: vals[k] for k in GOOD} == GOOD
    assert {k: vals[k] for k in S.SECRET_KEYS} == {"GH_PAT": "", "S3_AccessKey": "", "S3_SecretKey": ""}
    lines = text.splitlines()
    for k in S.SECRET_KEYS:
        i = lines.index(f"{k}=")
        assert lines[i - 1].startswith("# paste here:")


def test_new_refuses_existing_file_missing_values_and_secrets(envfile):
    envfile.parent.mkdir(parents=True)
    envfile.write_text("FAASR_GH_USERNAME=bob\n")
    before = envfile.read_text()
    code, out = run("write", "--env-file", str(envfile), "--new", *[x for s in SETS for x in ("--set", s)])
    assert code == 2 and "already exists" in out["error"] and envfile.read_text() == before
    other = envfile.parent / "other.env"
    code, out = run("write", "--env-file", str(other), "--new", "--set", SETS[0])
    assert code == 2 and "values needed for" in out["error"] and not other.exists()
    code, out = run("write", "--env-file", str(other), "--new", *[x for s in SETS for x in ("--set", s)],
                    "--set", f"GH_PAT={FAKE_PAT}")
    assert code == 2 and "is a secret" in out["error"] and not other.exists()
    assert FAKE_PAT not in json.dumps(out)


# ── write: existing file ──

EXISTING = ("# mine\nexport FAASR_GH_USERNAME=bob\nFAASR_ACTION_REPO='acts'\nGH_PAT=" + FAKE_PAT
            + "  # my token\nFAASR_S3_BUCKET=b1\n")


def test_add_missing_appends_only_and_fixes_mode(envfile):
    envfile.parent.mkdir(parents=True)
    envfile.write_text(EXISTING)
    os.chmod(envfile, 0o644)
    sets = ["FAASR_ACTION_BRANCH=main", "FAASR_S3_ENDPOINT=https://s3.amazonaws.com", "FAASR_S3_REGION=us-east-1"]
    code, out = run("write", "--env-file", str(envfile), "--add-missing", *[x for s in sets for x in ("--set", s)])
    assert code == 0 and out["action"] == "added" and out["mode_fixed"] is True and mode(envfile) == "600"
    assert out["added"] == ["FAASR_ACTION_BRANCH", "FAASR_S3_ENDPOINT", "FAASR_S3_REGION", "S3_AccessKey",
                            "S3_SecretKey"]
    text = envfile.read_text()
    assert text.startswith(EXISTING)  # existing lines untouched, byte for byte
    vals, problems = S.parse(text)
    assert problems == [] and vals["GH_PAT"] == FAKE_PAT and vals["FAASR_GH_USERNAME"] == "bob"
    assert vals["S3_AccessKey"] == "" and vals["FAASR_S3_REGION"] == "us-east-1"
    assert FAKE_PAT not in json.dumps(out)
    code, out = run("write", "--env-file", str(envfile), "--add-missing")
    assert code == 0 and out["action"] == "nothing to add" and envfile.read_text() == text


def test_add_missing_never_changes_an_existing_key_and_needs_values(envfile):
    envfile.parent.mkdir(parents=True)
    envfile.write_text(EXISTING)
    code, out = run("write", "--env-file", str(envfile), "--add-missing", "--set", "FAASR_GH_USERNAME=carol")
    assert code == 2 and "already in the file" in out["error"] and envfile.read_text() == EXISTING
    code, out = run("write", "--env-file", str(envfile), "--add-missing")
    assert code == 2 and "values needed for the missing keys" in out["error"] and envfile.read_text() == EXISTING


def test_replace_needs_confirmed_and_keeps_a_600_backup(envfile):
    envfile.parent.mkdir(parents=True)
    envfile.write_text(EXISTING)
    args = ["write", "--env-file", str(envfile), "--replace", *[x for s in SETS for x in ("--set", s)]]
    code, out = run(*args)
    assert code == 2 and "--confirmed" in out["error"] and envfile.read_text() == EXISTING
    code, out = run(*args, "--confirmed")
    assert code == 0 and out["action"] == "replaced"
    backup = Path(out["backup"])
    assert backup.read_text() == EXISTING and mode(backup) == "600" and mode(envfile) == "600"
    assert S.parse(envfile.read_text())[0]["GH_PAT"] == ""
    assert FAKE_PAT not in json.dumps(out)


# ── status and parsing ──

def test_status_never_shows_a_secret(envfile):
    assert run("status", "--env-file", str(envfile))[1]["exists"] is False
    envfile.parent.mkdir(parents=True)
    envfile.write_text(EXISTING + "S3_SecretKey=\nS3_AccessKey=AKIAABCDEFGHIJKLMNOP\n")
    os.chmod(envfile, 0o644)
    code, out = run("status", "--env-file", str(envfile))
    dumped = json.dumps(out)
    assert FAKE_PAT not in dumped and "AKIAABCDEFGHIJKLMNOP" not in dumped
    assert out["secrets"] == {"GH_PAT": "set", "S3_AccessKey": "set", "S3_SecretKey": "empty"}
    assert out["values"] == {"FAASR_GH_USERNAME": "bob", "FAASR_ACTION_REPO": "acts", "FAASR_S3_BUCKET": "b1"}
    assert out["mode_ok"] is False and set(out["missing"]) == {"FAASR_ACTION_BRANCH", "FAASR_S3_ENDPOINT",
                                                               "FAASR_S3_REGION"}


def test_parse_problems_never_hold_values():
    vals, problems = S.parse("A=val1x val2x\nB='val3x\nC=ok # fine\nnot a line\nexport D=\"q v\"\n")
    assert vals == {"C": "ok", "D": "q v"}
    assert len(problems) == 3 and not any(x in " ".join(problems) for x in ("val1x", "val2x", "val3x"))


@pytest.mark.skipif(shutil.which("sh") is None, reason="no sh")
def test_parser_agrees_with_the_shell(tmp_path):
    f = tmp_path / ".env"
    f.write_text("# c\nexport A=1\nB='two words'\nC=\"x\"\nD=\nE=val # note\nGH_PAT=" + FAKE_PAT + "\n")
    proc = subprocess.run(["sh", "-c", f'set -a; . "{f}"; set +a; for k in A B C D E GH_PAT; do '
                                       'eval "printf \'%s=%s\\n\' $k \\"\\${$k}\\""; done'],
                          capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    shell = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    vals, problems = S.parse(f.read_text())
    assert problems == [] and vals == shell


# ── alias and the final doctor run ──

def test_alias_uses_a_subshell_and_home(monkeypatch, tmp_path):
    monkeypatch.setattr(S.Path, "home", classmethod(lambda cls: tmp_path))
    out = S.deploy_alias(str(PLUGIN), tmp_path / ".config" / "curate" / ".env")
    assert out["alias"] == (f"alias curate-deploy='(set -a; . \"$HOME/.config/curate/.env\"; set +a; "
                            f"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 exec claude --plugin-dir \"{PLUGIN}\")'")
    assert "subshell" in out["explain"]


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
def test_alias_leaves_the_users_shell_clean(tmp_path):
    home, bindir = tmp_path / "home", tmp_path / "bin"
    bindir.mkdir()
    envf = home / ".config" / "curate" / ".env"
    assert run("write", "--env-file", str(envf), "--new", *[x for s in SETS for x in ("--set", s)])[0] == 0
    envf.write_text(envf.read_text().replace("GH_PAT=\n", f"GH_PAT={FAKE_PAT}\n"))
    (bindir / "claude").write_text('#!/bin/sh\necho "inside: ${GH_PAT:+set} $FAASR_S3_BUCKET '
                                   '$CLAUDE_CODE_DISABLE_BACKGROUND_TASKS $*"\n')
    (bindir / "claude").chmod(0o755)
    rc = home / ".bashrc"
    rc.write_text(S.deploy_alias(str(PLUGIN), envf)["alias"].replace("$HOME", str(home)) + "\n")
    proc = subprocess.run(["bash", "--rcfile", str(rc), "-i", "-c",
                           'curate-deploy; echo "after: ${GH_PAT:-unset} ${FAASR_S3_BUCKET:-unset}"'],
                          capture_output=True, text=True, timeout=30,
                          env={"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home)})
    assert f"inside: set my-bucket.data 1 --plugin-dir {PLUGIN}" in proc.stdout
    assert "after: unset unset" in proc.stdout


def test_doctor_runs_with_the_files_values_and_redacts(monkeypatch, envfile):
    assert run("write", "--env-file", str(envfile), "--new", *[x for s in SETS for x in ("--set", s)])[0] == 0
    envfile.write_text(envfile.read_text().replace("GH_PAT=\n", f"GH_PAT={FAKE_PAT}\n")
                       .replace("S3_SecretKey=\n", f"S3_SecretKey={FAKE_AWS_SECRET}\n"))
    seen = {}

    class P:
        returncode = 1
        stdout = f"FAIL deploy x  echoed {FAKE_PAT} and {FAKE_AWS_SECRET}\n"

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw["env"])
        return P()
    monkeypatch.setattr(S.subprocess, "run", fake_run)
    code, out = run("doctor", "--env-file", str(envfile), "--plugin-root", str(PLUGIN))
    assert seen["cmd"][1].endswith("doctor.py") and seen["cmd"][2:5] == ["--deploy", "--online", "--plugin-root"]
    assert seen["env"]["FAASR_S3_BUCKET"] == "my-bucket.data" and seen["env"]["GH_PAT"] == FAKE_PAT
    assert "S3_AccessKey" not in seen["env"]  # empty values are not exported
    dumped = json.dumps(out)
    assert FAKE_PAT not in dumped and FAKE_AWS_SECRET not in dumped
    assert out["ok"] is False and out["empty_secrets"] == ["S3_AccessKey"]
    assert not any(FAKE_PAT in c for c in seen["cmd"])  # secrets never on the command line


# ── the skill ──

def test_skill_text():
    text = (PLUGIN / "skills" / "curate-setup" / "SKILL.md").read_text()
    front, body = text.split("---")[1], " ".join(text.split("---", 2)[2].split())
    assert "disable-model-invocation: true" in front
    assert "allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup_env.py *), AskUserQuestion" in front
    # prerequisites come before any question about values
    assert body.index("## 1. Prerequisites") < body.index("## 3. Ask for the non-secret settings")
    for item in ("Your own action repo", "**repo** and **workflow**", "An S3 bucket", "`GH_PAT`, `S3_AccessKey`, `S3_SecretKey`"):
        assert item in body
    assert "are **never** asked for, typed, shown or passed to a command here" in body
    assert "do **not** repeat it" in body and "revoke it and create a new one" in body
    assert "Never open, read, `cat` or `source` the `.env` file yourself" in body
    assert "One key per message" in body
    assert "Never replace the file without that answer" in body and "--replace --confirmed" in body
    assert "`setup.py doctor --plugin-root '${CLAUDE_PLUGIN_ROOT}'`" in body
    assert "no valid value has those" in body
