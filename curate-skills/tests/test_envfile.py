"""~/.config/curate/.env: read by deploy, monitor and doctor; the environment wins; names only."""
import json
import os

import pytest

import doctor
import envfile

GH = "ghp_" + "F" * 36
AK, SK = "AKIA" + "FILEKEY000000000", "file-secret-value-0123456789abcdef"
FILE = f"""# comment
FAASR_GH_USERNAME=filealice
export FAASR_ACTION_REPO='file-repo'
FAASR_ACTION_BRANCH=
FAASR_S3_ENDPOINT="https://s3.us-east-1.amazonaws.com"
FAASR_S3_BUCKET=file-bucket   # trailing comment
FAASR_S3_REGION=us-east-1
GH_PAT={GH}
S3_AccessKey={AK}
S3_SecretKey={SK}
PATH=/evil/bin
SOMETHING_ELSE=x
"""


@pytest.fixture
def envf(tmp_path):
    f = tmp_path / ".env"
    f.write_text(FILE)
    f.chmod(0o600)
    return f


def test_parse_forms_and_problems_hold_no_values():
    vals, problems = envfile.parse("A=1\nexport B='two words'\nC=\"x\" # c\nD=val1x val2x\nE='val3x\nnot a line\n")
    assert vals == {"A": "1", "B": "two words", "C": "x"}
    assert len(problems) == 3 and not any(v in " ".join(problems) for v in ("val1x", "val2x", "val3x"))


def test_file_fills_unset_deploy_variables_only(envf):
    env = {"PATH": "/usr/bin"}
    info = envfile.load(env, envf)
    assert env["FAASR_GH_USERNAME"] == "filealice" and env["FAASR_ACTION_REPO"] == "file-repo"
    assert env["FAASR_S3_BUCKET"] == "file-bucket" and env["GH_PAT"] == GH and env["S3_AccessKey"] == AK
    assert env["PATH"] == "/usr/bin" and "SOMETHING_ELSE" not in env  # only the template's names
    assert "FAASR_ACTION_BRANCH" not in env  # empty in the file: left unset (deploy's default applies)
    assert info["sources"]["GH_PAT"] == "file" and info["mode"] == "600" and info["problems"] == []


def test_environment_wins_over_the_file(envf):
    env = {"FAASR_GH_USERNAME": "envbob", "GH_PAT": "env-token-value", "S3_ACCESSKEY": "ENVKEY", "FAASR_S3_BUCKET": ""}
    info = envfile.load(env, envf)
    assert env["FAASR_GH_USERNAME"] == "envbob" and env["GH_PAT"] == "env-token-value"
    assert "S3_AccessKey" not in env and env["S3_ACCESSKEY"] == "ENVKEY"  # any spelling set in the env wins
    assert env["FAASR_S3_BUCKET"] == "file-bucket"  # empty in the env counts as unset
    assert info["sources"]["FAASR_GH_USERNAME"] == "env" and info["sources"]["S3_ACCESSKEY"] == "env"
    assert info["sources"]["FAASR_S3_BUCKET"] == "file"


def test_report_never_holds_a_value(envf):
    info = envfile.load({}, envf)
    dumped = json.dumps(info)
    for v in (GH, AK, SK, "filealice", "file-bucket"):
        assert v not in dumped


def test_missing_file(tmp_path):
    env = {"GH_PAT": "x"}
    info = envfile.load(env, tmp_path / "nope.env")
    assert info["exists"] is False and env == {"GH_PAT": "x"} and info["sources"] == {"GH_PAT": "env"}


def test_default_path_is_under_home(monkeypatch, tmp_path):
    import importlib.util  # a fresh copy: conftest replaces default_path in the imported module
    spec = importlib.util.spec_from_file_location("envfile_fresh", envfile.__file__)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert fresh.default_path() == tmp_path / ".config" / "curate" / ".env"


@pytest.mark.parametrize("module", ["deploy", "monitor"])
def test_deploy_and_monitor_load_the_file(monkeypatch, envf, module):
    import importlib
    mod = importlib.import_module(module)
    fake_env = {k: v for k, v in os.environ.items()}
    monkeypatch.setattr(os, "environ", fake_env)
    monkeypatch.setattr(envfile, "default_path", lambda: envf)
    assert mod.main(["status", "--run", str(envf.parent / "no-such-run")]) == 2  # bad run; the file was read first
    assert fake_env["FAASR_S3_BUCKET"] == "file-bucket" and fake_env["GH_PAT"] == GH


# ── doctor ──

def _doctor(monkeypatch, envf, argv):
    for fn in ("check_hook_python", "check_uv_envs", "check_foreground", "check_loaded_once", "check_catalog",
               "check_gitignore", "check_repo_online", "check_s3_online"):
        monkeypatch.setattr(doctor, fn, lambda r, *a, **k: None)
    monkeypatch.setattr(doctor, "check_uv", lambda r: "/bin/uv")
    monkeypatch.setattr(envfile, "default_path", lambda: envf)
    monkeypatch.setattr(os, "environ", {**os.environ, "FAASR_GH_USERNAME": "envbob"})
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = doctor.main(argv)
    return code, buf.getvalue()


def test_doctor_reports_sources_names_only(monkeypatch, envf):
    code, out = _doctor(monkeypatch, envf, ["--deploy", "--json"])
    rows = {r["check"]: r for r in json.loads(out)["checks"]}
    assert code == 0 and rows["settings file"]["status"] == "ok" and "mode 600" in rows["settings file"]["message"]
    msg = rows["variables"]["message"]
    assert "FAASR_GH_USERNAME (environment)" in msg and "FAASR_S3_BUCKET (file)" in msg and "GH_PAT" not in msg
    assert "S3_AccessKey (file)" in msg and rows["GitHub token"]["message"] == "from GH_PAT (file) (value not shown)"
    for v in (GH, AK, SK, "filealice", "file-bucket", "envbob"):
        assert v not in out


def test_doctor_text_output_has_no_secret(monkeypatch, envf):
    code, out = _doctor(monkeypatch, envf, ["--deploy"])
    assert code == 0
    for v in (GH, AK, SK):
        assert v not in out


@pytest.mark.parametrize("argv", [[], ["--deploy"]])
def test_doctor_warns_when_the_file_is_not_600(monkeypatch, envf, argv):
    envf.chmod(0o644)
    code, out = _doctor(monkeypatch, envf, argv + ["--json"])
    (row,) = [r for r in json.loads(out)["checks"] if r["check"] == "settings file"]
    assert row["status"] == "warn" and "mode is 644, not 600" in row["message"]
    assert f"chmod 600 {envf}" in row["message"]


def test_doctor_without_deploy_does_not_load_values(monkeypatch, envf):
    code, out = _doctor(monkeypatch, envf, ["--json"])
    assert "GH_PAT" not in os.environ and [r["check"] for r in json.loads(out)["checks"]] == ["settings file"]


def test_doctor_reports_unreadable_lines_without_values(monkeypatch, envf):
    envf.write_text(FILE + "BAD=val9x more9x\n")
    code, out = _doctor(monkeypatch, envf, ["--deploy", "--json"])
    (row,) = [r for r in json.loads(out)["checks"] if r["check"] == "settings file"]
    assert row["status"] == "warn" and "(BAD)" in row["message"] and "val9x" not in out
