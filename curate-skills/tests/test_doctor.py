"""doctor.py: read-only readiness checks. No test touches the network, uv, or a real token."""
import io
import json
import os
import shutil
import subprocess
import sys
import types
from argparse import Namespace
from pathlib import Path

import pytest

import doctor

PLUGIN = Path(__file__).resolve().parents[1]
DOCTOR = PLUGIN / "scripts" / "doctor.py"
SYSTEM_PY = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else shutil.which("python3")
TOKEN = "ghp_" + "A" * 36  # shaped like a GitHub token, so leakcheck's pattern also catches it
ALL_DEPLOY = {"FAASR_GH_USERNAME": "alice", "FAASR_ACTION_REPO": "act", "FAASR_S3_ENDPOINT": "https://s3.x",
              "FAASR_S3_BUCKET": "bkt", "FAASR_S3_REGION": "us-west-2", "S3_AccessKey": "AK-secret-value",
              "S3_SecretKey": "SK-secret-value"}


def rows(r, group=None):
    return {x["check"]: x for x in r.rows if group in (None, x["group"])}


def one(r):
    (x,) = r.rows
    return x["status"], x["message"]


# ── runs on the hook's python, standard library only ──

def test_runs_on_system_python_with_stdlib_only(tmp_path):
    proc = subprocess.run([SYSTEM_PY, "-I", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import doctor",
                           str(PLUGIN / "scripts")], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    proc = subprocess.run([SYSTEM_PY, str(DOCTOR), "--help"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0 and "--deploy" in proc.stdout and "--online" in proc.stdout


# ── core ──

def test_uv_missing(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)
    r = doctor.Report()
    assert doctor.check_uv(r) is None
    status, msg = one(r)
    assert status == "fail" and "docs.astral.sh/uv" in msg


def test_hook_python_missing_or_old(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda n: None)
    r = doctor.Report()
    doctor.check_hook_python(r)
    assert one(r)[0] == "fail" and "BLOCKS EVERY Write, Edit and Bash call" in one(r)[1]
    monkeypatch.setattr(doctor.shutil, "which", lambda n: "/opt/py/python3")
    monkeypatch.setattr(doctor, "_run", lambda cmd, **k: (0, "3.8.10\n", ""))
    r = doctor.Report()
    doctor.check_hook_python(r)
    assert one(r)[0] == "fail" and "3.8.10" in one(r)[1]
    monkeypatch.setattr(doctor, "_run", lambda cmd, **k: (0, "3.9.6\n", ""))
    r = doctor.Report()
    doctor.check_hook_python(r)
    assert one(r) == ("ok", "Python 3.9.6 at /opt/py/python3")


@pytest.mark.parametrize("spec_ok,validator_out,status", [
    (True, '{"ok": false, "error": "empty"}', "ok"),  # the validator ran: FaaSr_py importable
    (True, '{"unavailable": true, "error": "ModuleNotFoundError"}', "fail"),
    (False, '{"ok": true}', "fail"),
    (True, "", "fail"),
])
def test_uv_envs(monkeypatch, spec_ok, validator_out, status):
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("input")))
        if cmd[-1] == "--help":
            return (0 if spec_ok else 1), "", "" if spec_ok else "error: network unreachable"
        return 0, validator_out, ""
    monkeypatch.setattr(doctor, "_run", fake_run)
    r = doctor.Report()
    doctor.check_uv_envs(r, "/bin/uv")
    assert one(r)[0] == status
    assert [c[0][1:3] for c in calls] == [["run", "--quiet"], ["run", "--quiet"]]
    assert calls[0][0][3].endswith("spec.py") and calls[1][0][3].endswith("faasr_validate.py")
    assert calls[1][1] == "{}"  # validator gets an empty workflow on stdin; nothing is written
    if status == "fail":
        assert "network access the first time" in one(r)[1]


def test_uv_envs_without_uv():
    r = doctor.Report()
    doctor.check_uv_envs(r, None)
    assert one(r)[0] == "fail"


@pytest.mark.parametrize("env,status", [
    ({"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"}, "ok"),
    ({"CLAUDECODE": "1"}, "fail"),
    ({"CLAUDECODE": "1", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0"}, "fail"),
    ({}, "warn"),  # a plain terminal: the session's value can't be known
])
def test_foreground(env, status):
    r = doctor.Report()
    doctor.check_foreground(r, env)
    assert one(r)[0] == status
    if status == "fail":
        assert "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 claude --plugin-dir" in one(r)[1]


@pytest.fixture
def claude_dir(tmp_path):
    d = tmp_path / "claude"
    (d / "plugins" / "cache" / "mkt" / "curate-skills" / "0.1.0").mkdir(parents=True)
    return d


def _install(claude_dir, entries, enabled=None):
    (claude_dir / "plugins" / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": entries}))
    if enabled is not None:
        (claude_dir / "settings.json").write_text(json.dumps({"enabledPlugins": enabled}))


def test_loaded_once(tmp_path, claude_dir):
    project = tmp_path / "proj"
    project.mkdir()
    checkout = str(tmp_path / "clone" / "curate-skills")
    cached = str(claude_dir / "plugins" / "cache" / "mkt" / "curate-skills" / "0.1.0")

    def status(root):
        r = doctor.Report()
        doctor.check_loaded_once(r, root, claude_dir, project)
        return one(r)

    assert status(checkout)[0] == "ok"  # nothing installed: --plugin-dir only
    _install(claude_dir, {"curate-skills@curate-local": [{"scope": "user", "installPath": cached}],
                          "other@x": [{"scope": "user"}]})
    st, msg = status(checkout)
    assert st == "fail" and "loaded twice" in msg and "curate-skills@curate-local (user scope)" in msg
    assert status(cached)[0] == "ok"  # the installed copy itself
    assert status("${CLAUDE_PLUGIN_ROOT}")[0] == "warn"  # unsubstituted: can't tell
    _install(claude_dir, {"curate-skills@curate-local": [{"scope": "user"}]},
             enabled={"curate-skills@curate-local": False})
    assert status(checkout)[0] == "ok"  # installed but disabled
    _install(claude_dir, {"curate-skills@curate-local": [{"scope": "project", "projectPath": str(tmp_path / "elsewhere")}]}, enabled={})
    assert status(checkout)[0] == "ok"  # installed for another project only
    _install(claude_dir, {"curate-skills@curate-local": [{"scope": "project", "projectPath": str(project)}]}, enabled={})
    assert status(checkout)[0] == "fail"


def test_catalog(tmp_path):
    def check(flag=None, env=None, config=None):
        r = doctor.Report()
        doctor.check_catalog(r, flag, env or {}, config)
        return one(r)

    cat = tmp_path / "cat"
    cat.mkdir()
    assert check(env={"CURATE_CATALOG_DIR": str(cat)}) == ("ok", f"{cat.resolve()} (from CURATE_CATALOG_DIR), writable")
    assert "will be created" in check(flag=str(tmp_path / "new" / "deeper"))[1]
    assert "(from --catalog-dir)" in check(flag=str(cat), env={"CURATE_CATALOG_DIR": "/elsewhere"})[1]
    assert "(from plugin setting)" in check(config=str(cat))[1]
    assert "(from default)" in check(config="${user_config.catalog_dir}")[1]
    f = tmp_path / "file"
    f.write_text("x")
    assert check(flag=str(f))[0] == "fail"
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        try:
            assert check(flag=str(locked))[0] == "fail"
            assert check(flag=str(locked / "sub"))[0] == "fail"
        finally:
            locked.chmod(0o700)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_gitignore(tmp_path):
    def check(p):
        r = doctor.Report()
        doctor.check_gitignore(r, p)
        return one(r)[0]

    plain = tmp_path / "plain"
    plain.mkdir()
    assert check(plain) == "ok"  # not a git repo
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert check(repo) == "warn"
    (repo / ".gitignore").write_text("node_modules/\n.curate/\n")
    assert check(repo) == "ok"
    assert sorted(p.name for p in repo.iterdir()) == [".git", ".gitignore"]  # doctor created nothing


# ── deploy ──

def test_deploy_variables():
    r = doctor.Report()
    assert doctor.check_deploy_vars(r, {}) is False
    st, msg = one(r)
    assert st == "fail" and "FAASR_S3_REGION" in msg and "S3_SecretKey (or S3_SECRETKEY)" in msg
    upper = {**ALL_DEPLOY}
    upper["S3_ACCESSKEY"] = upper.pop("S3_AccessKey")  # GitHub's spelling is accepted
    r = doctor.Report()
    assert doctor.check_deploy_vars(r, upper) is True
    st, msg = one(r)
    assert st == "ok" and "branch main (FAASR_ACTION_BRANCH unset)" in msg
    assert not any(v in msg for v in ALL_DEPLOY.values() if "secret" in v)
    r = doctor.Report()
    doctor.check_deploy_vars(r, {**ALL_DEPLOY, "GITHUB_REPOSITORY": "x/y"})
    assert [x["status"] for x in r.rows] == ["ok", "fail"] and "GITHUB_REPOSITORY" in r.rows[1]["message"]


def test_token_sources(monkeypatch):
    r = doctor.Report()
    assert doctor.check_token(r, {"GH_PAT": TOKEN}) == TOKEN
    assert one(r) == ("ok", "from GH_PAT (value not shown)")
    monkeypatch.setattr(doctor, "gh_cli_token", lambda: TOKEN)
    r = doctor.Report()
    assert doctor.check_token(r, {}) == TOKEN and "gh auth token" in one(r)[1]
    monkeypatch.setattr(doctor, "gh_cli_token", lambda: None)
    r = doctor.Report()
    assert doctor.check_token(r, {}) is None and one(r)[0] == "fail"


class FakeResp(io.BytesIO):
    def __init__(self, status, body, headers=None):
        super().__init__(json.dumps(body).encode())
        self.status, self.headers = status, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def gh_opener(repo_status=200, push=True, scopes="repo, workflow", branch_status=200):
    seen = []

    def opener(req, timeout=None):
        seen.append((req.get_method(), req.full_url, req.headers.get("Authorization")))
        status = branch_status if "/branches/" in req.full_url else repo_status
        if status != 200:
            raise doctor.urllib.error.HTTPError(req.full_url, status, "x", {}, None)
        return FakeResp(200, {"permissions": {"pull": True, "push": push}},
                        {"X-OAuth-Scopes": scopes} if scopes is not None else {})
    opener.seen = seen
    return opener


@pytest.mark.parametrize("kw,status,text", [
    ({}, "ok", "readable, push allowed"),
    ({"scopes": None}, "ok", "fine-grained token"),
    ({"repo_status": 404}, "fail", "not found, or the token can't see it"),
    ({"repo_status": 401}, "fail", "token was rejected"),
    ({"push": False}, "fail", "can read but not push"),
    ({"scopes": "repo"}, "fail", "lack ['workflow']"),
    ({"branch_status": 404}, "fail", "branch 'main' not found"),
])
def test_repo_online(kw, status, text):
    opener = gh_opener(**kw)
    r = doctor.Report()
    r.secrets["GH_PAT"] = TOKEN
    doctor.check_repo_online(r, ALL_DEPLOY, TOKEN, opener)
    st, msg = one(r)
    assert st == status and text in msg and TOKEN not in msg
    assert all(m == "GET" for m, _, _ in opener.seen)  # read-only
    assert opener.seen[0][1] == "https://api.github.com/repos/alice/act"


def test_repo_online_needs_inputs():
    r = doctor.Report()
    doctor.check_repo_online(r, {}, None)
    assert one(r)[0] == "fail" and "not checked" in one(r)[1]


@pytest.mark.parametrize("probe_out,status", [('{"ok": true}', "ok"),
                                              ('{"ok": false, "error": "head_bucket failed: ClientError 403"}', "fail"),
                                              ("", "fail")])
def test_s3_online_runs_the_probe_through_uv(monkeypatch, probe_out, status):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, env=kw.get("env"))
        return 0, probe_out, ""
    monkeypatch.setattr(doctor, "_run", fake_run)
    r = doctor.Report()
    doctor.check_s3_online(r, "/bin/uv", ALL_DEPLOY)
    assert one(r)[0] == status
    assert seen["cmd"][:2] == ["/bin/uv", "run"] and "FaaSr_py==0.1.13" in seen["cmd"] and seen["cmd"][-1] == "_s3probe"
    assert not any("secret-value" in c for c in seen["cmd"])  # keys go via the environment, never argv
    assert seen["env"]["S3_SecretKey"] == "SK-secret-value"


def test_s3_probe_uses_deploys_client_and_reports_codes_only(monkeypatch, capsys):
    made = []

    class Client:
        def __init__(self, fail):
            self.fail = fail

        def head_bucket(self, Bucket):
            made.append(("head_bucket", Bucket))
            if self.fail:
                err = Exception("AccessDenied for key AK-secret-value")
                err.response = {"Error": {"Code": "403"}}
                raise err

    for k, v in ALL_DEPLOY.items():
        monkeypatch.setenv(k, v)
    for fail, expect in ((False, {"ok": True}), (True, {"ok": False, "error": "head_bucket failed: Exception 403"})):
        boto3 = types.SimpleNamespace(client=lambda svc, **kw: (made.append((svc, kw["endpoint_url"])), Client(fail))[1])
        monkeypatch.setitem(sys.modules, "boto3", boto3)
        assert doctor.main(["_s3probe"]) == 0
        assert json.loads(capsys.readouterr().out) == expect
    assert made[:2] == [("s3", "https://s3.x"), ("head_bucket", "bkt")]


# ── whole run: exit codes, redaction ──

def _args(**kw):
    return Namespace(**{"deploy": False, "online": False, "plugin_root": None, "catalog_dir": None,
                        "config_catalog_dir": None, "json": False, **kw})


@pytest.fixture
def all_ok(monkeypatch, tmp_path):
    def ok(group, name):
        return lambda r, *a, **k: r.add(group, name, "ok", "fine")
    monkeypatch.setattr(doctor, "check_uv", lambda r: (r.add("core", "uv", "ok", "fine"), "/bin/uv")[1])
    for fn in ("check_hook_python", "check_uv_envs", "check_foreground", "check_loaded_once", "check_catalog",
               "check_gitignore"):
        monkeypatch.setattr(doctor, fn, ok("core", fn))
    monkeypatch.setattr(doctor, "check_s3_online", ok("deploy", "s3"))
    monkeypatch.setattr(doctor, "check_repo_online", ok("deploy", "repo"))
    return monkeypatch


def test_exit_codes(all_ok, capsys):
    assert doctor.main([]) == 0
    all_ok.setattr(doctor, "check_gitignore", lambda r, p: r.add("core", "gi", "warn", "add it"))
    assert doctor.main([]) == 0  # warn alone does not fail
    assert doctor.main(["--deploy"]) == 1  # no deploy variables here (conftest clears them)
    all_ok.setattr(doctor, "check_catalog", lambda r, *a: r.add("core", "cat", "fail", "no"))
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL  core" in out and "fix the FAIL lines above" in out


def test_online_implies_deploy_and_json(all_ok, capsys, monkeypatch):
    for k, v in ALL_DEPLOY.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GH_PAT", TOKEN)
    assert doctor.main(["--online", "--json"]) == 0
    out = capsys.readouterr().out
    res = json.loads(out)
    assert res["ok"] is True and [c["check"] for c in res["checks"] if c["group"] == "deploy"] == \
        ["variables", "GitHub token", "repo", "s3"]
    assert TOKEN not in out and "AK-secret-value" not in out and "SK-secret-value" not in out


def test_every_line_is_redacted():
    r = doctor.Report()
    r.secrets["gh auth token"] = "plain-token-value-123"
    r.add("deploy", "x", "fail", "server said plain-token-value-123 and " + TOKEN)
    assert "plain-token-value-123" not in r.rows[0]["message"] and TOKEN not in r.rows[0]["message"]
    assert "[REDACTED:gh auth token]" in r.rows[0]["message"]


# ── the skill ──

def test_skill_runs_doctor_with_python3_and_reads_nothing_secret():
    text = (PLUGIN / "skills" / "curate-doctor" / "SKILL.md").read_text()
    assert "allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py *)" in text
    assert ("`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py --plugin-root '${CLAUDE_PLUGIN_ROOT}' "
            "--config-catalog-dir '${user_config.catalog_dir}'`") in text
    flat = " ".join(text.split())
    assert "never open a `.env` file" in flat and "Never read, print, echo or ask for a credential" in flat
    assert "Don't try to fix anything yourself" in flat
