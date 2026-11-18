"""doctor.py --pegasus: settings, IR tools, and the host checks (local or over ssh). No test runs
Pegasus, HTCondor, Docker or ssh: commands go through a fake doctor._run."""
from argparse import Namespace
from pathlib import Path

import pytest

import doctor
import envfile

PLUGIN = Path(__file__).resolve().parents[1]


def rows(r):
    return {x["check"]: (x["status"], x["message"]) for x in r.rows if x["group"] == "pegasus"}


class FakeHost:
    """Answers doctor._run: commands are matched by a substring of the shell command."""

    def __init__(self, answers=None, ssh_ok=True):
        self.calls, self.ssh_ok = [], ssh_ok
        self.answers = {"true": (0, "", ""), "pegasus-version": (0, "5.1.3\n", ""),
                        "import Pegasus.api": (0, "", ""), "condor_q": (0, "", ""),
                        "docker info": (0, "29.2.1\n", ""), "apptainer": (0, "apptainer version 1.3\n", "")}
        self.answers.update(answers or {})

    def __call__(self, cmd, timeout=60, **kw):
        self.calls.append(cmd)
        if cmd[0] == "ssh" and not self.ssh_ok:
            return 255, "", "ssh: connect to host maxwell port 22: Connection refused"
        command = cmd[-1]
        for key, ans in self.answers.items():
            if key == command or (key != "true" and key in command):
                return ans
        raise AssertionError(f"unexpected command {cmd}")


def test_settings_defaults_and_local():
    s = doctor.pegasus_settings({})
    assert s == {"host": None, "runtime": "docker", "image": "docker:///python:3.12-slim"}
    assert doctor.pegasus_settings({"CURATE_PEGASUS_HOST": "local"})["host"] is None
    assert doctor.pegasus_settings({"CURATE_PEGASUS_HOST": " maxwell "})["host"] == "maxwell"


@pytest.mark.parametrize("env,needle", [
    ({"CURATE_PEGASUS_HOST": "-oProxyCommand=evil"}, "not an ssh host name"),
    ({"CURATE_PEGASUS_HOST": "a host"}, "not an ssh host name"),
    ({"CURATE_PEGASUS_RUNTIME": "podman"}, "use docker or singularity"),
    ({"CURATE_PEGASUS_IMAGE": "python:3.12"}, "needs a scheme"),
])
def test_bad_settings_fail_and_skip_host_checks(monkeypatch, env, needle):
    fake = FakeHost()
    monkeypatch.setattr(doctor, "_run", fake)
    r = doctor.Report()
    s = doctor.pegasus_settings(env)
    assert doctor.check_pegasus_settings(r, s) is False
    status, msg = rows(r)["settings"]
    assert status == "fail" and needle in msg
    assert fake.calls == []


def test_ir_tools_present_in_this_plugin(tmp_path):
    r = doctor.Report()
    doctor.check_ir_tools(r)
    assert rows(r)["IR tools"][0] == "ok"
    r = doctor.Report()
    doctor.check_ir_tools(r, tmp_path)
    status, msg = rows(r)["IR tools"]
    assert status == "fail" and "curate_ir/backends/pegasus_backend.py" in msg


def test_local_host_all_ok(monkeypatch):
    fake = FakeHost()
    monkeypatch.setattr(doctor, "_run", fake)
    r = doctor.Report()
    doctor.check_pegasus_host(r, doctor.pegasus_settings({}))
    got = rows(r)
    assert list(got) == ["pegasus-version", "Pegasus Python API", "HTCondor", "container runtime"]
    assert all(s == "ok" for s, _ in got.values())
    assert got["pegasus-version"][1] == "Pegasus 5.1.3" and "Docker 29.2.1" in got["container runtime"][1]
    assert all(c[:2] == ["sh", "-c"] for c in fake.calls)  # nothing over ssh
    assert any("pegasus-config --python" in c[-1] for c in fake.calls)


def test_remote_host_runs_everything_over_batch_ssh(monkeypatch):
    fake = FakeHost()
    monkeypatch.setattr(doctor, "_run", fake)
    r = doctor.Report()
    doctor.check_pegasus_host(r, doctor.pegasus_settings({"CURATE_PEGASUS_HOST": "maxwell",
                                                          "CURATE_PEGASUS_RUNTIME": "singularity"}))
    got = rows(r)
    assert list(got) == ["ssh", "pegasus-version", "Pegasus Python API", "HTCondor", "container runtime"]
    assert all(s == "ok" for s, _ in got.values()) and "on maxwell" in got["HTCondor"][1]
    assert "apptainer version" in got["container runtime"][1]
    for c in fake.calls:
        assert c[:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "maxwell"]


def test_unreachable_host_stops_early(monkeypatch):
    fake = FakeHost(ssh_ok=False)
    monkeypatch.setattr(doctor, "_run", fake)
    r = doctor.Report()
    doctor.check_pegasus_host(r, doctor.pegasus_settings({"CURATE_PEGASUS_HOST": "maxwell"}))
    got = rows(r)
    assert got["ssh"][0] == "fail" and "Connection refused" in got["ssh"][1] and "ssh maxwell true" in got["ssh"][1]
    assert got["pegasus"][0] == "fail" and len(fake.calls) == 1


@pytest.mark.parametrize("key,answer,check", [
    ("pegasus-version", (127, "", "sh: pegasus-version: not found"), "pegasus-version"),
    ("import Pegasus.api", (1, "", "ModuleNotFoundError: No module named 'yaml'"), "Pegasus Python API"),
    ("condor_q", (1, "", "Failed to fetch ads from schedd"), "HTCondor"),
    ("docker info", (1, "", "permission denied while trying to connect to the Docker daemon"),
     "container runtime"),
])
def test_each_failure_is_reported(monkeypatch, key, answer, check):
    monkeypatch.setattr(doctor, "_run", FakeHost({key: answer}))
    r = doctor.Report()
    doctor.check_pegasus_host(r, doctor.pegasus_settings({}))
    status, msg = rows(r)[check]
    assert status == "fail" and answer[2].split(":")[-1].strip() in msg


def test_old_pegasus_is_a_warning(monkeypatch):
    monkeypatch.setattr(doctor, "_run", FakeHost({"pegasus-version": (0, "4.9.3\n", "")}))
    r = doctor.Report()
    doctor.check_pegasus_host(r, doctor.pegasus_settings({}))
    assert rows(r)["pegasus-version"][0] == "warn"


def _args(**kw):
    return Namespace(**{"deploy": False, "online": False, "pegasus": False, "plugin_root": None,
                        "catalog_dir": None, "config_catalog_dir": None, "json": False, **kw})


@pytest.fixture
def core_ok(monkeypatch):
    monkeypatch.setattr(doctor, "check_uv", lambda r: None)
    for fn in ("check_hook_python", "check_uv_envs", "check_foreground", "check_loaded_once", "check_catalog",
               "check_gitignore"):
        monkeypatch.setattr(doctor, fn, lambda r, *a, **k: None)
    return monkeypatch


def test_run_checks_only_with_flag_and_reads_settings_file(core_ok, tmp_path):
    fake = FakeHost()
    core_ok.setattr(doctor, "_run", fake)
    r = doctor.run_checks(_args(), env={}, claude_dir=tmp_path)
    assert rows(r) == {} and fake.calls == []
    f = tmp_path / ".env"
    f.write_text("CURATE_PEGASUS_HOST=maxwell\nCURATE_PEGASUS_RUNTIME=docker\n")
    core_ok.setattr(envfile, "default_path", lambda: f)
    env = {}
    r = doctor.run_checks(_args(pegasus=True), env=env, claude_dir=tmp_path)
    got = rows(r)
    assert got["settings"][0] == "ok" and "ssh host maxwell" in got["settings"][1]
    assert got["ssh"][0] == "ok" and fake.calls[0][0] == "ssh"
    assert env["CURATE_PEGASUS_HOST"] == "maxwell"  # filled from the file, as for deploy
    # A missing settings file is fine for Pegasus: every setting has a default.
    core_ok.setattr(envfile, "default_path", lambda: tmp_path / "none.env")
    r = doctor.run_checks(_args(pegasus=True), env={}, claude_dir=tmp_path)
    assert not [x for x in r.rows if x["check"] == "settings file"]
    assert rows(r)["settings"][0] == "ok" and not r.failed()


def test_cli_flag(core_ok, capsys):
    core_ok.setattr(doctor, "_run", FakeHost())
    assert doctor.main(["--pegasus"]) == 0
    out = capsys.readouterr().out
    assert "pegasus  pegasus-version" in out and "Pegasus 5.1.3" in out
