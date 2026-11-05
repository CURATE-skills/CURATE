import hashlib
import json
from pathlib import Path

import pytest

import emit
import spec as S
from conftest import call, ok

FIX = Path(__file__).parent / "fixtures" / "e2-stages"


def state(run):
    return json.loads((Path(run) / "state.json").read_text())


def tree_hash(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(folder)).encode() + p.read_bytes())
    return h.hexdigest()


@pytest.fixture
def source_run(make_run, capsys, monkeypatch, tmp_path):
    """A run taken through Gates 1-4 with the scripts alone (no model)."""
    monkeypatch.setattr(emit, "faasr_py_validate", lambda wf, timeout=300: None)
    run = make_run()
    work = run / "work"
    (work / "sk.json").write_text((FIX / "skeleton.json").read_text())
    ok(S, ["skeleton", "--run", str(run), "--json", str(work / "sk.json")], capsys)
    ok(S, ["approve", "--run", str(run), "--gate", "1"], capsys)
    ok(S, ["apply-candidates", "--run", str(run), "--json", str(FIX / "candidates.json")], capsys)
    ok(S, ["merge-resolved", "--run", str(run), "--json", str(FIX / "resolved.json")], capsys)
    ok(S, ["approve", "--run", str(run), "--gate", "2"], capsys)
    s = state(run)
    code = {n["name"]: n["code"] for n in json.loads((FIX / "spec-with-code.json").read_text())["nodes"]}
    for n in s["spec"]["nodes"]:
        n["code"] = code[n["name"]]
    s["mga"] = {n["name"]: {"status": "passed", "attempts": 1} for n in s["spec"]["nodes"]}
    s["stage"] = "gate3"
    (run / "state.json").write_text(json.dumps(s))
    ok(S, ["approve", "--run", str(run), "--gate", "3"], capsys)
    emit.main(["bundle", "--run", str(run), "--out", str(tmp_path / "faasr_output")])
    capsys.readouterr()
    ok(S, ["approve", "--run", str(run), "--gate", "4"], capsys)
    return run


def start(capsys, run, at, *extra):
    project = Path(state(run)["project_dir"])
    argv = ["start", "--at", at, "--project-dir", str(project), *map(str, extra)]
    return call(S, argv, capsys)


# ── from a run ──

@pytest.mark.parametrize("at,gate,stage", [("reuse", 1, "reuse"), ("resolve", 2, "resolve"),
                                           ("generate", 2, "generate"), ("emit", 3, "emit"),
                                           ("finalize", 4, "finalize")])
def test_from_run_copies_the_approved_results_into_a_new_run(source_run, capsys, monkeypatch,
                                                             at, gate, stage):
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "1")  # generate requires it
    before = tree_hash(source_run)
    rid = state(source_run)["run_id"]
    extra = ("--existing-code", "ignore") if at == "generate" else ()  # Gate 3 was approved: a choice
    code, out = start(capsys, source_run, at, "--from-run", rid, *extra)
    assert code == 0, out
    new = Path(out["run"])
    assert new != source_run and tree_hash(source_run) == before          # source untouched
    s = state(new)
    assert s["stage"] == stage and out["next_skill"].endswith(S.PIPELINE[S.STAGE_NAMES.index(at)][2])
    rec = s["history"][-1]
    assert rec["decision"] == "copied" and rec["gate"] == gate and rec["from_run"] == rid
    assert rec["snapshot"].endswith(f"gate{gate}-approve.json") and len(rec["sha256"]) == 64
    snap = json.loads((source_run / "history" / rec["snapshot"]).read_text())
    assert s["spec"] == snap["spec"] and s["user_request"] == snap["user_request"]
    assert s["prior_spec"] is None  # the source's old code is not an approved result
    has_code = at in ("emit", "finalize")
    assert all(bool(n["code"]) is has_code for n in s["spec"]["nodes"])
    # the new run passes its own stage's precondition
    assert ok(S, ["require", "--run", str(new), "--stage", at], capsys)["problems"] == []


def test_dry_run_prints_what_will_be_used_and_creates_nothing(source_run, capsys):
    runs = sorted((source_run.parent).iterdir())
    code, out = start(capsys, source_run, "generate", "--from-run", state(source_run)["run_id"],
                      "--existing-code", "ignore", "--dry-run")
    assert code == 0 and out["dry_run"] and sorted(source_run.parent.iterdir()) == runs
    assert out["uses"].startswith("history/") and "Gate 2" in out["uses"]
    assert out["redoes"] == ["generate", "emit", "deploy (if chosen at Gate 4)", "monitor (if deployed)", "finalize"]
    assert {f["has_code"] for f in out["functions"]} == {False}
    assert set(out["settings"]) == {"catalog_dir", "mga_max_retries", "stubtest_mode"}


def test_refuses_a_gate_that_was_never_approved(make_run, capsys):
    run = make_run(stage="gate2")
    code, out = start(capsys, run, "emit", "--from-run", state(run)["run_id"])
    assert code == 2 and "never approved Gate 3" in out["error"]


def test_refuses_an_approval_superseded_by_a_revision(source_run, capsys):
    rid = state(source_run)["run_id"]
    s = state(source_run)
    s["stage"] = "gate4"
    (source_run / "state.json").write_text(json.dumps(s))
    ok(S, ["directive", "--run", str(source_run), "--gate", "4", "--text", "use csv",
           "--structural", "false"], capsys)  # functional → back to reuse
    for at in ("generate", "emit", "finalize"):
        code, out = start(capsys, source_run, at, "--from-run", rid)
        assert code == 2 and "superseded" in out["error"] and "use csv" in out["error"], at
    code, out = start(capsys, source_run, "reuse", "--from-run", rid)  # Gate 1 still stands
    assert code == 0


def test_refuses_v1_and_unknown_or_partial_ids(source_run, make_run, capsys):
    rid = state(source_run)["run_id"]
    code, out = start(capsys, source_run, "reuse", "--from-run", rid[:8])
    assert code == 2 and "no run is exactly" in out["error"] and rid in out["error"]
    code, out = start(capsys, source_run, "reuse", "--from-run", "nope")
    assert code == 2 and "no run 'nope'" in out["error"]
    v1 = make_run()
    s = state(v1)
    s["schema_version"] = 1
    (v1 / "state.json").write_text(json.dumps(s))
    code, out = start(capsys, source_run, "reuse", "--from-run", str(v1))
    assert code == 2 and "v1" in out["error"]


# ── other sources ──

def test_from_bundle(source_run, capsys):
    bundle = Path(state(source_run)["bundle_dir"])
    before = tree_hash(bundle)
    code, out = start(capsys, source_run, "generate", "--from-bundle", bundle)
    assert code == 2 and out["needs_choice"] == "existing_code"
    code, out = start(capsys, source_run, "generate", "--from-bundle", bundle, "--existing-code", "keep")
    assert code == 0 and {n["source"] for n in state(out["run"])["spec"]["nodes"]} == {"cached"}
    assert state(out["run"])["history"][-1]["source_kind"] == "bundle"
    code, out = start(capsys, source_run, "emit", "--from-bundle", bundle)
    assert code == 0 and state(out["run"])["stage"] == "emit"
    assert tree_hash(bundle) == before
    code, out = start(capsys, source_run, "reuse", "--from-bundle", bundle)
    assert code == 2 and "can start generate or emit" in out["error"]


def test_from_bundle_without_code_or_spec(source_run, capsys, tmp_path):
    (tmp_path / "empty").mkdir()
    code, out = start(capsys, source_run, "emit", "--from-bundle", tmp_path / "empty")
    assert code == 2 and "not a curate bundle" in out["error"]
    nocode = tmp_path / "nocode"
    nocode.mkdir()
    (nocode / "spec.json").write_text((FIX / "spec-resolved.json").read_text())
    code, out = start(capsys, source_run, "emit", "--from-bundle", nocode)
    assert code == 2 and "has no code" in out["error"]


def test_from_file_and_sketch(source_run, capsys, tmp_path):
    code, out = start(capsys, source_run, "reuse", "--from-file", FIX / "skeleton.json")
    assert code == 0 and state(out["run"])["stage"] == "reuse"
    code, out = start(capsys, source_run, "skeleton", "--sketch-file", FIX / "sketch.txt")
    assert code == 0 and state(out["run"])["stage"] == "skeleton"
    code, out = start(capsys, source_run, "generate", "--sketch-file", FIX / "sketch.txt")
    assert code == 2 and "starts at skeleton" in out["error"]
    code, out = start(capsys, source_run, "skeleton", "--from-run", state(source_run)["run_id"])
    assert code == 2 and "needs a sketch" in out["error"]


def test_exactly_one_source(source_run, capsys):
    rid = state(source_run)["run_id"]
    code, out = start(capsys, source_run, "reuse", "--from-run", rid, "--from-file", FIX / "skeleton.json")
    assert code == 2 and "exactly one source" in out["error"]
    code, out = start(capsys, source_run, "reuse")
    assert code == 2 and "exactly one source" in out["error"]
    code, out = start(capsys, source_run, "emit", "--from-run", rid, "--existing-code", "keep")
    assert code == 2 and "only applies when starting at generate" in out["error"]


def test_summary_is_ready_to_print(source_run, capsys):
    rid = state(source_run)["run_id"]
    code, out = start(capsys, source_run, "emit", "--from-run", rid, "--dry-run")
    md = out["summary"]
    assert md.startswith("**Start at emit — a new run**") and f"run `{rid}`" in md
    assert "will not be changed" in md and "gate3-approve.json" in md
    assert "Stub-test results copied: generate_text (passed)" in md and "Redoes: emit → deploy (if chosen at Gate 4) → monitor (if deployed) → finalize" in md
    code, out = start(capsys, source_run, "generate", "--from-bundle", Path(state(source_run)["bundle_dir"]),
                      "--existing-code", "keep", "--dry-run")
    assert out["summary"].startswith("**Start at generate — a new run**") and "bundle `" in out["summary"]


def test_real_start_returns_the_summary_with_the_new_run_id(source_run, capsys):
    code, out = start(capsys, source_run, "generate", "--from-run", state(source_run)["run_id"],
                      "--existing-code", "ignore")
    assert code == 0 and out["summary"].startswith(f"**Start at generate — new run `{out['run_id']}`**")


def test_real_start_from_bundle_summary_is_complete(source_run, capsys):
    bundle = Path(state(source_run)["bundle_dir"])
    code, out = start(capsys, source_run, "emit", "--from-bundle", bundle)
    md = out["summary"]
    assert code == 0 and "`None`" not in md and "Workflow: `mapreduce_word_count`, 5 functions" in md
    assert "with code: generate_text, split, map, shuffle, reduce" in md
    assert "Redoes: emit → deploy (if chosen at Gate 4) → monitor (if deployed) → finalize" in md and "spec.json" in md


# ── generate from a run with approved code (results log #27, bug 4) ──

def test_from_run_at_generate_asks_about_the_approved_code(source_run, capsys):
    runs = sorted(source_run.parent.iterdir())
    code, out = start(capsys, source_run, "generate", "--from-run", state(source_run)["run_id"])
    assert code == 2 and out["needs_choice"] == "existing_code" and out["flag"] == "--existing-code"
    assert set(out["options"]) == {"keep", "rewrite", "ignore"} and len(out["functions"]) == 5
    assert sorted(source_run.parent.iterdir()) == runs  # nothing created


@pytest.mark.parametrize("mode,source,generated", [("keep", "cached", False), ("rewrite", "adapt", True)])
def test_from_run_at_generate_carries_the_gate3_code(source_run, capsys, monkeypatch,
                                                     mode, source, generated):
    import scratch
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "1")  # rewrite generates
    before = tree_hash(source_run)
    code, out = start(capsys, source_run, "generate", "--from-run", state(source_run)["run_id"],
                      "--existing-code", mode)
    assert code == 0, out
    new = Path(out["run"])
    g3 = json.loads(next((source_run / "history").glob("*-gate3-approve.json")).read_text())
    approved = {n["name"]: n["code"] for n in g3["spec"]["nodes"]}
    nodes = state(new)["spec"]["nodes"]
    assert {n["name"]: n["code"] for n in nodes} == approved and {n["source"] for n in nodes} == {source}
    rec = state(new)["history"][-1]
    assert rec["code_snapshot"].endswith("gate3-approve.json") and rec["existing_code"] == mode
    assert ok(S, ["require", "--run", str(new), "--stage", "generate"], capsys)["problems"] == []
    ok(S, ["begin", "--run", str(new), "--stage", "generate"], capsys)
    built = ok(scratch, ["build", "--run", str(new)], capsys)
    assert all(n["generate"] is generated for n in built["nodes"])  # keep → no subagents at all
    if mode == "keep":
        assert "keeps generate_text, split, map, shuffle, reduce (stub-tested only" in out["summary"]
        assert "code from history/" in out["summary"]
    else:
        assert "writes generate_text, split, map, shuffle, reduce with a subagent each" in out["summary"]
    assert tree_hash(source_run) == before


def test_from_run_at_generate_refuses_without_the_foreground_variable(source_run, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", raising=False)
    code, out = start(capsys, source_run, "generate", "--from-run", state(source_run)["run_id"],
                      "--existing-code", "rewrite")
    assert code == 0, out
    code, out = call(S, ["require", "--run", out["run"], "--stage", "generate"], capsys)
    assert code == 1 and "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" in out["problems"][0]


def test_from_run_at_generate_ignore_says_everything_is_written(source_run, capsys):
    code, out = start(capsys, source_run, "generate", "--from-run", state(source_run)["run_id"],
                      "--existing-code", "ignore", "--dry-run")
    assert code == 0 and {f["has_code"] for f in out["functions"]} == {False}
    assert "writes generate_text, split, map, shuffle, reduce with a subagent each" in out["summary"]
    assert "keeps" not in out["summary"]


def test_keep_without_approved_code_is_refused(make_run, capsys):
    run = make_run()
    work = run / "work"
    (work / "sk.json").write_text((FIX / "skeleton.json").read_text())
    ok(S, ["skeleton", "--run", str(run), "--json", str(work / "sk.json")], capsys)
    ok(S, ["approve", "--run", str(run), "--gate", "1"], capsys)
    ok(S, ["apply-candidates", "--run", str(run), "--json", str(FIX / "candidates.json")], capsys)
    ok(S, ["merge-resolved", "--run", str(run), "--json", str(FIX / "resolved.json")], capsys)
    ok(S, ["approve", "--run", str(run), "--gate", "2"], capsys)
    rid = state(run)["run_id"]
    code, out = start(capsys, run, "generate", "--from-run", rid, "--existing-code", "keep")
    assert code == 2 and "no approved code to keep" in out["error"] and "--from-bundle" in out["error"]
    code, out = start(capsys, run, "generate", "--from-run", rid)  # no code: nothing to choose
    assert code == 0 and "needs_choice" not in out


# ── Gate 4: Approve & deploy / Approve, no deploy; starting at deploy ──

def test_gate4_records_the_bundle_hash_and_the_deploy_choice(source_run, capsys):
    import deploy
    s = state(source_run)
    assert s["deploy_choice"] == "no" and s["stage"] == "finalize"  # the fixture approved without --deploy
    assert s["bundle_hash"] == deploy.tree_hash(Path(s["bundle_dir"]))
    snap = json.loads(next((source_run / "history").glob("*-gate4-approve.json")).read_text())
    assert snap["bundle_hash"] == s["bundle_hash"] and s["history"][-1]["deploy"] == "no"


def test_gate4_approve_and_deploy_goes_to_the_deploy_stage(source_run, capsys, monkeypatch):
    s = state(source_run)
    s["stage"] = "gate4"
    (source_run / "state.json").write_text(json.dumps(s))
    out = ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--deploy", "yes"], capsys)
    assert out["stage"] == "deploy" and out["deploy"] == "yes" and out["next_skill"] == "curate-skills:curate-deploy"
    assert ok(S, ["require", "--run", str(source_run), "--stage", "deploy"], capsys)["problems"] == []
    code, out = call(S, ["approve", "--run", str(source_run), "--gate", "3", "--deploy", "yes"], capsys)
    assert code == 2 and "only applies to Gate 4" in out["error"]


def test_deploy_requires_the_deploy_choice(source_run, capsys):
    code, out = call(S, ["require", "--run", str(source_run), "--stage", "deploy"], capsys)
    assert code == 1 and "--start-at deploy --from-run" in out["problems"][0]


def test_approve_and_deploy_needs_the_bundle(source_run, capsys):
    s = state(source_run)
    s.update(stage="gate4", bundle_dir=str(source_run / "nowhere"))
    (source_run / "state.json").write_text(json.dumps(s))
    code, out = call(S, ["approve", "--run", str(source_run), "--gate", "4", "--deploy", "yes"], capsys)
    assert code == 2 and "cannot be deployed" in out["error"]


def test_start_at_deploy_copies_the_approved_bundle_and_its_hash(source_run, capsys):
    before = tree_hash(source_run)
    rid = state(source_run)["run_id"]
    code, out = start(capsys, source_run, "deploy", "--from-run", rid)
    assert code == 0, out
    new = Path(out["run"])
    s, src = state(new), state(source_run)
    assert s["stage"] == "deploy" and out["next_skill"] == "curate-skills:curate-deploy"
    assert s["bundle_dir"] == src["bundle_dir"] and s["bundle_hash"] == src["bundle_hash"]
    rec = s["history"][-1]
    assert rec["bundle_hash_from"] == "the Gate 4 approval snapshot" and rec["bundle_hash"] == src["bundle_hash"]
    assert rec["from_run"] == rid and rec["snapshot"].endswith("gate4-approve.json")
    assert out["redoes"] == ["deploy", "monitor", "finalize"]
    assert "- Bundle hash (checked against the bundle now): from the Gate 4 approval snapshot" in out["summary"]
    # Not a deploy choice yet: that is the user's Yes to "Start this run?"
    assert s["deploy_choice"] is None and rec["deploy"] == "pending" and rec["source_deploy_choice"] == "no"
    code, req = call(S, ["require", "--run", str(new), "--stage", "deploy"], capsys)
    assert code == 1 and 'has not confirmed starting this run' in req["problems"][0]
    conf = ok(S, ["confirm-start", "--run", str(new)], capsys)
    assert conf["recorded"] == "deploy_choice" and state(new)["deploy_choice"] == "yes"
    last = state(new)["history"][-1]
    assert last["decision"] == "deploy-confirmed" and 'answered Yes to "Start this run?"' in last["text"]
    assert ok(S, ["require", "--run", str(new), "--stage", "deploy"], capsys)["problems"] == []
    assert ok(S, ["confirm-start", "--run", str(new)], capsys)["recorded"] is None  # once only
    assert tree_hash(source_run) == before


def _drop_snapshot_hash(run):
    snap_path = next((run / "history").glob("*-gate4-approve.json"))
    snap = json.loads(snap_path.read_text())
    snap.pop("bundle_hash")
    snap_path.write_text(json.dumps(snap))


def test_start_at_deploy_takes_a_later_recorded_hash_from_state(source_run, capsys):
    _drop_snapshot_hash(source_run)  # a run approved before Gate 4 recorded hashes
    s = state(source_run)
    s["bundle_hash_recorded"] = {"how": "checked by hand"}
    (source_run / "state.json").write_text(json.dumps(s))
    code, out = start(capsys, source_run, "deploy", "--from-run", s["run_id"])
    assert code == 0, out
    assert "state.json (recorded after approval: checked by hand)" in state(Path(out["run"]))["history"][-1]["bundle_hash_from"]


@pytest.mark.parametrize("case,needle", [
    ("no_hash", "has no recorded bundle hash"),
    ("changed", "changed after Gate 4 was approved"),
    ("never", "never approved Gate 4"),
])
def test_start_at_deploy_refuses(source_run, capsys, case, needle):
    s = state(source_run)
    if case == "no_hash":
        _drop_snapshot_hash(source_run)
        s.pop("bundle_hash")
    if case == "changed":
        (Path(s["bundle_dir"]) / "README.md").write_text("edited after approval\n")
    if case == "never":
        s["history"] = [h for h in s["history"] if h.get("gate") != 4]
    (source_run / "state.json").write_text(json.dumps(s))
    runs = sorted(source_run.parent.iterdir())
    code, out = start(capsys, source_run, "deploy", "--from-run", s["run_id"])
    assert code == 2 and needle in out["error"], out
    assert sorted(source_run.parent.iterdir()) == runs


def test_start_at_deploy_only_from_a_run(source_run, capsys):
    code, out = start(capsys, source_run, "deploy", "--from-bundle", state(source_run)["bundle_dir"])
    assert code == 2 and "--from-run" in out["error"]


def test_redoing_emit_clears_the_deploy_record(source_run, capsys):
    s = state(source_run)
    s.update(deploy_choice="yes", deploy={"steps": {"publish": {"ok": True}}})
    (source_run / "state.json").write_text(json.dumps(s))
    out = ok(S, ["begin", "--run", str(source_run), "--stage", "emit", "--rewind", "--confirmed"], capsys)
    assert {"bundle_hash", "deploy_choice", "deploy"} <= set(out["cleared"])
    s = state(source_run)
    assert s["bundle_hash"] is None and s["deploy"] is None and s["deploy_choice"] is None


def _move_bundle(run):
    """Copy the run's bundle elsewhere and repoint only state.json (as copying a run by hand does)."""
    import shutil
    s = state(run)
    old = Path(s["bundle_dir"])
    new = old.parent.parent / "moved" / old.name
    shutil.copytree(old, new)
    s["bundle_dir"] = str(new)
    (run / "state.json").write_text(json.dumps(s))
    return old, new


@pytest.mark.parametrize("snapshot_hash", [True, False])
def test_start_from_a_copied_run_uses_the_copied_bundle(source_run, capsys, snapshot_hash):
    old, new = _move_bundle(source_run)
    if not snapshot_hash:
        _drop_snapshot_hash(source_run)  # approved before hashes: the hash comes from state.json
    snap_before = next((source_run / "history").glob("*-gate4-approve.json")).read_text()
    code, out = start(capsys, source_run, "deploy", "--from-run", state(source_run)["run_id"])
    assert code == 0, out
    s = state(Path(out["run"]))
    assert s["bundle_dir"] == str(new) and s["history"][-1]["bundle_dir_moved_from"] == str(old)
    assert "points at a copied or moved bundle folder" in out["summary"]
    assert next((source_run / "history").glob("*-gate4-approve.json")).read_text() == snap_before  # source untouched


def test_start_from_a_copied_run_refuses_a_changed_copy(source_run, capsys):
    _, new = _move_bundle(source_run)
    (new / "README.md").write_text("edited in the copy\n")
    code, out = start(capsys, source_run, "deploy", "--from-run", state(source_run)["run_id"])
    assert code == 2 and "changed after Gate 4 was approved" in out["error"]


def test_a_differently_named_bundle_is_not_taken_for_the_approved_one(source_run, capsys):
    s = state(source_run)
    other = Path(s["bundle_dir"]).parent / "something-else"
    import shutil
    shutil.copytree(s["bundle_dir"], other)
    s["bundle_dir"] = str(other)
    (source_run / "state.json").write_text(json.dumps(s))
    code, out = start(capsys, source_run, "deploy", "--from-run", s["run_id"])
    assert code == 0 and state(Path(out["run"]))["bundle_dir"] != str(other)  # the snapshot's folder


@pytest.mark.parametrize("at", ["deploy", "finalize"])
def test_a_moved_bundle_without_any_hash_is_refused(source_run, capsys, at):
    # Never by folder name alone: with no hash in the snapshot or state.json, a copied run's
    # moved bundle cannot be confirmed to be the approved one.
    _move_bundle(source_run)
    _drop_snapshot_hash(source_run)
    s = state(source_run)
    s.pop("bundle_hash")
    (source_run / "state.json").write_text(json.dumps(s))
    runs = sorted(source_run.parent.iterdir())
    code, out = start(capsys, source_run, at, "--from-run", s["run_id"])
    assert code == 2 and "cannot be confirmed to be the approved bundle by its name alone" in out["error"]
    assert "approve Gate 4 again" in out["error"] and "put the approved bundle back" in out["error"]
    assert sorted(source_run.parent.iterdir()) == runs


def test_a_moved_bundle_that_differs_from_the_hash_is_refused_at_finalize_too(source_run, capsys):
    _, new = _move_bundle(source_run)
    (new / "README.md").write_text("edited in the copy\n")
    code, out = start(capsys, source_run, "finalize", "--from-run", state(source_run)["run_id"])
    assert code == 2 and "does not match the hash recorded for the approved bundle" in out["error"]



def test_confirm_start_records_nothing_for_other_start_points(source_run, capsys):
    code, out = start(capsys, source_run, "emit", "--from-run", state(source_run)["run_id"])
    before = state(Path(out["run"]))
    assert ok(S, ["confirm-start", "--run", out["run"]], capsys)["recorded"] is None
    assert state(Path(out["run"])) == before


def test_start_at_deploy_end_to_end_with_fakes(source_run, capsys, monkeypatch):
    """start --at deploy --from-run → confirm → begin → preflight → publish, against fakes."""
    import deploy as D
    from test_deploy import FAKE, FakeGitHub, FakeS3
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FAASR_GH_USERNAME", "alice")
    monkeypatch.setenv("FAASR_ACTION_REPO", "faasr-actions")
    for k in ("FAASR_S3_ENDPOINT", "FAASR_S3_BUCKET", "FAASR_S3_REGION"):
        monkeypatch.setenv(k, {"FAASR_S3_BUCKET": "b"}.get(k, "x"))
    for k in ("FAASR_ACTION_BRANCH", "GITHUB_REPOSITORY", "GITHUB_REF_NAME", "CUSTOM_CONTAINER"):
        monkeypatch.delenv(k, raising=False)
    code, out = start(capsys, source_run, "deploy", "--from-run", state(source_run)["run_id"])
    assert code == 0, out
    new = out["run"]
    ok(S, ["confirm-start", "--run", new], capsys)
    assert ok(S, ["begin", "--run", new, "--stage", "deploy"], capsys)["mode"] == "fresh"

    fake = FakeGitHub(writable=True)
    code = D.main(["preflight", "--run", new], opener=fake, s3_factory=FakeS3())
    pf = json.loads(capsys.readouterr().out)
    assert code == 0 and pf["ok"], pf["problems"]
    code = D.main(["publish", "--run", new], opener=fake, s3_factory=FakeS3())
    assert json.loads(capsys.readouterr().out)["needs_confirmation"] and fake.puts == []
    code = D.main(["publish", "--run", new, "--confirmed"], opener=fake, s3_factory=FakeS3())
    res = json.loads(capsys.readouterr().out)
    assert code == 0 and res["ok"], res
    name = pf["deploy_name"]
    # named after the run that approved Gate 4 (the source), not this deploy-only run
    m = D.RUN_TS_RX.match(state(source_run)["run_id"])
    assert name == f"{pf['approved_name']}-{m[1]}{m[2]}-{m[3]}{m[4]}", name
    assert "Gate 4 approval in run " + state(source_run)["run_id"] in pf["name_source"]
    assert all(p.startswith(f"functions/{name}/") for p, _ in fake.puts)
    assert len(fake.puts) == len(state(source_run)["spec"]["nodes"])

    # the carried-over approval only counts while the bundle matches its hash
    bundle = Path(state(Path(new))["bundle_dir"])
    (bundle / "README.md").write_text("edited after approval\n")
    code = D.main(["upload", "--run", new, "--confirmed"], opener=fake, s3_factory=FakeS3())
    res = json.loads(capsys.readouterr().out)
    assert code == 1 and any("no longer matches the Gate 4 approval carried over" in p for p in res["problems"])
