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


# ── the options name the target and the image ──

DEFAULT_IMG = "docker:///python:3.12-slim"
DIFFERENT = "Approve & run on Pegasus with a different image…"


def test_options_name_target_and_image(source_run, capsys, monkeypatch):
    at_gate4(source_run)
    out = ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)
    assert out["options"] == ["Approve & deploy to FaaSr (cloud)", f"Approve & run on Pegasus (this machine · {DEFAULT_IMG})",
                              DIFFERENT, "Approve, no deploy"]  # Stop is typed, as the question says
    assert out["question"].startswith('Approve this workflow, and where should it run? (type "stop" to stop here)')
    monkeypatch.setenv("CURATE_PEGASUS_HOST", "local")
    assert ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)["options"][1] == \
        f"Approve & run on Pegasus (this machine · {DEFAULT_IMG})"
    monkeypatch.setenv("CURATE_PEGASUS_HOST", "maxwell")
    monkeypatch.setenv("CURATE_PEGASUS_IMAGE", "docker:///me/img:2")
    out = ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)
    assert out["options"][1] == "Approve & run on Pegasus (maxwell · docker:///me/img:2)"


def test_option_label_from_the_settings_file(source_run, capsys, monkeypatch, tmp_path):
    f = tmp_path / ".env"
    f.write_text("CURATE_PEGASUS_HOST=lab-server\n")
    monkeypatch.setattr(envfile, "default_path", lambda: f)
    at_gate4(source_run)
    assert ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)["options"][1] == \
        f"Approve & run on Pegasus (lab-server · {DEFAULT_IMG})"
    import os
    assert "CURATE_PEGASUS_HOST" not in os.environ  # read, never exported


# ── the Gate 4 display: a short summary per target, no file lists ──

def where_section(md):
    return md.split("### Where it can run")[1].split("\n\n— end of Gate 4")[0]


def test_gate4_summary_local(source_run, capsys):
    at_gate4(source_run)
    s = state(source_run)
    md = ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)["markdown"]
    sec = where_section(md)
    rid = s["run_id"]
    assert sec.index("**FaaSr (cloud)**") < sec.index("**Pegasus**")
    assert (f"**Pegasus** on this machine: `mapreduce-word-count-{rid[4:8]}-{rid[9:13]}`, 11 jobs, image "
            f"`{DEFAULT_IMG}` (the built-in default), runtime docker, 5 deliverables") in sec
    assert "- input files you must provide: none" in sec
    # no file lists at the gate, for either target
    for listing in ("### Deploy would write", "### Pegasus run would write", "Files in the bundle:", "- Writes:",
                    "ir.yaml", "functions/", "word_count_{rank5}"):
        assert listing not in md, listing
    full = (source_run / "review" / "gate4.md").read_text()  # the review file keeps them
    assert "### Pegasus run would write" in full and "ir.yaml" in full and "### Deploy would write" in full


def test_gate4_summary_remote_and_setting_image(source_run, capsys, monkeypatch):
    monkeypatch.setenv("CURATE_PEGASUS_HOST", "maxwell")
    monkeypatch.setenv("CURATE_PEGASUS_IMAGE", "docker:///me/img:2")
    at_gate4(source_run)
    md = ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)["markdown"]
    assert "**Pegasus** on `maxwell` (over ssh): " in md
    assert "image `docker:///me/img:2` (CURATE_PEGASUS_IMAGE)" in md


def with_conditional_edge(run):
    s = state(run)
    s["spec"]["edges"][0]["condition"] = "True"
    s["validation"]["spec_hash"] = S.spec_hash(s["spec"])
    s["stage"] = "gate4"
    from conftest import redraft
    redraft(run, s)  # emit again after the spec changed: the draft carries curate:run_if
    save(run, s)
    return s["spec"]["edges"][0]


def run_if_refusal(e) -> str:
    return (f"step {e['to_node']} runs only if {e['from_node']} returns True (curate:run_if): Pegasus runs a fixed "
            "DAG in which every step runs, so it cannot honour the condition. Deploy to FaaSr instead.")


def test_gate4_says_why_it_cannot_run_and_drops_the_image_option(source_run, capsys):
    e = with_conditional_edge(source_run)
    out = ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)
    sec = where_section(out["markdown"])
    assert "**Pegasus** on this machine: **cannot run on Pegasus:**" in sec
    assert f"- {run_if_refusal(e)}" in sec
    # not available: no "different image" option; Stop is a button again
    assert out["options"] == ["Approve & deploy to FaaSr (cloud)", f"Approve & run on Pegasus (this machine · {DEFAULT_IMG})",
                              "Approve, no deploy", "Stop here"]


# ── approval and the stage checks ──

def test_approve_pegasus_goes_to_deploy(source_run, capsys):
    at_gate4(source_run)
    out = ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus"], capsys)
    assert out["stage"] == "deploy" and out["deploy"] == "pegasus" and out["next_skill"] == "curate-skills:curate-deploy"
    s = state(source_run)
    assert s["deploy_choice"] == "pegasus" and s["pegasus"] is None and s["deploy"] is None
    assert s["bundle_hash"] == D.tree_hash(Path(s["bundle_dir"])) and s["history"][-1]["deploy"] == "pegasus"
    assert ok(S, ["require", "--run", str(source_run), "--stage", "deploy"], capsys)["problems"] == []


def test_approve_pegasus_refused_when_it_cannot_run(source_run, capsys):
    e = with_conditional_edge(source_run)
    code, out = call(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus"], capsys)
    assert code == 2 and out["error"].startswith(f"this workflow cannot run on Pegasus: {run_if_refusal(e)}")
    assert state(source_run)["stage"] == "gate4"


def test_monitor_and_finalize_checks_for_pegasus(source_run, capsys):
    at_gate4(source_run)
    ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus"], capsys)
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
    ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus"], capsys)
    s = state(source_run)
    assert D.gate4_problem(s) == D.PEGASUS_REFUSAL
    out = D.deploy_step(str(source_run), "publish", "-x", None, None, "none")
    assert out["ok"] is False and out["error"] == D.PEGASUS_REFUSAL
    assert D.reuse(str(source_run), "-x", None, None, "none")["error"] == D.PEGASUS_REFUSAL
    assert not (source_run / "deploy").exists()


# ── a FaaSr run behaves as before ──

def test_faasr_run_unchanged(source_run, capsys):
    at_gate4(source_run)
    ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "yes"], capsys)
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
    ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus"], capsys)
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


# ── doctor --pegasus: the auto-mode rules ──

def test_doctor_pegasus_auto_mode_rules(tmp_path):
    import doctor
    root = str(Path(doctor.__file__).resolve().parent.parent)
    claude, project = tmp_path / "claude", tmp_path / "proj"
    claude.mkdir()
    project.mkdir()
    r = doctor.Report()
    doctor.check_auto_mode_rules(r, root, claude, project, doctor.PEGASUS_AUTO_MODE_SCRIPTS, "pegasus")
    (row,) = r.rows
    assert row["group"] == "pegasus" and row["status"] == "warn"
    assert "the Pegasus run will be blocked after the first step" in row["message"]
    assert f"scripts/pegasus_deploy.py *)" in row["message"] and "allow_deploy.py --pegasus" in row["message"]
    (claude / "settings.json").write_text(json.dumps({"permissions": {"allow": [
        doctor.auto_mode_rule(root, s) for s in doctor.PEGASUS_AUTO_MODE_SCRIPTS]}}))
    r = doctor.Report()
    doctor.check_auto_mode_rules(r, root, claude, project, doctor.PEGASUS_AUTO_MODE_SCRIPTS, "pegasus")
    assert r.rows[0]["status"] == "ok"
    r = doctor.Report()  # FaaSr's check: same group, same text as before
    doctor.check_auto_mode_rules(r, root, claude, project)
    assert r.rows[0]["group"] == "deploy" and "In auto mode, deploy will be blocked" in r.rows[0]["message"]
    assert r.rows[0]["message"].endswith("allow_deploy.py — or use manual mode for deploy.")


# ── --start-at deploy --from-run: FaaSr or Pegasus ──

def test_start_at_deploy_offers_both_targets_and_runs_on_pegasus(source_run, capsys, monkeypatch):
    from test_start import start
    monkeypatch.setenv("CURATE_PEGASUS_HOST", "maxwell")
    code, out = start(capsys, source_run, "deploy", "--from-run", state(source_run)["run_id"])
    assert code == 0, out
    assert out["start_options"] == ["Yes, deploy to FaaSr (cloud)", f"Yes, run on Pegasus (maxwell · {DEFAULT_IMG})",
                                    "Yes, run on Pegasus with a different image…", "Stop"]
    new = Path(out["run"])
    res = ok(S, ["confirm-start", "--run", str(new), "--deploy", "pegasus"], capsys)
    assert res["recorded"] == "deploy_choice" and res["deploy_choice"] == "pegasus"
    s = state(new)
    assert s["deploy_choice"] == "pegasus" and s["pegasus"] is None and s["stage"] == "deploy"
    assert s["history"][-1]["deploy"] == "pegasus" and "Yes (run on Pegasus)" in s["history"][-1]["text"]
    assert ok(S, ["require", "--run", str(new), "--stage", "deploy"], capsys)["problems"] == []
    assert ok(S, ["confirm-start", "--run", str(new)], capsys)["deploy_choice"] == "pegasus"  # once only
    import pegasus_deploy as PD
    monkeypatch.setattr(PD, "RUN", lambda cmd, timeout=600: (0, "5.1.3", "") if cmd[-1] == "pegasus-version"
                        else (1, "", ""))
    pf = PD.preflight(str(new))
    assert pf["ok"] and pf["name"].startswith("mapreduce-word-count-") and pf["name"] != ""
    assert pf["name"].endswith(f"{s['run_id'][4:8]}-{s['run_id'][9:13]}")  # the new run's own name


def test_start_at_deploy_pegasus_refused_when_it_cannot_run(source_run, capsys):
    from test_start import start
    e = with_conditional_edge(source_run)
    ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep"], capsys)
    code, out = start(capsys, source_run, "deploy", "--from-run", state(source_run)["run_id"])
    assert code == 0, out
    new = Path(out["run"])
    code, res = call(S, ["confirm-start", "--run", str(new), "--deploy", "pegasus"], capsys)
    assert code == 2 and res["error"].startswith(f"this workflow cannot run on Pegasus: {run_if_refusal(e)}")
    assert state(new)["deploy_choice"] is None
    assert ok(S, ["confirm-start", "--run", str(new)], capsys)["deploy_choice"] == "yes"  # FaaSr still possible


def test_start_options_only_for_deploy_and_deploy_flag_only_there(source_run, capsys):
    from test_start import start
    code, out = start(capsys, source_run, "finalize", "--from-run", state(source_run)["run_id"])
    assert code == 0 and "start_options" not in out
    code, res = call(S, ["confirm-start", "--run", out["run"], "--deploy", "pegasus"], capsys)
    assert code == 2 and "only applies to a run started with --start-at deploy" in res["error"]
