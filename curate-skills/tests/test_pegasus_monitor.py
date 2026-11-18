"""pegasus_monitor.py (via monitor.py poll) for a Pegasus run: lines, verdicts, state.monitor, stage
changes, Gate 5 and the catalog check; locally and over ssh, with a scripted fake host; and once
with the real backend on a synthetic finished run folder. No Pegasus, HTCondor or ssh."""
import json
import shlex
import subprocess
from pathlib import Path

import pytest

import catalog
import deploy as D
import hostcmd
import monitor
import pegasus_deploy as PD
import pegasus_monitor as PM
import spec as S
from conftest import call, ok
from test_pegasus_deploy import Fake, peg_run, run_steps, tiny_ir  # noqa: F401  (fixtures)
from test_start import source_run, state  # noqa: F401  (fixture)

JOBS = ["generate_text", "split", "map__1", "map__2", "map__3", "shuffle",
        "reduce__1", "reduce__2", "reduce__3", "reduce__4", "reduce__5"]


def st(state_, done=(), running=(), failed=(), exit_=None):
    jobs = {j: "done" if j in done else "running" if j in running else "failed" if j in failed else "pending"
            for j in JOBS}
    counts = {}
    for v in jobs.values():
        counts[v] = counts.get(v, 0) + 1
    return {"workflow": "wf", "state": state_, "dagman_exit": exit_, "run_dir": "/r", "submitted_at": "t",
            "jobs": jobs, "counts": counts}


ALL = st("success", done=JOBS, exit_=0)


class Host:
    """Scripted backend answers: `statuses` in order (the last repeats), then `collect`."""

    def __init__(self, statuses, collect=None, remote_files=None):
        self.statuses, self.collect, self.calls = list(statuses), collect, []
        self.remote_files = remote_files or {}

    def __call__(self, cmd, timeout=600):
        self.calls.append(cmd)
        if cmd[0] == "scp":
            src, dest = cmd[-2].split(":", 1)[1], Path(cmd[-1])
            name = Path(src).name
            if name == "run-report.json":
                (dest / name).write_text(json.dumps(self.collect))
            else:
                (dest / name / "results").mkdir(parents=True, exist_ok=True)
                for f in self.collect.get("delivered") or []:
                    (dest / name / f).write_text("x")
            return 0, "", ""
        command = cmd[-1]
        if " status " in command:
            s_ = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return 0, json.dumps(s_), ""
        if " collect " in command:
            return (0 if self.collect["status"] == "success" else 1), json.dumps(self.collect), ""
        raise AssertionError(f"unexpected {cmd}")


def report(status="success", delivered=None, missing=()):
    return {"status": status, "delivered": delivered if delivered is not None else
            [f"results/word_count_{i}.json" for i in range(1, 6)], "missing": list(missing), "jobs": {}}


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def launched(peg_run, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(PD, "RUN", Fake(tmp_path))
    run_steps(peg_run, "prepare", "launch")
    assert state(peg_run)["stage"] == "monitor"
    return peg_run


def poll(run, host, monkeypatch, clock=None, **kw):
    monkeypatch.setattr(PM, "RUN", host)
    clock = clock or Clock()
    return PM.poll(str(run), clock=clock, sleep=clock.sleep, **kw)


# ── local ──

def test_running_then_success(launched, monkeypatch, capsys):
    host = Host([st("running", done=["generate_text"], running=["split"]),
                 st("running", done=["generate_text", "split"], running=["map__1", "map__2"]),
                 ALL], collect=report())
    out = poll(launched, host, monkeypatch)
    s = state(launched)
    work = s["pegasus"]["work_dir"]
    assert out["result"] == "success" and out["stage"] == "finalize" and out["next_skill"] == "curate-skills:wda-finalize"
    assert out["lines"] == ["✓ generate_text succeeded", "… split running", "✓ split succeeded",
                            "… map running (0/3)", "✓ map succeeded (3/3)", "✓ shuffle succeeded",
                            "✓ reduce succeeded (5/5)", f"✓ workflow succeeded (11 jobs; results in {work}/deliver)"]
    mon = s["monitor"]
    assert mon["target"] == "pegasus" and mon["run_dir"] == s["pegasus"]["run_dir"] and mon["result"] == "success"
    assert mon["deliver_dir"] == f"{work}/deliver" and mon["run_report"] == f"{work}/run-report.json"
    assert len(mon["delivered"]) == 5 and mon["missing"] == [] and mon["dagman_exit"] == 0
    assert s["history"][-1]["gate"] == "monitor" and s["history"][-1]["decision"] == "succeeded"
    assert (launched.resolve() / "pegasus" / "monitor-report.md").is_file()
    # the backend ran with the plugin's backend and the run's own IR and work folders, here
    (first,) = host.calls[:1]
    assert first[:2] == ["sh", "-c"] and f"{PM.IR_TOOLS}/backends/pegasus_backend.py status" in first[2]
    assert f"--work {work}" in first[2]
    # finalize and Gate 5 work from it
    assert S.require_problems(S.load_state(launched), "finalize") == []
    assert catalog.deployed_run_problem(s) is None
    md = ok(S, ["show", "--run", str(launched), "--gate", "5"], capsys)["markdown"]
    assert f"**Pegasus run** `{s['pegasus']['name']}` on this machine: success — 11/11 jobs done, DAGMan exit 0" in md
    assert f"- Delivered to `{work}/deliver/`: `results/word_count_1.json`, `results/word_count_2.json`" in md
    assert "Output correctness is not judged" in md


def test_pending_across_calls_shows_only_changes(launched, monkeypatch):
    host = Host([st("running", running=["generate_text"])], collect=report())
    out = poll(launched, host, monkeypatch, chunk=20)
    assert out["result"] == "pending" and out["lines"] == ["… generate_text running"]
    assert state(launched)["stage"] == "monitor" and state(launched)["monitor"]["result"] == "pending"
    out = poll(launched, Host([st("running", running=["generate_text"])]), monkeypatch, chunk=20)
    assert out["lines"] == []  # nothing changed
    out = poll(launched, Host([ALL], collect=report()), monkeypatch)
    assert out["result"] == "success" and "… generate_text running" not in out["lines"]
    assert state(launched)["monitor"]["polls"] >= 3


def test_failure_stops_and_blocks_finalize(launched, monkeypatch):
    host = Host([st("failure", done=JOBS[:5], failed=["shuffle"], exit_=1)],
                collect=report("failure (DAGMan status 1)", delivered=[],
                               missing=[f"word_count_{i}.json" for i in range(1, 6)]))
    out = poll(launched, host, monkeypatch)
    assert out["result"] == "failure" and not out["ok"] and out["stage"] == "stopped"
    assert out["lines"][-1].startswith("✗ workflow failed: step shuffle failed; DAGMan exit 1; missing outputs: ")
    s = state(launched)
    assert out["recheck_command"] == f"/curate-skills:curate-monitor --run {s['run_id']}"
    assert s["stopped_from"] == "monitor" and s["monitor"]["failed"] == ["shuffle"]
    assert "pegasus-analyzer" in (launched.resolve() / "pegasus" / "monitor-report.md").read_text()
    s["stage"] = "finalize"
    assert S.require_problems(s, "finalize")[0].startswith("the Pegasus run has not been confirmed to work (monitor result failure)")
    assert catalog.deployed_run_problem(s).startswith("the Pegasus run has not been confirmed")


def test_timeout_then_recheck(launched, monkeypatch, capsys):
    out = poll(launched, Host([st("running", running=["generate_text"])]), monkeypatch, minutes="0.5")
    assert out["result"] == "timeout" and out["stage"] == "stopped"
    assert out["lines"][-1] == "… not finished after 0.5 min (still running under DAGMan)"
    ok(S, ["begin", "--run", str(launched), "--stage", "monitor"], capsys)  # the re-check
    out = poll(launched, Host([ALL], collect=report()), monkeypatch)
    assert out["result"] == "success" and out["rechecked"] == "timeout" and out["stage"] == "finalize"
    assert state(launched)["history"][-1]["text"].endswith("(re-checked after timeout)")


def test_report_only_writes_and_collects_nothing(launched, monkeypatch):
    before = (launched / "state.json").read_text()
    host = Host([ALL], collect=report())
    out = poll(launched, host, monkeypatch, report_only=True)
    assert out["mode"] == "report only" and out["result"] == "success" and "report" in out
    assert not any(" collect " in c[-1] for c in host.calls)
    assert (launched / "state.json").read_text() == before


def test_not_launched_is_refused(peg_run):  # noqa: F811
    with pytest.raises(PM.InputError, match="was not launched"):
        PM.poll(str(peg_run))


# ── over ssh ──

def test_remote_status_collect_and_fetch(peg_run, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("CURATE_PEGASUS_HOST", "maxwell")
    fake = Fake(tmp_path)
    monkeypatch.setattr(PD, "RUN", fake)
    run_steps(peg_run, "prepare", "copy", "launch")
    rec = state(peg_run)["pegasus"]
    host = Host([ALL], collect=report())
    out = poll(peg_run, host, monkeypatch)
    res = peg_run.resolve() / "pegasus" / "result"
    assert out["result"] == "success" and out["lines"][-1].endswith(f"results in {res}/deliver)")
    ssh = [c for c in host.calls if c[0] == "ssh"]
    assert ssh and all(c[1:6] == ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "maxwell"] for c in ssh)
    assert f"{rec['remote_dir']}/curate_ir/backends/pegasus_backend.py status {rec['remote_dir']}/ir/ir.yaml" in ssh[0][-1]
    scp = [c for c in host.calls if c[0] == "scp"]
    assert [c[-2] for c in scp] == [f"maxwell:{rec['work_dir']}/run-report.json", f"maxwell:{rec['work_dir']}/deliver"]
    assert (res / "run-report.json").is_file() and (res / "deliver" / "results" / "word_count_1.json").is_file()
    mon = state(peg_run)["monitor"]
    assert mon["deliver_dir"] == str(res / "deliver") and mon["run_report"] == str(res / "run-report.json")


# ── monitor.py dispatch ──

def test_monitor_cli_dispatches_without_github(launched, monkeypatch, capsys):
    monkeypatch.setattr(D, "get_token", lambda: (_ for _ in ()).throw(AssertionError("no token for Pegasus")))
    monkeypatch.setattr(PM, "RUN", Host([ALL], collect=report()))
    c = Clock()
    code = monitor.main(["poll", "--run", str(launched)], clock=c, sleep=c.sleep)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["result"] == "success"
    st_ = ok(monitor, ["status", "--run", str(launched)], capsys)
    assert st_["result"] == "success" and st_["deliver_dir"].endswith("/pegasus/work/deliver")
    code, out = call(monitor, ["download-outputs", "--run", str(launched), "--to", "/tmp/x"], capsys)
    assert code == 2 and "already on disk" in out["error"]


def test_faasr_status_has_no_new_keys(source_run, capsys):  # noqa: F811
    out = ok(monitor, ["status", "--run", str(source_run)], capsys)
    assert set(out) == {"ok", "stage", "result", "accepted", "minutes_used", "settings", "summary"}
    assert monitor._is_pegasus(str(source_run)) is False


# ── the real backend on a finished run folder (no Pegasus needed) ──

def _system_yaml() -> bool:
    p = subprocess.run(["python3", "-c", "import yaml"], env=hostcmd.host_env(), capture_output=True)
    return p.returncode == 0


@pytest.mark.skipif(not _system_yaml(), reason="the system python3 has no PyYAML")
def test_real_backend_local(launched, monkeypatch, tmp_path):
    ir = tiny_ir(tmp_path)  # 3 jobs: a, b__1, b__2 -> b_{1,2}.txt delivered
    work = tmp_path / "work"
    run_dir = work / "submit" / "u" / "pegasus" / "wf" / "run0001"
    run_dir.mkdir(parents=True)
    (work / "submit.json").write_text(json.dumps({"workflow": "wf", "work": str(work), "run_dir": str(run_dir),
                                                  "submitted_at": "t0", "runtime": "docker"}))
    (run_dir / "jobstate.log").write_text("".join(f"1 {j} POST_SCRIPT_SUCCESS 0 condorpool - 1\n"
                                                  for j in ("a_a", "b_b__1", "b_b__2")))
    (run_dir / "wf-0.dag.dagman.out").write_text("x EXITING WITH STATUS 0\n")
    (work / "output").mkdir()
    for i in (1, 2):
        (work / "output" / f"b_{i}.txt").write_text(f"b{i}")
    s = state(launched)
    s["pegasus"].update(ir_dir=str(ir.parent), work_dir=str(work), run_dir=str(run_dir))
    (launched / "state.json").write_text(json.dumps(s))
    c = Clock()
    out = PM.poll(str(launched), clock=c, sleep=c.sleep)  # PM.RUN is the real hostcmd.run
    assert out["result"] == "success", out
    assert out["lines"] == ["✓ a succeeded", "✓ b succeeded (2/2)", f"✓ workflow succeeded (3 jobs; results in {work}/deliver)"]
    assert (work / "deliver" / "results" / "b_2.txt").read_text() == "b2"
    assert json.loads((work / "run-report.json").read_text())["status"] == "success"
