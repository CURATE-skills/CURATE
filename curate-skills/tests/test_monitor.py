"""curate-monitor (monitor.py) against fakes: nothing reaches GitHub or S3."""
import io
import json
import urllib.error
from datetime import timedelta
from pathlib import Path

import pytest

import monitor as M

FAKE = {"GH_PAT": "ghp_" + "M" * 36, "S3_AccessKey": "AKIA" + "N" * 16, "S3_SecretKey": "s3cr3t-" + "p" * 33}
INV = "2026-11-01-17-33-10"
TS = "2026-11-01T17-33-10"
DISPATCHED = "2026-11-02T01:33:09+00:00"
AFTER = "2026-11-02T01:40:00+00:00"
BEFORE = "2026-11-01T20:00:00+00:00"  # an earlier invocation's files
FOLDER = f"FaaSrLog/wx-cs/{TS}/{INV}"

WF = {"WorkflowName": "wx-cs", "FunctionInvoke": "download", "FaaSrLog": "FaaSrLog", "DefaultDataStore": "S3",
      "DataStores": {"S3": {"Bucket": "b", "AccessKey": "S3_AccessKey", "SecretKey": "S3_SecretKey"}},
      "ActionList": {
          "download": {"FunctionName": "dl", "Arguments": {"folder": "wx-cs", "output1": "station.csv"},
                       "InvokeNext": ["process(3)"]},
          "process": {"FunctionName": "pr", "Arguments": {"folder": "wx-cs", "input1": "station.csv",
                                                          "output1": "cmp_{rank}.csv"}, "InvokeNext": ["plot"]},
          "plot": {"FunctionName": "pl", "Arguments": {"folder": "wx-cs", "input1": "cmp_{rank}.csv",
                                                       "output1": "fig.png"}, "InvokeNext": []}}}
LABELS = ["download", "process(1)", "process(2)", "process(3)", "plot"]
OUTPUTS = ["wx-cs/station.csv", "wx-cs/cmp_1.csv", "wx-cs/cmp_2.csv", "wx-cs/cmp_3.csv", "wx-cs/fig.png"]


@pytest.fixture
def env(monkeypatch):
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(M.D.shutil, "which", lambda name: None)


@pytest.fixture
def run(tmp_path):
    bundle = tmp_path / "faasr_output" / "wx"
    bundle.mkdir(parents=True)
    (bundle / "workflow.json").write_text("{}")
    r = tmp_path / ".curate" / "runs" / "r1"
    (r / "deploy").mkdir(parents=True)
    (r / "deploy" / "workflow.json").write_text(json.dumps(WF))
    (r / "state.json").write_text(json.dumps({
        "schema_version": 2, "run_id": "r1", "stage": "monitor", "bundle_dir": str(bundle), "deploy_choice": "yes",
        "spec": {"name": "wx", "nodes": [{"name": "dl"}, {"name": "pr"}, {"name": "pl"}]},
        "iteration": 0, "max_iterations": 5,
        "history": [{"gate": 4, "decision": "approve"}],
        "deploy": {"deploy_name": "wx-cs", "repo": "alice/r", "branch": "main", "invocation_id": INV,
                   "invocation_timestamp": TS, "dispatched_at": DISPATCHED, "steps": {"invoke": {"ok": True}}}}))
    return r


class World:
    """A fake GitHub (Actions endpoints, log redirect to a blob host) and a fake S3 for one invocation."""

    def __init__(self):
        self.runs = {}      # action -> list of run dicts
        self.logs = {}      # job id -> text (None: no log available)
        self.s3 = {}        # key -> (text, last_modified)
        self.blob_fetches = {}
        self.requests = []
        self.s3_calls = []
        self._next = 100

    # -- building a scenario --
    def add_run(self, action, conclusion="success", log=None, status="completed", created=AFTER):
        self._next += 1
        rid = self._next
        self.runs.setdefault(action, []).append({"id": rid, "status": status, "conclusion": conclusion,
                                                 "html_url": f"https://github.com/alice/r/actions/runs/{rid}",
                                                 "created_at": created})
        self.logs[rid] = f"##[group]Run cd /action\n  OVERWRITTEN: {{\"InvocationID\": \"{INV}\"}}\nall good\n" \
            if log is None else log
        return rid

    def finish_all(self):
        self.add_run("download")
        for _ in range(3):
            self.add_run("process")
        self.add_run("plot")
        for label in LABELS:
            self.s3[f"{FOLDER}/{label}.txt"] = (f"[1.0] [INFO] {label} ran\n", AFTER)
            done = label.replace("(", ".").replace(")", "")
            self.s3[f"{FOLDER}/function_completions/{done}.done"] = ("", AFTER)
        for key in OUTPUTS:
            self.s3[key] = ("data", AFTER)
        return self

    # -- the fake GitHub --
    def __call__(self, req, timeout=None):
        self.requests.append(req)
        assert req.get_method() == "GET", "the monitor must never write"
        url = req.full_url
        if url.startswith("https://blob.example/"):
            assert "Authorization" not in req.headers and "authorization" not in {k.lower() for k in req.headers}
            jid = int(url.rsplit("/", 1)[1])
            self.blob_fetches[jid] = self.blob_fetches.get(jid, 0) + 1
            return _resp(self.logs[jid].encode())
        assert req.headers["Authorization"] == f"Bearer {FAKE['GH_PAT']}"
        path = url.split("https://api.github.com", 1)[1].split("?", 1)[0]
        if path.startswith("/repos/alice/r/actions/workflows/"):
            action = path.split("/workflows/wx-cs-", 1)[1].split(".yml", 1)[0]
            return _resp(json.dumps({"workflow_runs": self.runs.get(action, [])}).encode())
        if path.startswith("/repos/alice/r/actions/runs/") and path.endswith("/jobs"):
            rid = int(path.split("/runs/", 1)[1].split("/", 1)[0])
            return _resp(json.dumps({"jobs": [{"id": rid}]}).encode())
        if path.startswith("/repos/alice/r/actions/jobs/") and path.endswith("/logs"):
            jid = int(path.split("/jobs/", 1)[1].split("/", 1)[0])
            if self.logs.get(jid) is None:
                raise urllib.error.HTTPError(url, 410, "Gone", {}, io.BytesIO(b"{}"))
            raise urllib.error.HTTPError(url, 302, "Found", {"Location": f"https://blob.example/log/{jid}"},
                                         io.BytesIO(b""))
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    # -- the fake S3 --
    def reader(self, wf):
        world = self

        class Reader:
            def head(self, key):
                world.s3_calls.append(("head", key))
                if key not in world.s3:
                    return None
                return {"size": len(world.s3[key][0]), "last_modified": world.s3[key][1]}

            def text(self, key):
                world.s3_calls.append(("text", key))
                return world.s3[key][0] if key in world.s3 else None

            def get_bytes(self, key):
                world.s3_calls.append(("get", key))
                return world.s3[key][0].encode()
            bucket = "b"
        return Reader()


def _resp(body):
    class R(io.BytesIO):
        status = 200

        def __init__(self):
            super().__init__(body)
            self.headers = {}
    return R()


def at(iso, clock=None, plus=0):
    """A wall clock for the grace period: `iso` + `plus` seconds + the fake clock's time."""
    base = M._iso(iso)
    return lambda: base + timedelta(seconds=plus + (clock.t if clock else 0))


class Clock:
    def __init__(self, on_sleep=None):
        self.t = 0.0
        self.on_sleep = on_sleep

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s
        if self.on_sleep:
            self.on_sleep()


def poll(capsys, run, world, *extra, clock=None, now=None):
    clock = clock or Clock()
    code = M.main(["poll", "--run", str(run), *extra], opener=world, s3_factory=world.reader,
                  clock=clock, sleep=clock.sleep, now=now)
    return code, json.loads(capsys.readouterr().out)


def state(run):
    return json.loads((run / "state.json").read_text())


def report(out):
    """The full report: deploy/monitor-report.md (recorded) or `report` (report only)."""
    return Path(out["details"]).read_text() if out.get("details") else out["report"]


# ── the weather workflow's units and outputs ──

def test_units_and_declared_outputs_follow_the_workflow():
    assert [u["label"] for u in M.units(WF)] == LABELS
    assert [o["key"] for o in M.declared_outputs(WF)] == OUTPUTS
    assert M.invocation_folder(WF, INV, TS) == FOLDER


# ── results ──

def test_success_is_recorded_and_goes_to_finalize(run, env, capsys):
    world = World().finish_all()
    code, out = poll(capsys, run, world)
    assert code == 0 and out["result"] == "success" and out["stage"] == "finalize", out
    assert out["actions_ok"] and out["outputs_ok"] and not out["unconfirmed"]
    s = state(run)
    assert s["stage"] == "finalize" and s["monitor"]["result"] == "success" and s["monitor"]["success"]
    assert s["history"][-1]["decision"] == "succeeded"
    md = report(out)
    assert "**Actions:** all succeeded" in md and "**Outputs:** 5/5 declared files present and fresh" in md
    assert "plot) is not judged" in md
    for key in OUTPUTS:
        assert f"`{key}` — present" in md
    saved = sorted(p.name for p in (run / "deploy" / "monitor").iterdir())
    assert saved == ["download.txt", "plot.txt", "process.1.txt", "process.2.txt", "process.3.txt"]


def test_a_failed_action_stops_with_a_redacted_excerpt(run, env, capsys):
    world = World()
    world.add_run("download")
    world.s3["wx-cs/station.csv"] = ("data", AFTER)
    world.add_run("process")
    world.add_run("process", conclusion="failure")
    world.add_run("process")
    world.s3[f"{FOLDER}/process(2).txt"] = (
        f"[1.0] [INFO] start\n[2.5] [ERROR] boom; token was {FAKE['GH_PAT']}\n", AFTER)
    code, out = poll(capsys, run, world)
    assert code == 1 and out["result"] == "failure"
    assert out["failed"]["label"] == "process(2)" and "Actions run concluded failure" in out["failed"]["why"]
    assert "[ERROR] in its FaaSr log" in out["failed"]["why"]
    text = json.dumps(out) + (run / "state.json").read_text()
    text += "".join(p.read_text() for p in (run / "deploy" / "monitor").iterdir())
    assert FAKE["GH_PAT"] not in text and "[REDACTED:" in report(out)
    s = state(run)
    assert s["stage"] == "stopped" and s["stopped_from"] == "monitor"
    assert out["resume_command"] == "/curate-skills:curate --resume r1"
    assert "**Failed: `process(2)`**" in report(out)


@pytest.mark.parametrize("case,state_word", [("missing", "missing"), ("stale", "stale")])
def test_a_missing_or_stale_output_fails_the_outputs_check_only(run, env, capsys, case, state_word):
    world = World().finish_all()
    if case == "missing":
        del world.s3["wx-cs/cmp_3.csv"]
    else:  # left over from an earlier invocation
        world.s3["wx-cs/cmp_3.csv"] = ("old", BEFORE)
    code, out = poll(capsys, run, world)
    assert code == 1 and out["result"] == "failure"
    assert out["actions_ok"] is True and out["outputs_ok"] is False  # reported separately
    assert f"wx-cs/cmp_3.csv ({state_word})" in out["failed"]["why"]
    assert f"`wx-cs/cmp_3.csv` — {state_word}" in report(out) and "4/5 declared files" in report(out)


def test_all_outputs_stale_from_an_earlier_invocation(run, env, capsys):
    world = World().finish_all()
    for key in OUTPUTS:
        world.s3[key] = ("old", BEFORE)
    code, out = poll(capsys, run, world)
    assert out["result"] == "failure" and "0/5 declared files" in report(out)


def test_timeout_then_resume_continues_the_same_invocation(run, env, capsys):
    import spec as S
    world = World().finish_all()
    plot = world.runs.pop("plot")  # plot has not run yet
    code, out = poll(capsys, run, world, "--minutes", "0.5", "--interval", "10")
    assert code == 1 and out["result"] == "timeout" and out["minutes_used"] >= 0.5 - 1e-6
    assert out["resume_command"] == "/curate-skills:curate --resume r1"
    s = state(run)
    assert s["stage"] == "stopped" and s["stopped_from"] == "monitor" and s["history"][-1]["decision"] == "timed-out"
    assert "not finished (timed out)" in report(out)
    # resume: begin re-enters monitor (never deploy), and the same invocation is polled again
    S.main(["begin", "--run", str(run), "--stage", "monitor"])
    assert json.loads(capsys.readouterr().out)["mode"] == "resume" and state(run)["stage"] == "monitor"
    world.runs["plot"] = plot
    code, out = poll(capsys, run, world)
    assert code == 0 and out["result"] == "success" and state(run)["stage"] == "finalize"
    assert state(run)["monitor"]["settings"]["minutes"] == 0.5  # pinned; the budget was fresh
    assert all(r.get_method() == "GET" for r in world.requests)


def test_resume_after_success_is_a_no_op(run, env, capsys):
    world = World().finish_all()
    poll(capsys, run, world)
    s = state(run)
    s["stage"] = "monitor"  # e.g. curate-monitor run again
    (run / "state.json").write_text(json.dumps(s))
    calls = len(world.requests), len(world.s3_calls)
    code, out = poll(capsys, run, world)
    assert code == 0 and out["no_op"] and out["result"] == "success" and state(run)["stage"] == "finalize"
    assert (len(world.requests), len(world.s3_calls)) == calls


# ── matching runs to the InvocationID ──

def test_job_logs_are_fetched_once_and_runs_of_other_invocations_ignored(run, env, capsys):
    world = World().finish_all()
    plot = world.runs.pop("plot")
    other = world.add_run("download", log="OVERWRITTEN: {\"InvocationID\": \"2026-10-01-00-00-00\"}\n")
    clock = Clock()
    code, out = poll(capsys, run, world, "--minutes", "0.5", "--interval", "10", clock=clock)
    assert out["result"] == "timeout"
    first = dict(world.blob_fetches)
    assert all(n == 1 for n in first.values()) and other in first
    S = __import__("spec")
    S.main(["begin", "--run", str(run), "--stage", "monitor"])
    capsys.readouterr()
    world.runs["plot"] = plot
    code, out = poll(capsys, run, world)
    assert out["result"] == "success"
    assert all(n == 1 for n in world.blob_fetches.values())  # cached: no log fetched twice
    assert str(other) in state(run)["monitor"]["matches"] and state(run)["monitor"]["matches"][str(other)]["match"] == "other"


def test_job_log_text_is_never_printed_or_saved(run, env, capsys):
    world = World().finish_all()
    leaky = (f"OVERWRITTEN: {{\"InvocationID\": \"{INV}\", \"ComputeServers\": {{\"GH\": {{\"Token\": "
             f"\"{FAKE['GH_PAT']}\"}}}}, \"PayloadMarker\": \"JOBLOG-ONLY\"}}\nTraceback: crashed\n")
    world.runs["plot"] = []
    world.add_run("plot", conclusion="failure", log=leaky)
    code, out = poll(capsys, run, world)
    assert out["result"] == "failure" and out["failed"]["excerpt_from"] == "job log (redacted)"
    everything = json.dumps(out) + (run / "state.json").read_text()
    everything += "".join(p.read_text() for p in (run / "deploy").rglob("*") if p.is_file())
    assert FAKE["GH_PAT"] not in everything
    saved = "".join(p.read_text() for p in (run / "deploy" / "monitor").iterdir())
    assert "JOBLOG-ONLY" not in saved  # only FaaSr's S3 logs are saved, never job logs


def test_runs_without_a_log_are_success_unconfirmed_and_need_the_users_acceptance(run, env, capsys):
    import catalog
    world = World().finish_all()
    world.logs[world.runs["process"][1]["id"]] = None  # its job log is gone
    code, out = poll(capsys, run, world)
    assert code == 0 and out["result"] == "success_unconfirmed" and out["unconfirmed"]
    assert "success, unconfirmed match" in report(out) and "time only" in report(out)
    assert "Gate 5 asks you to accept" in report(out)
    s = state(run)
    assert s["stage"] == "finalize" and s["monitor"]["accepted"] is None
    assert "accept" in catalog.deployed_run_problem(s)
    code = M.main(["accept-unconfirmed", "--run", str(run)])
    assert code == 1 and json.loads(capsys.readouterr().out)["needs_confirmation"]
    assert M.main(["accept-unconfirmed", "--run", str(run), "--confirmed"]) == 0
    capsys.readouterr()
    s = state(run)
    assert s["monitor"]["accepted"]["by"] == "user" and s["history"][-1]["decision"] == "accepted-unconfirmed-match"
    assert catalog.deployed_run_problem(s) is None


def test_catalog_refuses_to_save_a_deployed_run_before_success(run, env, capsys):
    import catalog
    assert "not been confirmed to work" in catalog.deployed_run_problem(state(run))
    code = catalog.main(["save-workflow", "--run", str(run), "--catalog-dir", str(run / "cat")])
    assert code != 0 and "not been confirmed to work" in capsys.readouterr().out


# ── read-only on finished runs ──

def test_report_only_on_a_done_run_writes_nothing(run, env, capsys):
    s = state(run)
    s["stage"] = "done"
    (run / "state.json").write_text(json.dumps(s))
    before = {p: p.read_bytes() for p in run.rglob("*") if p.is_file()}
    world = World().finish_all()
    code, out = poll(capsys, run, world, "--report-only")
    assert code == 0 and out["result"] == "success" and out["mode"] == "report only" and "stage" not in out
    assert {p: p.read_bytes() for p in run.rglob("*") if p.is_file()} == before
    # also without the flag: a run past monitor is never recorded
    code, out = poll(capsys, run, world)
    assert out["mode"] == "report only" and {p: p.read_bytes() for p in run.rglob("*") if p.is_file()} == before


def test_no_token_or_no_deploy_record(run, env, capsys, monkeypatch):
    monkeypatch.delenv("GH_PAT")
    code, out = poll(capsys, run, World())
    assert code == 2 and "no GitHub token" in out["error"]
    monkeypatch.setenv("GH_PAT", FAKE["GH_PAT"])
    s = state(run)
    s["deploy"].pop("invocation_id")
    (run / "state.json").write_text(json.dumps(s))
    code, out = poll(capsys, run, World())
    assert code == 2 and "no deployed invocation" in out["error"]


# ── pipeline ──

def test_finalize_after_a_deploy_needs_monitor_success(run, env, capsys):
    import spec as S
    s = state(run)
    s["validation"] = {"ok": True, "spec_hash": S.spec_hash(s["spec"])}
    problems = S.require_problems(s, "finalize")
    assert any("not been confirmed to work" in p for p in problems)
    s["monitor"] = {"result": "success"}
    assert not any("confirmed to work" in p for p in S.require_problems(s, "finalize"))
    assert S.require_problems({**s, "deploy": {}}, "monitor")[0].startswith("nothing to monitor")


# ── live-test support: interim polls, --minutes on resume, downloading the outputs ──

def test_interim_polls_show_earlier_outputs_as_stale_until_rewritten(run, env, capsys):
    world = World().finish_all()
    plot = world.runs.pop("plot")
    for key in OUTPUTS:  # the previous invocation's files are still there
        world.s3[key] = ("old", BEFORE)

    def the_run_finishes():
        world.runs["plot"] = plot
        for key in OUTPUTS:
            world.s3[key] = ("new", AFTER)
    clock = Clock(on_sleep=the_run_finishes)
    code, out = poll(capsys, run, world, clock=clock, now=at(AFTER, clock))
    assert code == 0 and out["result"] == "success"
    first, last = state(run)["monitor"]["poll_log"]
    assert first["runs_concluded"] == 4 and first["outputs"] == {"stale": 5}
    assert last["runs_concluded"] == 5 and last["outputs"] == {"present": 5}
    assert "| 1 | 0 | 4/5 | 0 / 5 / 0 |" in report(out) and "| 2 | 10 | 5/5 | 5 / 0 / 0 |" in report(out)


def test_minutes_given_on_resume_replace_the_pinned_value(run, env, capsys):
    import spec as S
    world = World().finish_all()
    plot = world.runs.pop("plot")
    code, out = poll(capsys, run, world, "--minutes", "0.5")
    assert out["result"] == "timeout" and state(run)["monitor"]["settings"]["minutes"] == 0.5
    S.main(["begin", "--run", str(run), "--stage", "monitor"])
    capsys.readouterr()
    world.runs["plot"] = plot
    code, out = poll(capsys, run, world, "--minutes", "10")
    assert out["result"] == "success" and out["settings_changed"] == {"minutes": [0.5, 10.0]}
    settings = state(run)["monitor"]["settings"]
    assert settings["minutes"] == 10.0 and settings["minutes_source"] == "flag (changed on resume)"


def test_download_outputs_copies_files_with_a_manifest_and_leaves_the_run_alone(run, env, capsys, tmp_path):
    world = World().finish_all()
    world.s3["wx-cs/fig.png"] = ("png-bytes", "2026-11-02T01:41:00+00:00")
    before = {p: p.read_bytes() for p in run.rglob("*") if p.is_file()}
    dest = tmp_path / "backup" / "r1-outputs"
    code = M.main(["download-outputs", "--run", str(run), "--to", str(dest)], s3_factory=world.reader)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and [f["key"] for f in out["files"]] == OUTPUTS
    fig = next(f for f in out["files"] if f["key"] == "wx-cs/fig.png")
    assert fig["size"] == 9 and fig["last_modified"] == "2026-11-02T01:41:00+00:00" and len(fig["sha256"]) == 64
    assert (dest / "wx-cs" / "fig.png").read_bytes() == b"png-bytes"
    assert json.loads((dest / "manifest.json").read_text())["invocation_id"] == INV
    assert {p: p.read_bytes() for p in run.rglob("*") if p.is_file()} == before
    assert {c[0] for c in world.s3_calls} <= {"head", "get"}  # read-only


def test_download_outputs_refuses_unsafe_targets_and_skips_secrets(run, env, capsys, tmp_path):
    world = World().finish_all()
    code = M.main(["download-outputs", "--run", str(run), "--to", str(run / "x")], s3_factory=world.reader)
    assert code == 2 and "inside the run folder" in json.loads(capsys.readouterr().out)["error"]
    full = tmp_path / "full"
    full.mkdir()
    (full / "keep.txt").write_text("mine")
    code = M.main(["download-outputs", "--run", str(run), "--to", str(full)], s3_factory=world.reader)
    assert code == 2 and "not empty" in json.loads(capsys.readouterr().out)["error"]
    world.s3["wx-cs/station.csv"] = (f"key,{FAKE['S3_SecretKey']}\n", AFTER)
    dest = tmp_path / "out"
    code = M.main(["download-outputs", "--run", str(run), "--to", str(dest)], s3_factory=world.reader)
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["skipped"][0]["key"] == "wx-cs/station.csv" and "S3_SecretKey" in out["skipped"][0]["reason"]
    assert not (dest / "wx-cs" / "station.csv").exists() and FAKE["S3_SecretKey"] not in json.dumps(out)


@pytest.mark.parametrize("config,expected", [
    ("${user_config.monitor_minutes}", (10.0, "default")),  # --plugin-dir: left unsubstituted
    ("", (10.0, "default")),
    ("3", (3.0, "plugin setting")),
])
def test_config_minutes_from_skill(monkeypatch, config, expected):
    monkeypatch.delenv("CURATE_MONITOR_MINUTES", raising=False)
    assert M._setting(None, "CURATE_MONITOR_MINUTES", config, M.DEFAULT_MINUTES, float) == expected
    monkeypatch.setenv("CURATE_MONITOR_MINUTES", "7")
    assert M._setting(None, "CURATE_MONITOR_MINUTES", config, M.DEFAULT_MINUTES, float) == (7.0, "CURATE_MONITOR_MINUTES")
    assert M._setting("2", "CURATE_MONITOR_MINUTES", config, M.DEFAULT_MINUTES, float) == (2.0, "flag")


# ── fan-in runs, the grace period, re-check, the display (run 20261104-210753) ──

def _all_but_plot_output(world):
    world.s3["wx-cs/fig.png"] = ("last week's figure", BEFORE)  # an earlier invocation's file
    return world


def test_an_extra_fan_in_run_in_progress_is_waited_for(run, env, capsys):
    # plot is triggered once per predecessor: one run concluded (it found its inputs incomplete and
    # exited), another is still running. All 5 expected runs are green and fig.png is stale; the
    # grace period is long over. Nothing may be judged yet.
    world = _all_but_plot_output(World().finish_all())
    late = world.add_run("plot", status="in_progress", conclusion=None)
    code, out = poll(capsys, run, world, "--chunk-seconds", "20")
    assert code == 0 and out["result"] == "pending" and state(run)["stage"] == "monitor"
    assert out["lines"] == ["✓ download succeeded", "✓ process succeeded (3/3)", "… plot running"]
    assert not out["failed"]
    # the second run finishes and writes the figure
    world.runs["plot"][-1].update(status="completed", conclusion="success")
    world.s3["wx-cs/fig.png"] = ("new figure", AFTER)
    code, out = poll(capsys, run, world)
    assert code == 0 and out["result"] == "success" and state(run)["stage"] == "finalize"
    assert out["lines"] == ["✓ plot succeeded", "✓ workflow succeeded"]
    assert str(late) in state(run)["monitor"]["matches"]


def test_an_unconfirmed_run_in_progress_of_any_action_blocks_the_verdict(run, env, capsys):
    world = World().finish_all()
    world.add_run("download", status="queued", conclusion=None)
    code, out = poll(capsys, run, world, "--chunk-seconds", "10")
    assert out["result"] == "pending" and "✓ workflow succeeded" not in out["lines"]


def test_a_late_output_within_the_grace_period_succeeds(run, env, capsys):
    world = _all_but_plot_output(World().finish_all())

    def written_later():
        if clock.t >= 30:
            world.s3["wx-cs/fig.png"] = ("new figure", AFTER)
    clock = Clock(on_sleep=written_later)
    code, out = poll(capsys, run, world, clock=clock, now=at(AFTER, clock, plus=5))
    assert code == 0 and out["result"] == "success"
    assert out["lines"] == ["✓ download succeeded", "✓ process succeeded (3/3)", "… plot running",
                            "✓ plot succeeded", "✓ workflow succeeded"]


def test_an_output_still_missing_after_the_grace_period_fails_in_plain_words(run, env, capsys):
    world = _all_but_plot_output(World().finish_all())
    clock = Clock()
    code, out = poll(capsys, run, world, clock=clock, now=at(AFTER, clock, plus=5))
    assert code == 1 and out["result"] == "failure" and clock.t >= M.GRACE_SECONDS - 5
    assert out["lines"][-2:] == ["✗ plot succeeded but did not write fig.png",
                                 "✗ workflow failed: plot: succeeded but did not write fig.png"]
    assert "wx-cs/fig.png (stale)" in out["failed"]["why"]  # the details: in the report and state
    assert "`wx-cs/fig.png` — stale" in report(out)


def test_recheck_after_a_failure_polls_the_same_invocation_and_can_replace_the_verdict(run, env, capsys):
    import spec as S
    world = _all_but_plot_output(World().finish_all())
    late = world.add_run("plot", status="in_progress", conclusion=None)
    world.runs["plot"].remove(next(r for r in world.runs["plot"] if r["id"] == late))
    code, out = poll(capsys, run, world)  # the old false FAILURE: the late run not listed yet
    assert out["result"] == "failure" and state(run)["stage"] == "stopped"
    assert out["recheck_command"] == "/curate-skills:curate-monitor --run r1"
    # curate-monitor --run r1: begin resumes monitor; poll re-checks the same invocation
    S.main(["begin", "--run", str(run), "--stage", "monitor"])
    assert json.loads(capsys.readouterr().out)["mode"] == "resume"
    world.add_run("plot")
    world.s3["wx-cs/fig.png"] = ("new figure", AFTER)
    requests_before = len(world.requests)
    code, out = poll(capsys, run, world)
    assert code == 0 and out["result"] == "success" and out["rechecked"] == "failure"
    assert out["lines"] == ["✓ download succeeded", "✓ process succeeded (3/3)", "✓ plot succeeded",
                            "✓ workflow succeeded"]  # every line again, then the new verdict
    s = state(run)
    assert s["stage"] == "finalize" and s["monitor"]["result"] == "success"
    assert [h["decision"] for h in s["history"] if h["gate"] == "monitor"] == ["failed", "succeeded"]
    assert "re-checked after FAILURE" in s["history"][-1]["text"]
    assert s["deploy"]["invocation_id"] == INV and all(r.get_method() == "GET" for r in world.requests[requests_before:])


def test_lines_show_each_change_once_and_one_call_returns_after_its_chunk(run, env, capsys):
    world = World()
    world.add_run("download")
    world.s3["wx-cs/station.csv"] = ("data", AFTER)
    world.add_run("process")
    world.add_run("process", status="in_progress", conclusion=None)
    world.add_run("process", status="in_progress", conclusion=None)
    clock = Clock()
    code, out = poll(capsys, run, world, clock=clock)
    assert out["result"] == "pending" and 60 <= clock.t <= 70 and out["minutes_used"] <= 70 / 60
    assert out["lines"] == ["✓ download succeeded", "… process running (1/3)"]
    code, out = poll(capsys, run, world, "--chunk-seconds", "10")
    assert out["result"] == "pending" and out["lines"] == []  # nothing changed: nothing shown
    for r in world.runs["process"]:
        r.update(status="completed", conclusion="success")
    world.add_run("plot")
    for key in OUTPUTS:
        world.s3[key] = ("data", AFTER)
    code, out = poll(capsys, run, world)
    assert out["lines"] == ["✓ process succeeded (3/3)", "✓ plot succeeded", "✓ workflow succeeded"]
    shown = "\n".join(out["lines"]) + out["summary"]
    for word in ("present", "stale", "missing", "| Poll |"):
        assert word not in shown
    assert out["summary"].startswith("✓ download succeeded\n✓ process succeeded (3/3)\n✓ plot succeeded\n"
                                     "✓ workflow succeeded")


def test_a_failure_line_names_the_function_and_the_reason_from_its_log(run, env, capsys):
    world = World()
    world.add_run("download")
    world.s3["wx-cs/station.csv"] = ("data", AFTER)
    for c in ("success", "failure", "success"):
        world.add_run("process", conclusion=c)
    world.s3[f"{FOLDER}/process(2).txt"] = ("[1.0] [INFO] start\n[2.5] [ERROR] KeyError: 'TMAX' not in columns\n", AFTER)
    code, out = poll(capsys, run, world)
    assert out["lines"] == ["✓ download succeeded", "✗ process failed: KeyError: 'TMAX' not in columns",
                            "✗ workflow failed: process(2): KeyError: 'TMAX' not in columns"]
    # without a FaaSr [ERROR] line: the job log's last error-like line, else the conclusion
    assert M.one_line_reason({"conclusion": "failure", "excerpt": [
        "2026-11-05T05:10:01.1Z ##[error]Process completed with exit code 1.", "2026-11-05T05:10:02Z Cleaning up"]},
        None) == "Process completed with exit code 1."
    assert M.one_line_reason({"conclusion": "timed_out", "excerpt": []}, None) == "its Actions run ended with timed_out"


def test_deploy_progress_line():
    assert M.D.progress_line(["publish"]) == "published"
    assert M.D.progress_line(["invoke", "publish", "upload", "register"]) == \
        "published · uploaded · registered · invoked"
