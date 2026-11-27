"""pegasus_deploy.py: preflight and the asked steps (prepare, copy, launch), locally and over ssh,
with a fake command runner (no Pegasus, HTCondor, ssh or network); and pegasus_backend.py's
status / collect on a run folder shaped like a real one."""
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import runstate

import pegasus_deploy as PD
import spec as S
from conftest import ok
from test_start import source_run, state  # noqa: F401  (fixture)

IR_DIR = Path(__file__).resolve().parents[1] / "curate_ir"
BACKEND = IR_DIR / "backends" / "pegasus_backend.py"
pytest.importorskip("yaml")


class Fake:
    """Answers PD.RUN. Local `sh -c` commands act on the real file system; ssh commands act on a
    fake home folder (`remote_home`, mapped from the remote's absolute paths); scp copies there."""

    def __init__(self, tmp_path, submit_ok=True):
        self.calls, self.submit_ok = [], submit_ok
        self.home = tmp_path / "remote-home"
        self.home.mkdir()
        self.remote_home = "/home/alice"

    def _r(self, p: str) -> Path:  # a remote absolute path -> the fake folder
        p = p.replace("~", self.remote_home, 1) if p.startswith("~") else p
        assert p.startswith(self.remote_home), p
        return self.home / p[len(self.remote_home):].lstrip("/")

    def __call__(self, cmd, timeout=600):
        self.calls.append(cmd)
        if cmd[0] == "scp":
            dest = self._r(cmd[-1].split(":", 1)[1].rstrip("/"))
            for src in cmd[-3:-1]:
                shutil.copytree(src, dest / Path(src).name)
            return 0, "", ""
        remote = cmd[0] == "ssh"
        command = cmd[-1]
        if command == "pegasus-version":
            return 0, "5.1.3\n", ""
        if command == "cd && pwd":
            return 0, self.remote_home + "\n", ""
        if command.startswith("test -e "):
            p = shlex.split(command)[2]
            path = self._r(p) if remote else Path(p)
            return (0 if path.exists() else 1), "", ""
        if command.startswith("mkdir -p "):
            a, b = shlex.split(command.replace(" && ", " ; "))[2], shlex.split(command.split(" && ")[1])[1]
            self._r(a).mkdir(parents=True, exist_ok=True)
            try:
                self._r(b).mkdir()
            except FileExistsError:
                return 1, "", "mkdir: File exists"
            return 0, "", ""
        if command.startswith("rm -rf "):
            p = shlex.split(command)[2]
            shutil.rmtree(self._r(p) if remote else Path(p), ignore_errors=True)
            return 0, "", ""
        if " submit " in command:
            if not self.submit_ok:
                return 1, "", "pegasus-plan failed (exit 1); see work/pegasus-plan.log"
            args = shlex.split(command.split("python3 ", 1)[1])
            work = args[args.index("--work") + 1]
            w = self._r(work) if remote else Path(work)
            w.mkdir(parents=True)
            run_dir = f"{work}/submit/u/pegasus/wf/run0001"
            rec = {"workflow": "mapreduce-word-count-x", "work": work, "run_dir": run_dir,
                   "submitted_at": "2026-11-18T10:00:00-0800", "runtime": "docker", "dagman_cluster": 412}
            (w / "submit.json").write_text(json.dumps(rec))
            return 0, json.dumps(rec, indent=2), "warn : contract faasr (...)\n"
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture
def peg_run(source_run, capsys):  # noqa: F811
    s = state(source_run)
    s["stage"] = "gate4"
    runstate.save(source_run, s)
    ok(S, ["approve", "--session", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus"], capsys)
    return source_run


@pytest.fixture
def fake(monkeypatch, tmp_path):
    f = Fake(tmp_path)
    monkeypatch.setattr(PD, "RUN", f)
    return f


def run_steps(run, *names, **kw):
    for n in names:
        out = PD.step(str(run), n, confirmed=True, **kw)
        assert out["ok"], out
    return out


def ex(run):
    """The run Gate 4 opened for this versioned run: its bundle folder and work folder."""
    e = state(run)["execution"]
    return Path(e["bundle_dir"]), Path(e["dir"]) / "work", e["reference"]


# ── local ──

def test_preflight_local(peg_run, fake):
    out = PD.preflight(str(peg_run))
    assert out["ok"], out
    assert out["steps"] == ["prepare", "launch"] and out["next_step"] == "prepare"
    s = state(peg_run)
    assert out["summary"].startswith(f"Pegasus run `{s['workflow_id']}` "  # named after the version
                                     "on this machine: 11 jobs, image `docker:///python:3.12-slim` (the built-in default), "
                                     "runtime docker")
    ir, _, ref = ex(peg_run)
    assert ir.parent.parent.parent.name == state(peg_run)["workflow_id"] and ref.startswith(state(peg_run)["workflow_id"] + "/")
    assert f"- writes `{ir}/` (workflow.yml, functions/ 5 files, inputs/): new" in out["summary"]
    assert "- input files: none" in out["summary"]
    # read-only: the run Gate 4 opened holds only its state
    assert not ir.exists() and {p.name for p in ir.parent.iterdir()} <= {"state.json", "state.lock"}


def test_each_step_asks_first_then_runs(peg_run, fake):
    out = PD.step(str(peg_run), "prepare")
    assert out["needs_confirmation"] and out["plan"]["what"].startswith("write the Pegasus workflow folder")
    ir, work_p, _ = ex(peg_run)
    assert not ir.exists() and {p.name for p in ir.parent.iterdir()} <= {"state.json", "state.lock"}
    out = PD.step(str(peg_run), "prepare", confirmed=True)
    assert out["ok"] and out["progress"] == "prepared" and out["next_step"] == "launch"
    assert (ir / "workflow.yml").is_file() and len(list((ir / "functions").iterdir())) == 5
    assert (ir.parent / "manifest.json").is_file()  # built from the approved version, then the manifest
    import ids
    assert ids.check_manifest(ir.parent, state(peg_run)["version_hash"])["target"] == "pegasus"
    assert PD.step(str(peg_run), "launch")["needs_confirmation"]
    assert not any(" submit " in c[-1] for c in fake.calls)
    out = PD.step(str(peg_run), "launch", confirmed=True)
    assert out["ok"] and out["progress"] == "prepared · launched" and out["stage"] == "monitor"
    assert "DAGMan cluster 412" in out["message"]
    s = state(peg_run)
    rec = s["pegasus"]
    work = str(work_p)
    assert rec["host"] is None and rec["runtime"] == "docker" and rec["jobs"] == 11
    assert rec["run_dir"] == f"{work}/submit/u/pegasus/wf/run0001" and rec["work_dir"] == work
    assert rec["submitted_at"] and rec["dagman_cluster"] == 412
    assert all(rec["steps"][k]["ok"] for k in ("prepare", "launch")) and "copy" not in rec["steps"]
    log = PD.read_log(peg_run.resolve())
    assert [(e["step"], e["where"], e["path"]) for e in log] == [("prepare", "local", str(ir)), ("launch", "local", work)]
    # submitted with the plugin's backend and the Pegasus Python path, then nothing waits
    (submit,) = [c for c in fake.calls if " submit " in c[-1]]
    assert submit[:2] == ["sh", "-c"] and submit[2].startswith('PYTHONPATH="$(pegasus-config --python)" python3 ')
    assert str(BACKEND) in submit[2] and "--runtime docker" in submit[2]
    assert S.require_problems(S.load_state(peg_run), "monitor") == []  # monitor can start from it


def test_steps_in_order_and_once(peg_run, fake):
    out = PD.step(str(peg_run), "launch", confirmed=True)
    assert not out["ok"] and out["problems"] == ["the next step is prepare"]
    run_steps(peg_run, "prepare")
    assert PD.step(str(peg_run), "prepare", confirmed=True)["problems"] == ["prepare is already done"]
    assert PD.step(str(peg_run), "copy")["problems"] == ["copy is not a step on this machine"]


def test_nothing_overwritten_and_overwrite_own(peg_run, fake):
    ir, _, _ = ex(peg_run)
    ir.mkdir(parents=True)
    (ir / "mine.txt").write_text("not CURATE's")
    pf = PD.preflight(str(peg_run))
    assert not pf["ok"] and f"{ir} already exists; nothing is overwritten" in pf["problems"]
    out = PD.step(str(peg_run), "prepare", confirmed=True)
    assert not out["ok"] and out["error"].startswith(f"{ir} already exists")
    out = PD.step(str(peg_run), "prepare", confirmed=True, overwrite_own=True)  # not logged as ours
    assert not out["ok"] and (ir / "mine.txt").exists()


def test_failed_launch_retry_with_overwrite_own(peg_run, fake, monkeypatch, tmp_path):
    run_steps(peg_run, "prepare")
    fake.submit_ok = False
    out = PD.step(str(peg_run), "launch", confirmed=True)
    assert not out["ok"] and "pegasus-plan failed" in out["error"]
    s = state(peg_run)
    assert s["stage"] == "deploy" and s["pegasus"]["steps"]["launch"]["ok"] is False
    _, work, _ = ex(peg_run)
    work.mkdir()  # what the failed submit left behind
    fake.submit_ok = True
    assert "already exists" in PD.step(str(peg_run), "launch", confirmed=True)["error"]
    out = PD.step(str(peg_run), "launch", confirmed=True, overwrite_own=True)
    assert out["ok"] and state(peg_run)["stage"] == "monitor"


def test_inputs_from_a_folder(peg_run, fake, monkeypatch, tmp_path):
    import execution as X
    import pegasus_target as P
    real = X.pegasus_built

    def with_input(*a, **kw):
        b = real(*a, **kw)
        b["needed"] = ["seed.txt"]
        return b
    monkeypatch.setattr(X, "pegasus_built", with_input)
    monkeypatch.setattr(P, "plan", lambda s, st: {"ok": True, "name": "n", "jobs": 11, "functions": 5,
                                                   "inputs": ["seed.txt"], "deliverables": [], "warnings": []})
    pf = PD.preflight(str(peg_run))
    assert not pf["ok"] and pf["problems"][0].startswith("this workflow needs input files (seed.txt)")
    d = tmp_path / "in"
    d.mkdir()
    assert PD.preflight(str(peg_run), str(d))["problems"] == [f"not in {d}: seed.txt"]
    (d / "seed.txt").write_text("hello")
    assert PD.preflight(str(peg_run), str(d))["ok"]
    run_steps(peg_run, "prepare", inputs_dir=str(d))
    assert (ex(peg_run)[0] / "inputs" / "seed.txt").read_text() == "hello"
    assert state(peg_run)["pegasus"]["inputs_from"] == str(d.resolve())


def test_refuses_runs_that_did_not_choose_pegasus(source_run, fake):  # noqa: F811
    out = PD.preflight(str(source_run))  # Gate 4 approved without deploying
    assert not out["ok"] and out["problems"] == ["Gate 4 did not choose the Pegasus target for this run"]
    assert PD.step(str(source_run), "prepare", confirmed=True)["problems"] == out["problems"]
    assert fake.calls == []


def test_settings_pinned_after_prepare(peg_run, fake, monkeypatch):
    run_steps(peg_run, "prepare")
    monkeypatch.setenv("CURATE_PEGASUS_RUNTIME", "singularity")
    assert PD.step(str(peg_run), "launch", confirmed=True)["problems"] == [
        "the Pegasus settings changed since prepare; set them back"]


# ── over ssh ──

def test_remote_host_copy_and_launch(peg_run, fake, monkeypatch):
    monkeypatch.setenv("CURATE_PEGASUS_HOST", "maxwell")
    pf = PD.preflight(str(peg_run))
    rid = ex(peg_run)[2]  # the full reference: <workflow-id>/<run-id>
    assert pf["ok"] and pf["steps"] == ["prepare", "copy", "launch"]
    assert f"- copies to `maxwell:~/curate-runs/{rid}/` (bundle/, curate_ir/): new" in pf["summary"]
    run_steps(peg_run, "prepare")
    plan = PD.step(str(peg_run), "copy")["plan"]
    assert plan["paths"] == [f"maxwell:~/curate-runs/{rid}/bundle/", f"maxwell:~/curate-runs/{rid}/curate_ir/"]
    run_steps(peg_run, "copy")
    remote = fake.home / "curate-runs" / rid
    assert (remote / "bundle" / "workflow.yml").is_file() and (remote / "curate_ir" / "backends" / "pegasus_backend.py").is_file()
    out = run_steps(peg_run, "launch")
    assert out["progress"] == "prepared · copied · launched"
    rec = state(peg_run)["pegasus"]
    assert rec["host"] == "maxwell" and rec["remote_dir"] == f"/home/alice/curate-runs/{rid}"
    assert rec["run_dir"] == f"/home/alice/curate-runs/{rid}/work/submit/u/pegasus/wf/run0001"
    ssh = [c for c in fake.calls if c[0] == "ssh"]
    assert ssh and all(c[1:6] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "maxwell"] for c in ssh)
    (submit,) = [c for c in ssh if " submit " in c[-1]]
    assert f"/home/alice/curate-runs/{rid}/curate_ir/backends/pegasus_backend.py" in submit[-1]
    assert [e["where"] for e in PD.read_log(peg_run.resolve())] == ["local", "maxwell", "maxwell"]


def test_remote_folder_that_exists_is_refused(peg_run, fake, monkeypatch):
    monkeypatch.setenv("CURATE_PEGASUS_HOST", "maxwell")
    rid = ex(peg_run)[2]
    (fake.home / "curate-runs" / rid).mkdir(parents=True)
    pf = PD.preflight(str(peg_run))
    assert not pf["ok"] and f"maxwell:~/curate-runs/{rid} already exists; nothing is overwritten" in pf["problems"]
    run_steps(peg_run, "prepare")
    out = PD.step(str(peg_run), "copy", confirmed=True)
    assert not out["ok"] and "already exists" in out["error"]
    out = PD.step(str(peg_run), "copy", confirmed=True, overwrite_own=True)  # not ours: still refused
    assert not out["ok"] and (fake.home / "curate-runs" / rid).is_dir()


# ── pegasus_backend.py status / collect (real files, no Pegasus) ──

JOBSTATE = """1794986571 INTERNAL *** MONITORD_STARTED ***
1794986569 INTERNAL *** DAGMAN_STARTED 155.0 ***
1794986570 create_dir_wf_0_local SUBMIT 156.0 local - 1
1794986590 a_a SUBMIT 160.0 condorpool - 2
1794986595 a_a EXECUTE 160.0 condorpool - 2
1794986600 a_a JOB_TERMINATED 160.0 condorpool - 2
1794986601 a_a JOB_SUCCESS 0 condorpool - 2
1794986602 a_a POST_SCRIPT_SUCCESS 0 condorpool - 2
1794986603 b_b__1 SUBMIT 161.0 condorpool - 3
1794986604 b_b__1 EXECUTE 161.0 condorpool - 3
1794986605 b_b__2 SUBMIT 162.0 condorpool - 4
1794986606 b_b__2 JOB_FAILURE 1 condorpool - 4
"""


def backend(cmd, ir, work):
    return subprocess.run([sys.executable, str(BACKEND), cmd, str(ir), "--work", str(work)],
                          capture_output=True, text=True, timeout=60)


def tiny_ir(tmp_path):
    """A small Pegasus folder built the real way (spec -> spec_cwl -> cwl_pegasus): step a, then b ×2."""
    import cwl_pegasus as CP
    import spec_cwl
    from curate_models import WorkflowSpec
    sys.path.insert(0, str(IR_DIR))
    from cwl.validate import validate
    code = "def main(inputs, outputs, params):\n    pass\n"
    spec = WorkflowSpec.model_validate({"name": "wf", "folder": "wf", "nodes": [
        {"name": "a", "code": code, "inputs": [], "outputs": [{"name": "a.txt"}]},
        {"name": "b", "code": code, "rank": 2, "inputs": [{"name": "a.txt"}], "outputs": [{"name": "b_{rank}.txt"}]}],
        "edges": [{"from_node": "a", "to_node": "b"}]})
    cwl = spec_cwl.write(spec_cwl.build(spec), tmp_path / "cwl")
    wf, errors, _ = validate(cwl / "workflow.yml", cwl / "inputs.yml")
    assert errors == []
    return CP.write_folder(tmp_path / "bundle", CP.build(wf, "wf")) / "workflow.yml"


def test_backend_status_and_collect(tmp_path):
    ir = tiny_ir(tmp_path)
    work = tmp_path / "work"
    run_dir = work / "submit" / "u" / "pegasus" / "wf" / "run0001"
    run_dir.mkdir(parents=True)
    assert backend("status", ir, work).returncode != 0  # never submitted
    (work / "submit.json").write_text(json.dumps({"workflow": "wf", "work": str(work), "run_dir": str(run_dir),
                                                  "submitted_at": "t0", "runtime": "docker"}))
    (run_dir / "jobstate.log").write_text(JOBSTATE)
    (run_dir / "wf-0.dag.dagman.out").write_text("11/18/26 10:00:00 DAGMan starting\n")
    st = json.loads(backend("status", ir, work).stdout)
    assert st["state"] == "running" and st["dagman_exit"] is None
    assert st["jobs"] == {"a": "done", "b__1": "running", "b__2": "failed"}
    p = backend("collect", ir, work)
    assert p.returncode != 0 and "still running" in p.stderr
    with open(run_dir / "wf-0.dag.dagman.out", "a") as f:
        f.write("11/18/26 10:05:00 **** condor_scheduniv_exec.155.0 (condor_DAGMAN) pid 1 EXITING WITH STATUS 1\n")
    assert json.loads(backend("status", ir, work).stdout)["state"] == "failure"
    (work / "output").mkdir()
    b1, b2 = "b__output1__1__b_1.txt", "b__output1__2__b_2.txt"  # the generated names
    (work / "output" / b1).write_text("one")
    p = backend("collect", ir, work)
    rep = json.loads(p.stdout)
    assert p.returncode == 1 and rep["status"] == "failure (DAGMan status 1)"
    assert rep["delivered"] == ["results/b_1.txt"] and rep["missing"] == [b2]
    assert (work / "deliver" / "results" / "b_1.txt").read_text() == "one"
    assert json.loads((work / "run-report.json").read_text())["jobs"]["b__2"] == "failed"


def test_backend_submit_refuses_a_submitted_folder(tmp_path):
    ir = tiny_ir(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    (work / "submit.json").write_text("{}")
    p = backend("submit", ir, work)
    assert p.returncode != 0 and "was already submitted" in p.stderr


# ── commands run with the system's Python, not uv's ──

def test_host_env_drops_every_venv_bin_only(tmp_path):
    venvs = []
    for name in ("builds/tmpA", "archive/M8y", "proj/.venv"):  # what `uv run --with` stacks
        d = tmp_path / name
        (d / "bin").mkdir(parents=True)
        (d / "pyvenv.cfg").write_text("home = /x\n")
        venvs.append(str(d / "bin"))
    plain = tmp_path / "tools" / "bin"
    plain.mkdir(parents=True)
    env = {"PATH": ":".join(venvs + [str(plain), "/usr/bin"]), "VIRTUAL_ENV": str(tmp_path / "builds/tmpA"),
           "PYTHONHOME": "/p", "HOME": "/home/u"}
    assert PD.host_env(env) == {"PATH": f"{plain}:/usr/bin", "HOME": "/home/u"}
    assert PD.host_env({"PATH": "/a:/b"}) == {"PATH": "/a:/b"}  # no venv: unchanged
    # uv run --python 3.13 also puts its managed interpreter first (not a venv)
    env = {"PATH": "/home/u/.local/share/uv/python/cpython-3.13.18-linux/bin:/home/u/.local/bin:/usr/bin",
           "HOME": "/home/u"}
    assert PD.host_env(env)["PATH"] == "/home/u/.local/bin:/usr/bin"
    env = {"PATH": "/data/uvpy/cpython-3.13/bin:/usr/bin", "UV_PYTHON_INSTALL_DIR": "/data/uvpy", "HOME": "/h"}
    assert PD.host_env(env)["PATH"] == "/usr/bin"
    env = {"PATH": "/x/uv/python/c/bin:/usr/bin", "XDG_DATA_HOME": "/x", "HOME": "/h"}
    assert PD.host_env(env)["PATH"] == "/usr/bin"


def test_commands_get_the_host_env(monkeypatch):
    seen = {}
    import hostcmd
    monkeypatch.setattr(hostcmd.subprocess, "run", lambda cmd, **kw: seen.update(kw) or
                        subprocess.CompletedProcess(cmd, 0, "", ""))
    PD._run(["true"])
    assert seen["env"] == PD.host_env()
    assert PD.host_env is hostcmd.host_env and PD.SSH_OPTS == hostcmd.SSH_OPTS
