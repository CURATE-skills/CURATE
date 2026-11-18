"""Gate 4's Pegasus target (phase 3): the option label, the read-only preview, approval, the
stage checks and the catalog check; and that a FaaSr run (deploy_choice "yes") behaves as
before. Nothing here runs Pegasus or contacts anything."""
import json
from pathlib import Path

import pytest

import catalog
import deploy as D
import envfile
import spec as S
from conftest import call, ok
from test_start import source_run, state  # noqa: F401  (fixture)


def at_gate4(run):
    s = state(run)
    s["stage"] = "gate4"
    (run / "state.json").write_text(json.dumps(s))


def save(run, s):
    (run / "state.json").write_text(json.dumps(s))


# ── the option label names the target ──

def test_option_label_names_this_machine_or_the_host(source_run, capsys, monkeypatch):
    at_gate4(source_run)
    out = ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)
    assert out["options"] == ["Approve & deploy to FaaSr (cloud)", "Approve & run on Pegasus (this machine)",
                              "Approve, no deploy", "Stop here"]
    assert out["question"].startswith("Approve this bundle, and run it on FaaSr or Pegasus?")
    monkeypatch.setenv("CURATE_PEGASUS_HOST", "local")
    assert ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)["options"][1] == \
        "Approve & run on Pegasus (this machine)"
    monkeypatch.setenv("CURATE_PEGASUS_HOST", "maxwell")
    out = ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)
    assert out["options"][1] == "Approve & run on Pegasus (maxwell)"
    assert "If you choose **Approve & run on Pegasus (maxwell)**" in out["markdown"]


def test_option_label_from_the_settings_file(source_run, capsys, monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text("CURATE_PEGASUS_HOST=lab-server\n")
    monkeypatch.setattr(envfile, "default_path", lambda: f)
    at_gate4(source_run)
    assert ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)["options"][1] == \
        "Approve & run on Pegasus (lab-server)"
    import os
    assert "CURATE_PEGASUS_HOST" not in os.environ  # read, never exported


# ── the preview ──

def test_gate4_preview_local(source_run, capsys):
    at_gate4(source_run)
    s = state(source_run)
    md = ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)["markdown"]
    sec = md.split("### Pegasus run would write")[1].split("Files in the bundle:")[0]
    rid = s["run_id"]
    local = f"{s['run_dir']}/pegasus"
    assert "If you choose **Approve & run on Pegasus (this machine)** (nothing is written now" in sec
    assert f"- Runs on: this machine (Pegasus + HTCondor), in `{local}/`" in sec
    assert f"- Workflow: `mapreduce-word-count-{rid[4:8]}-{rid[9:13]}`, 11 jobs, image " \
           "`docker:///python:3.12-slim`, runtime docker" in sec
    assert f"- Writes: `{local}/ir.yaml`, `{local}/functions/` (5 files), `{local}/inputs/`" in sec
    assert "- Input files you must provide: none" in sec
    assert "- Delivered: `results/word_count_{rank5}.json`" in sec
    # the FaaSr section is still there, before it
    assert md.index("### Deploy would write") < md.index("### Pegasus run would write")


def test_gate4_preview_remote(source_run, capsys, monkeypatch):
    monkeypatch.setenv("CURATE_PEGASUS_HOST", "maxwell")
    monkeypatch.setenv("CURATE_PEGASUS_IMAGE", "docker:///me/img:2")
    at_gate4(source_run)
    s = state(source_run)
    md = ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)["markdown"]
    assert f"- Runs on: `maxwell` over ssh; `{s['run_dir']}/pegasus/` is copied to " \
           f"`maxwell:~/curate-runs/{s['run_id']}/` and run there" in md
    assert "image `docker:///me/img:2`" in md


def with_conditional_edge(run):
    s = state(run)
    s["spec"]["edges"][0]["condition"] = "True"
    s["validation"]["spec_hash"] = S.spec_hash(s["spec"])
    s["stage"] = "gate4"
    save(run, s)
    return s["spec"]["edges"][0]


def test_gate4_preview_says_why_it_cannot_run(source_run, capsys):
    e = with_conditional_edge(source_run)
    md = ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)["markdown"]
    sec = md.split("### Pegasus run would write")[1]
    assert "- **Cannot run on Pegasus:**" in sec
    assert f"  - conditional edge {e['from_node']} → {e['to_node']} (taken only when {e['from_node']} returns True)" in sec


# ── approval and the stage checks ──

def test_approve_pegasus_goes_to_deploy(source_run, capsys):
    at_gate4(source_run)
    out = ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--deploy", "pegasus"], capsys)
    assert out["stage"] == "deploy" and out["deploy"] == "pegasus" and out["next_skill"] == "curate-skills:curate-deploy"
    s = state(source_run)
    assert s["deploy_choice"] == "pegasus" and s["pegasus"] is None and s["deploy"] is None
    assert s["bundle_hash"] == D.tree_hash(Path(s["bundle_dir"])) and s["history"][-1]["deploy"] == "pegasus"
    assert ok(S, ["require", "--run", str(source_run), "--stage", "deploy"], capsys)["problems"] == []


def test_approve_pegasus_refused_when_it_cannot_run(source_run, capsys):
    with_conditional_edge(source_run)
    code, out = call(S, ["approve", "--run", str(source_run), "--gate", "4", "--deploy", "pegasus"], capsys)
    assert code == 2 and out["error"].startswith("this workflow cannot run on Pegasus: conditional edge")
    assert state(source_run)["stage"] == "gate4"


def test_monitor_and_finalize_checks_for_pegasus(source_run, capsys):
    at_gate4(source_run)
    ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--deploy", "pegasus"], capsys)
    code, out = call(S, ["require", "--run", str(source_run), "--stage", "monitor"], capsys)
    assert code == 1 and out["problems"][0].startswith("nothing to monitor: the Pegasus run was not started")
    s = state(source_run)
    s["pegasus"] = {"steps": {"prepare": {"ok": True}, "launch": {"ok": True}}}
    save(source_run, s)
    assert ok(S, ["require", "--run", str(source_run), "--stage", "monitor"], capsys)["problems"] == []
    for result, problem in (("failure", True), ("timeout", True), ("success_unconfirmed", True), ("success", False)):
        s["monitor"] = {"result": result}
        save(source_run, s)
        code, out = call(S, ["require", "--run", str(source_run), "--stage", "finalize"], capsys)
        assert bool(out["problems"]) is problem, result
        assert (catalog.deployed_run_problem(s) is not None) is problem, result
        if problem:
            assert out["problems"][0].startswith("the Pegasus run has not been confirmed to work")


def test_faasr_deploy_refuses_a_pegasus_run(source_run, capsys):
    at_gate4(source_run)
    ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--deploy", "pegasus"], capsys)
    s = state(source_run)
    assert D.gate4_problem(s) == D.PEGASUS_REFUSAL
    out = D.deploy_step(str(source_run), "publish", "-x", None, None, "none")
    assert out["ok"] is False and out["error"] == D.PEGASUS_REFUSAL
    assert D.reuse(str(source_run), "-x", None, None, "none")["error"] == D.PEGASUS_REFUSAL
    assert not (source_run / "deploy").exists()


# ── a FaaSr run behaves as before ──

def test_faasr_run_unchanged(source_run, capsys):
    at_gate4(source_run)
    ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--deploy", "yes"], capsys)
    s = state(source_run)
    assert s["deploy_choice"] == "yes" and s["stage"] == "deploy" and "pegasus" not in s
    assert ok(S, ["require", "--run", str(source_run), "--stage", "deploy"], capsys)["problems"] == []
    code, out = call(S, ["require", "--run", str(source_run), "--stage", "monitor"], capsys)
    assert out["problems"] == [f"nothing to monitor: the workflow was not started — run "
                               f"/curate-skills:curate-deploy --run {s['run_id']}"]
    s.update(deploy={"steps": {"invoke": {"ok": True}}, "invocation_id": "i1"}, monitor={"result": "failure"})
    save(source_run, s)
    assert ok(S, ["require", "--run", str(source_run), "--stage", "monitor"], capsys)["problems"] == []
    code, out = call(S, ["require", "--run", str(source_run), "--stage", "finalize"], capsys)
    assert out["problems"][0].startswith("the deployed run has not been confirmed to work (monitor result failure)")
    assert catalog.deployed_run_problem(s).startswith("the deployed run has not been confirmed")
    s["monitor"] = {"result": "success_unconfirmed"}
    assert catalog.deployed_run_problem(s).startswith("the deployed run's Actions runs were matched by time only")
    s["monitor"]["accepted"] = True
    assert catalog.deployed_run_problem(s) is None
    assert D.gate4_problem(state(source_run)) is None
    # Entering deploy and rewinding never adds the Pegasus field to a FaaSr run.
    ok(S, ["begin", "--run", str(source_run), "--stage", "deploy"], capsys)
    assert "pegasus" not in state(source_run)
    ok(S, ["begin", "--run", str(source_run), "--stage", "emit", "--rewind", "--confirmed"], capsys)
    s = state(source_run)
    assert "pegasus" not in s and s["deploy_choice"] is None and s["deploy"] is None


def test_no_deploy_unchanged(source_run, capsys):
    s = state(source_run)  # the fixture approved Gate 4 without --deploy
    assert s["deploy_choice"] == "no" and s["stage"] == "finalize" and "pegasus" not in s
    assert ok(S, ["require", "--run", str(source_run), "--stage", "finalize"], capsys)["problems"] == []
    assert catalog.deployed_run_problem(s) is None


def test_pegasus_field_cleared_on_rewind(source_run, capsys):
    at_gate4(source_run)
    ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--deploy", "pegasus"], capsys)
    s = state(source_run)
    s["pegasus"] = {"steps": {"launch": {"ok": True}}}
    save(source_run, s)
    out = ok(S, ["begin", "--run", str(source_run), "--stage", "emit", "--rewind", "--confirmed"], capsys)
    assert "pegasus" in out["cleared"]
    assert state(source_run)["pegasus"] is None and state(source_run)["deploy_choice"] is None


def test_settings_file_loading_keeps_faasr_names_only_by_default(tmp_path):
    f = tmp_path / ".env"
    f.write_text("FAASR_S3_BUCKET=bkt\nCURATE_PEGASUS_HOST=maxwell\nCURATE_PEGASUS_IMAGE=docker:///x:1\n")
    env = {}
    info = envfile.load(env, f)  # what deploy.py and monitor.py do: deploy names only, as before
    assert env == {"FAASR_S3_BUCKET": "bkt"} and set(info["sources"]) == {"FAASR_S3_BUCKET"}
    env = {}
    envfile.load(env, f, names=envfile.PEGASUS_NAMES)
    assert env == {"CURATE_PEGASUS_HOST": "maxwell", "CURATE_PEGASUS_IMAGE": "docker:///x:1"}
    import pegasus_ir
    assert pegasus_ir.settings({}) == {"host": None, "runtime": "docker", "image": "docker:///python:3.12-slim"}
