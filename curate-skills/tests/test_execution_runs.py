"""Runs (workflows/<workflow-id>/runs/<run-id>/, step 5): each has its own state.json.

- Gate 4 (or "Start this run?") of a session opens one; its deploy, Pegasus and monitor
  records live there, and the session sees them through runstate.load.
- `--start-at deploy --workflow <id>` (or `--from-run` with any run of the version) opens one that
  stands alone: no new session; it deploys, monitors and ends at Gate 5 without saving.
- Publishing writes approved.json (the approved spec) into the version, outside its hash; a version
  published before it is built with the spec of the run in this project that approved it.
"""
import json
import shutil
from pathlib import Path

import pytest

import catalog
import execution as X
import ids
import runstate
import spec as S
from conftest import call, ok
from test_start import source_run, start, state  # noqa: F401  (fixture)

REAL = Path(__file__).parent / "fixtures" / "real"


def at_gate4(run):
    s = state(run)
    s["stage"] = "gate4"
    runstate.save(run, s)


def raw(folder) -> dict:
    return json.loads((Path(folder) / "state.json").read_text())


def faasr_env(monkeypatch):
    from test_deploy import FAKE
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FAASR_GH_USERNAME", "alice")
    monkeypatch.setenv("FAASR_ACTION_REPO", "faasr-actions")
    for k in ("FAASR_S3_ENDPOINT", "FAASR_S3_BUCKET", "FAASR_S3_REGION"):
        monkeypatch.setenv(k, {"FAASR_S3_BUCKET": "b"}.get(k, "x"))
    for k in ("FAASR_ACTION_BRANCH", "GITHUB_REPOSITORY", "GITHUB_REF_NAME", "CUSTOM_CONTAINER"):
        monkeypatch.delenv(k, raising=False)


def files(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


# ── approved.json ──

def test_publish_writes_approved_json_outside_the_version_hash(source_run):
    s = state(source_run)
    vdir = Path(s["version_dir"])
    a = ids.read_approved(vdir)
    assert a["spec"] == s["spec"] and a["workflow_id"] == s["workflow_id"] and a["session"] == s["run_id"]
    assert a["content_hash"] == s["workflow_content_hash"]
    assert not (vdir / "approved.json").stat().st_mode & 0o222  # read-only, like the version
    assert ids.version_hash(vdir) == s["version_hash"] == ids.tree_hash(vdir, skip=("runs", "approved.json"))
    # the version hash is what it was before approved.json existed
    assert s["version_hash"] != ids.tree_hash(vdir, skip=("runs",))


# ── a session's run ──

def test_gate4_opens_an_execution_run_that_holds_the_records(source_run, capsys, monkeypatch):
    monkeypatch.chdir(state(source_run)["project_dir"])  # commands run in the project (<workflow-id>/<run-id>)
    at_gate4(source_run)
    ok(S, ["approve", "--session", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "yes"], capsys)
    gen = raw(source_run)
    ex_dir = Path(gen["execution"]["dir"])
    ex = raw(ex_dir)
    assert ex_dir.parent.parent.name == gen["workflow_id"] and ex_dir.name == gen["execution"]["run_id"]
    assert ex["kind"] == "execution" and ex["session"] == {"session_id": gen["run_id"], "dir": str(source_run)}
    assert ex["reference"] == gen["execution"]["reference"] == f"{gen['workflow_id']}/{ex_dir.name}"
    assert "deploy" not in gen and "monitor" not in gen and ex["deploy"] is None and "pegasus" not in ex
    assert (ex_dir / "bundle" / "workflow.json").is_file() and (ex_dir / "manifest.json").is_file()
    # the session sees them, and writes them there
    runstate.update(source_run, lambda st: st.update(monitor={"result": "pending"}))
    assert raw(ex_dir)["monitor"] == {"result": "pending"} and "monitor" not in raw(source_run)
    assert state(source_run)["monitor"] == {"result": "pending"}
    # the run's reference and id lead to the session that uses it
    project = Path(gen["project_dir"])
    for ref in (ex["reference"], ex["run_id"], str(ex_dir)):
        assert runstate.resolve_run(ref, project) == source_run.resolve()
    # FaaSr deploy steps write their log and the deploy copy into the run
    import deploy as D
    from test_deploy import FakeGitHub, FakeS3
    faasr_env(monkeypatch)
    ok(S, ["begin", "--session", str(source_run), "--stage", "deploy"], capsys)
    fake = FakeGitHub(writable=True)
    for argv in (["publish", "--run", ex["reference"], "--confirmed"], ["upload", "--run", ex["reference"], "--confirmed"]):
        code = D.main(argv, opener=fake, s3_factory=FakeS3())
        assert code == 0, capsys.readouterr().out
    capsys.readouterr()
    assert (ex_dir / "deploy-log.jsonl").is_file() and (ex_dir / "deploy" / "workflow.json").is_file()
    assert not (source_run / "deploy").exists()
    assert raw(ex_dir)["deploy"]["steps"]["upload"]["ok"] and "deploy" not in raw(source_run)


def test_a_rewind_detaches_the_execution_run_and_keeps_its_records(source_run, capsys):
    at_gate4(source_run)
    ok(S, ["approve", "--session", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus"], capsys)
    ex_dir = Path(raw(source_run)["execution"]["dir"])
    runstate.update(source_run, lambda st: st.update(pegasus={"steps": {"launch": {"ok": True}}}))
    ok(S, ["begin", "--session", str(source_run), "--stage", "emit", "--rewind", "--confirmed"], capsys)
    assert state(source_run)["execution"] is None and state(source_run)["pegasus"] is None
    assert raw(ex_dir)["pegasus"] == {"steps": {"launch": {"ok": True}}}  # what ran stays recorded
    assert runstate.resolve_run(raw(ex_dir)["reference"], raw(source_run)["project_dir"]) == ex_dir.resolve()


def test_resuming_a_generation_run_still_works(source_run, capsys):
    """A stopped session resumes where it stopped, before and after Gate 4 opened an execution."""
    at_gate4(source_run)
    ok(S, ["set-stage", "--session", str(source_run), "--stage", "stopped"], capsys)
    out = ok(S, ["next", "--session", str(source_run)], capsys)
    rid = state(source_run)["run_id"]
    assert out["resume_command"] == f"/curate-skills:curate --resume {rid}"
    assert ok(S, ["begin", "--session", str(source_run), "--stage", "emit"], capsys)["mode"] == "resume"
    ok(S, ["approve", "--session", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "yes"], capsys)
    ok(S, ["set-stage", "--session", str(source_run), "--stage", "stopped"], capsys)
    out = ok(S, ["status", "--session", str(source_run)], capsys)
    assert out["stopped_from"] == "deploy" and out["resume_command"].endswith(f"--resume {rid}")
    out = ok(S, ["begin", "--session", str(source_run), "--stage", "deploy"], capsys)
    assert out["mode"] == "resume" and out["stage"] == "deploy"
    assert state(source_run)["execution"]["target"] == "faasr"


# ── a standalone run: --start-at deploy ──

def test_start_at_deploy_with_workflow_opens_a_standalone_execution_run(source_run, capsys, monkeypatch):
    monkeypatch.chdir(state(source_run)["project_dir"])
    src = state(source_run)
    project, wid = Path(src["project_dir"]), src["workflow_id"]
    gens = sorted((project / ".curate" / "sessions").iterdir())
    code, out = start(capsys, source_run, "deploy", "--workflow", wid)
    assert code == 0, out
    d = Path(out["run"])
    assert d.parent == Path(src["version_dir"]) / "runs" and out["reference"] == f"{wid}/{d.name}"
    assert sorted((project / ".curate" / "sessions").iterdir()) == gens  # no session
    s = raw(d)
    assert s["kind"] == "execution" and s["session"] is None and s["stage"] == "deploy"
    assert s["spec"] == src["spec"] and s["spec_from"] == f"{wid}/approved.json"
    assert out["uses"].endswith(f"the spec from {wid}/approved.json")
    # commands name it <workflow-id>/<run-id>; any of its names finds it
    assert out["next_skill"] == "curate-skills:curate-deploy"
    for ref in (out["reference"], d.name, str(d)):
        assert runstate.resolve_run(ref, project) == d.resolve()
    runs = {r["run_id"]: r for r in ok(S, ["runs", "--project-dir", str(project)], capsys)["runs"]}
    assert runs[d.name]["reference"] == out["reference"] and runs[d.name]["session"] is None
    # "Start this run?": its own folder gets the bundle; nothing else is opened
    code, res = call(S, ["begin", "--run", out["reference"], "--stage", "deploy"], capsys)
    assert code == 1 and "spec.py confirm-start" in res["error"]
    ok(S, ["confirm-start", "--run", out["reference"]], capsys)
    s = state(d)
    assert s["execution"]["dir"] == str(d.resolve()) and (d / "bundle" / "workflow.json").is_file()
    assert sorted(p.name for p in d.parent.iterdir()) == [d.name]
    ids.check_manifest(d, s["version_hash"])
    assert ok(S, ["begin", "--run", out["reference"], "--stage", "deploy"], capsys)["mode"] == "fresh"
    nxt = ok(S, ["next", "--run", out["reference"]], capsys)
    assert nxt["command"] == f"/curate-skills:curate-deploy --run {out['reference']}"
    # it only deploys: earlier stages and gates are refused
    code, res = call(S, ["begin", "--run", out["reference"], "--stage", "emit", "--rewind", "--confirmed"], capsys)
    assert code == 1 and "run" in res["error"]
    code, res = call(S, ["approve", "--run", out["reference"], "--gate", "4"], capsys)
    assert code == 2 and "no Gates 1-4" in res["error"]


@pytest.mark.parametrize("how", ["session", "run id", "workflow-id/run-id", "old copied run",
                                 "old execution folder"])
def test_from_run_resolves_any_run_of_the_version(source_run, capsys, how):
    """The run that approved Gate 4, a run (new or from before runs had their own
    state), and a deploy-only run made the old way all resolve to their version."""
    src = state(source_run)
    project, wid = Path(src["project_dir"]), src["workflow_id"]
    if how == "session":
        ref = src["run_id"]
    elif how in ("run id", "workflow-id/run-id"):
        first = Path(start(capsys, source_run, "deploy", "--workflow", wid)[1]["run"])
        ref = first.name if how == "run id" else f"{wid}/{first.name}"
    elif how == "old copied run":  # what --start-at deploy --from-run made before: a session
        old = project / ".curate" / "runs" / "20261124-204358-92b6"  # where sessions were then
        old.mkdir(parents=True)
        (old / "state.json").write_text(json.dumps({
            "schema_version": 2, "run_id": old.name, "stage": "done", "workflow_id": wid,
            "version_hash": src["version_hash"], "spec": src["spec"],
            "history": [{"gate": 4, "decision": "copied", "from_run": src["run_id"], "deploy": "yes"}]}))
        ref = old.name
    else:  # a folder with manifest.json and bundle/, no state.json
        old = Path(src["version_dir"]) / "runs" / "20261124-210945-a884"
        (old / "bundle").mkdir(parents=True)
        (old / "manifest.json").write_text("{}")
        ref = f"{wid}/{old.name}"
    code, out = start(capsys, source_run, "deploy", "--from-run", ref)
    assert code == 0, out
    s = raw(out["run"])
    assert s["workflow_id"] == wid and s["kind"] == "execution" and s["spec"] == src["spec"]


def test_a_version_without_approved_json_uses_the_run_that_approved_it(source_run, capsys):
    """Versions published before approved.json (the two real ones): the session in this project
    whose Gate 4 published the version gives the spec; the version is not changed."""
    src = state(source_run)
    vdir = Path(src["version_dir"])
    (vdir / "approved.json").unlink()  # the folder itself stays writable (only files are read-only)
    code, out = start(capsys, source_run, "deploy", "--workflow", src["workflow_id"])
    assert code == 0, out
    s = raw(out["run"])
    assert s["spec"] == src["spec"] and s["spec_from"].startswith(f"session {src['run_id']}, history/")
    assert s["spec_from"].endswith("-gate4-approve.json") and ids.version_hash(vdir) == src["version_hash"]
    # nowhere to find it: refused, saying what to do
    shutil.rmtree(source_run / "history")
    code, out = start(capsys, source_run, "deploy", "--workflow", src["workflow_id"])
    assert code == 2 and "--start-at deploy --from-run" in out["error"]


def test_start_at_deploy_refuses_an_unknown_version(source_run, capsys):
    code, out = start(capsys, source_run, "deploy", "--workflow", "nothing-v9")
    assert code == 2 and "not in this project" in out["error"]
    code, out = start(capsys, source_run, "deploy", "--workflow", "not a workflow id")
    assert code == 2 and "is not a workflow ID" in out["error"]


# ── Gate 5: both paths ──

def test_gate5_of_a_standalone_execution_run_reports_and_saves_nothing(source_run, capsys, monkeypatch):
    monkeypatch.chdir(state(source_run)["project_dir"])
    src = state(source_run)
    out = start(capsys, source_run, "deploy", "--workflow", src["workflow_id"])[1]
    ref = out["reference"]
    ok(S, ["confirm-start", "--run", ref], capsys)
    d = Path(out["run"])
    code, res = call(S, ["require", "--run", ref, "--stage", "finalize"], capsys)
    assert "was not monitored" in res["problems"][0]
    runstate.update(d, lambda st: st.update(stage="finalize", monitor={"result": "success", "summary": "✓ workflow succeeded"},
                                            deploy={"deploy_name": "x-1", "invocation_id": "i-1"}))
    assert ok(S, ["require", "--run", ref, "--stage", "finalize"], capsys)["problems"] == []
    g5 = ok(S, ["show", "--run", ref, "--gate", "5"], capsys)
    assert g5["execution_run"] and g5["options"] == [] and g5["question"] is None
    md = g5["markdown"]
    assert f"Run — `{ref}`" in md and "✓ workflow succeeded" in md and "deployment: `x-1`" in md
    assert "Nothing is saved to the catalog" in md and (d / "review" / "gate5.md").is_file()
    # the catalog refuses to save from it
    code = catalog.main(["save-workflow", "--run", ref, "--dry-run"])
    assert code != 0 and "run" in capsys.readouterr().out
    ok(S, ["set-stage", "--run", ref, "--stage", "done"], capsys)
    assert ok(S, ["next", "--run", ref], capsys)["next_skill"] is None


def test_gate5_of_a_generation_run_reads_its_execution_run(source_run, capsys):
    at_gate4(source_run)
    ok(S, ["approve", "--session", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "yes"], capsys)
    ex_dir = Path(raw(source_run)["execution"]["dir"])
    runstate.update(ex_dir, lambda st: st.update(monitor={"result": "success", "summary": "✓ from the run"},
                                                 deploy={"steps": {"invoke": {"ok": True}}, "invocation_id": "i"}))
    runstate.update(source_run, lambda st: st.update(stage="finalize"))
    assert ok(S, ["require", "--session", str(source_run), "--stage", "finalize"], capsys)["problems"] == []
    g5 = ok(S, ["show", "--session", str(source_run), "--gate", "5"], capsys)
    assert not g5.get("execution_run") and g5["options"] == S.GATE_OPTIONS[5]
    assert "✓ from the run" in g5["markdown"] and "### What would be saved" in g5["markdown"]
    assert catalog.deployed_run_problem(state(source_run)) is None


# ── the two real versions, where they are ──

def real_project(tmp_path, which: str) -> tuple[Path, str]:
    """A project as on the machine that ran phase 7, in the layout of then: the real version as
    published (no approved.json) in .curate/workflows/, and the session that approved it (its state
    and Gate 4 snapshot) in .curate/runs/. Nothing is moved."""
    d = REAL / which
    ident = json.loads((d / "identity.json").read_text())
    spec = json.loads((d / "spec.json").read_text())
    project = tmp_path / "project"
    wid = ident["workflow_id"]
    shutil.copytree(d / "version", ids.legacy_workflows_root(project) / wid)
    gen = project / ".curate" / "runs" / "20261124-214915-3b2f"
    (gen / "history").mkdir(parents=True)
    st = {"schema_version": 2, "run_id": gen.name, "stage": "done", "project_dir": str(project),
          "workflow_id": wid, "version_hash": ident["version_hash"], "spec": spec,
          "history": [{"gate": 4, "decision": "approve", "deploy": "pegasus"}]}
    (gen / "state.json").write_text(json.dumps(st))
    (gen / "history" / "004-gate4-approve.json").write_text(json.dumps(st))
    return project, wid


@pytest.mark.parametrize("which", ["weather", "mapreduce"])
def test_the_real_versions_redeploy_by_workflow_from_their_old_location(tmp_path, capsys, which):
    project, wid = real_project(tmp_path, which)
    old = ids.legacy_workflows_root(project) / wid
    assert ids.version_dir(project, wid) == old and not (project / "workflows").exists()
    ident = json.loads((REAL / which / "identity.json").read_text())
    code, out = call(S, ["start", "--at", "deploy", "--workflow", wid, "--project-dir", str(project)], capsys)
    assert code == 0, out
    assert out["uses"].endswith("the spec from session 20261124-214915-3b2f, history/004-gate4-approve.json")
    assert Path(out["run"]).parent == old / "runs"  # the version's own runs/, where it is
    ok(S, ["confirm-start", "--run", out["run"]], capsys)
    s = state(out["run"])
    assert files(Path(s["execution"]["bundle_dir"])) == files(REAL / which / "faasr-bundle")
    assert ids.version_hash(ids.version_dir(project, wid)) == ident["version_hash"]
    # Pegasus: a second run of the same version builds the jobs that ran
    code, out = call(S, ["start", "--at", "deploy", "--workflow", wid, "--project-dir", str(project)], capsys)
    s = state(out["run"])
    built = X.pegasus_built(s, out["run"], ident["pegasus"]["name"], ident["pegasus"]["image"])
    import yaml
    assert built["jobs"] == yaml.safe_load((REAL / which / "pegasus-ir.yaml").read_text())["nodes"]
    # the session in .curate/runs/ that approved it, and a run of it, also name the version
    for ref in ("20261124-214915-3b2f", s["reference"]):
        code, out = call(S, ["start", "--at", "deploy", "--from-run", ref, "--project-dir", str(project)], capsys)
        assert code == 0 and raw(out["run"])["workflow_id"] == wid, out
    listed = ok(S, ["sessions", "--project-dir", str(project)], capsys)
    assert [x["session_id"] for x in listed["sessions"]] == ["20261124-214915-3b2f"] and len(listed["runs"]) == 4


# ── the layout: workflows/ in the project, sessions in .curate/ ──

def test_versions_and_runs_are_in_the_project_and_sessions_in_curate(source_run, capsys):
    src = state(source_run)
    project, wid = Path(src["project_dir"]), src["workflow_id"]
    assert source_run.parent == project / ".curate" / "sessions"
    assert Path(src["version_dir"]) == project / "workflows" / wid
    assert (project / "workflows" / ids.WORKFLOWS_MARKER).is_file()
    assert {p.name for p in (project / ".curate").iterdir()} <= {"sessions", "sketches", "mga.lock.json", "mga_guard.log"}
    out = start(capsys, source_run, "deploy", "--workflow", wid)[1]
    assert Path(out["run"]).parent == project / "workflows" / wid / "runs"


def test_a_workflows_folder_that_is_not_curates_is_refused(make_run, capsys, monkeypatch, tmp_path):
    """The project already has its own workflows/ (no marker): Gate 4 publishes nothing and says why;
    the folder is left as it was."""
    import emit
    monkeypatch.setattr(emit, "faasr_py_validate", lambda wf, timeout=300: None)
    run = make_run()
    project = Path(state(run)["project_dir"])
    theirs = project / "workflows"
    theirs.mkdir()
    (theirs / "ci.yml").write_text("on: push\n")
    from test_start import FIX
    work = run / "work"
    (work / "sk.json").write_text((FIX / "skeleton.json").read_text())
    ok(S, ["skeleton", "--session", str(run), "--json", str(work / "sk.json")], capsys)
    ok(S, ["approve", "--session", str(run), "--gate", "1"], capsys)
    ok(S, ["apply-candidates", "--session", str(run), "--json", str(FIX / "candidates.json")], capsys)
    ok(S, ["merge-resolved", "--session", str(run), "--json", str(FIX / "resolved.json")], capsys)
    ok(S, ["approve", "--session", str(run), "--gate", "2"], capsys)
    s = state(run)
    code = {n["name"]: n["code"] for n in json.loads((FIX / "spec-with-code.json").read_text())["nodes"]}
    for n in s["spec"]["nodes"]:
        n["code"] = code[n["name"]]
    s["mga"] = {n["name"]: {"status": "passed", "attempts": 1} for n in s["spec"]["nodes"]}
    s["stage"] = "gate3"
    runstate.save(run, s)
    ok(S, ["approve", "--session", str(run), "--gate", "3"], capsys)
    emit.main(["bundle", "--session", str(run)])
    capsys.readouterr()
    code, out = call(S, ["approve", "--session", str(run), "--gate", "4", "--on-duplicate", "keep"], capsys)
    assert code == 2 and f"{theirs} exists and is not CURATE's" in out["error"]
    assert ids.WORKFLOWS_MARKER in out["error"]
    assert sorted(p.name for p in theirs.iterdir()) == ["ci.yml"] and state(run).get("workflow_id") is None
    assert state(run)["stage"] == "gate4"


# ── approved.json is checked when it is read ──

def _edit_approved(vdir: Path, change) -> None:
    f = vdir / "approved.json"
    a = json.loads(f.read_text())
    change(a)
    f.chmod(0o644)
    f.write_text(json.dumps(a))


@pytest.mark.parametrize("edit,why", [
    (lambda a: a["spec"].update(folder="elsewhere"), "its spec is not the one it recorded"),
    (lambda a: (a["spec"]["nodes"][0].update(code=a["spec"]["nodes"][0]["code"] + "\n# edited\n"),
                a.update(spec_sha256=X.spec_sha256(a["spec"]))), "its spec does not build this version"),
    (lambda a: a.update(content_hash="sha256:" + "0" * 64), "its content hash is not the version's"),
    (lambda a: a.update(workflow_id="other-v1"), "it names 'other-v1'"),
], ids=["spec edited", "spec and its hash edited", "content hash edited", "another version"])
def test_an_edited_approved_json_is_refused(source_run, capsys, edit, why):
    src = state(source_run)
    vdir = Path(src["version_dir"])
    _edit_approved(vdir, edit)
    assert ids.version_hash(vdir) == src["version_hash"]  # outside the version hash: only this check sees it
    code, out = start(capsys, source_run, "deploy", "--workflow", src["workflow_id"])
    assert code == 2 and "approved.json does not match the version" in out["error"] and why in out["error"]
    with pytest.raises(X.ExecutionError):
        X.approved_spec(src["project_dir"], src["workflow_id"], vdir)


def test_an_unreadable_approved_json_is_refused(source_run, capsys):
    src = state(source_run)
    f = Path(src["version_dir"]) / "approved.json"
    f.chmod(0o644)
    f.write_text("{not json")
    code, out = start(capsys, source_run, "deploy", "--workflow", src["workflow_id"])
    assert code == 2 and "cannot be read" in out["error"]


# ── locks: a session and its run written at the same time ──

WRITER = """
import json, sys
sys.path[:0] = [sys.argv[1] + "/scripts", sys.argv[1] + "/curate_ir"]
import runstate, spec as S
kind, run, n = sys.argv[2], sys.argv[3], int(sys.argv[4])
for i in range(n):
    if kind == "stage":      # a generation stage: spec.py loads and saves the whole state
        S.main(["set-cap", "--session", run, "--max", str(100 + i)])
    elif kind == "gen":      # a stage's own change to the session
        runstate.update(run, lambda s: s.update(iteration=s["iteration"] + 1))
    elif kind == "monitor":  # monitor.py / pegasus_monitor.py: the monitor record, through the session
        runstate.update(run, lambda s: s["monitor"].update(polls=s["monitor"]["polls"] + 1))
    else:                    # a writer of the run itself
        runstate.update(run, lambda s: s["monitor"].update(direct=s["monitor"]["direct"] + 1))
"""


def test_concurrent_writes_lose_nothing_and_do_not_deadlock(source_run, capsys):
    """Four processes at once: a spec.py stage saving the whole generation state, a stage changing it,
    the monitor writing its record through the session, and a writer of the run
    directly. Every update survives; all finish (no deadlock)."""
    import subprocess
    import sys
    at_gate4(source_run)
    ok(S, ["approve", "--session", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "yes"], capsys)
    ex_dir = Path(raw(source_run)["execution"]["dir"])
    runstate.update(source_run, lambda s: s.update(iteration=0, monitor={"polls": 0, "direct": 0}))
    plugin = str(Path(__file__).resolve().parents[1])
    n = 40
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, plugin, kind, str(run), str(n)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
             for kind, run in (("stage", source_run), ("gen", source_run), ("monitor", source_run),
                               ("direct", ex_dir))]
    for p in procs:
        _, err = p.communicate(timeout=120)  # a deadlock fails here
        assert p.returncode == 0, err[-2000:]
    s = state(source_run)
    assert s["iteration"] == n and s["monitor"] == {"polls": n, "direct": n}
    assert s["max_iterations"] == 100 + n - 1
    assert raw(ex_dir)["monitor"] == {"polls": n, "direct": n} and "monitor" not in raw(source_run)


# ── resuming sessions: new layout and made before runs had their own state ──

def test_resuming_a_generation_run_mid_pipeline(source_run, capsys, monkeypatch):
    """Stopped at generate (before Gate 4: no run yet), it resumes at generate."""
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "1")
    code, out = start(capsys, source_run, "generate", "--from-run", state(source_run)["run_id"],
                      "--existing-code", "keep")
    assert code == 0, out
    run = Path(out["run"])
    assert ok(S, ["begin", "--session", str(run), "--stage", "generate"], capsys)["mode"] == "fresh"
    ok(S, ["set-stage", "--session", str(run), "--stage", "stopped"], capsys)
    assert ok(S, ["next", "--session", str(run)], capsys)["resume_stage_command"] == \
        f"/curate-skills:curate-generate --session {run.name}"
    out = ok(S, ["begin", "--session", str(run), "--stage", "generate"], capsys)
    assert out["mode"] == "resume" and out["stage"] == "generate" and state(run).get("execution") is None


def test_resuming_a_run_made_before_execution_runs_had_their_own_state(source_run, capsys):
    """A session whose execution folder has no state.json (made before this change) keeps
    every record in its own state.json: resuming reads them there and writes them there."""
    at_gate4(source_run)
    ok(S, ["approve", "--session", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus"], capsys)
    s = state(source_run)  # turn it into the old layout: the records in the session's own file
    ex_dir = Path(s["execution"]["dir"])
    (ex_dir / "state.json").unlink()
    s["pegasus"] = {"steps": {"prepare": {"ok": True}}, "name": "old"}
    (source_run / "state.json").write_text(json.dumps(s))
    assert runstate.is_legacy(source_run) and runstate.files_dir(source_run) == source_run
    ok(S, ["set-stage", "--session", str(source_run), "--stage", "stopped"], capsys)
    out = ok(S, ["begin", "--session", str(source_run), "--stage", "deploy"], capsys)
    assert out["mode"] == "resume" and out["stage"] == "deploy"
    runstate.update(source_run, lambda st: st["pegasus"].update(name="still here"))
    assert raw(source_run)["pegasus"]["name"] == "still here" and not (ex_dir / "state.json").exists()
    import pegasus_deploy as PD
    assert PD.log_path(source_run) == source_run / "pegasus" / "deploy-log.jsonl"
