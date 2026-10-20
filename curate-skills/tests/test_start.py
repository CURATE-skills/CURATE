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
def test_from_run_copies_the_approved_results_into_a_new_run(source_run, capsys, at, gate, stage):
    before = tree_hash(source_run)
    rid = state(source_run)["run_id"]
    code, out = start(capsys, source_run, at, "--from-run", rid)
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
    code, out = start(capsys, source_run, "generate", "--from-run", state(source_run)["run_id"], "--dry-run")
    assert code == 0 and out["dry_run"] and sorted(source_run.parent.iterdir()) == runs
    assert out["uses"].startswith("history/") and "Gate 2" in out["uses"]
    assert out["redoes"] == ["generate", "emit", "finalize"]
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
    assert "Stub-test results copied: generate_text (passed)" in md and "Redoes: emit → finalize" in md
    code, out = start(capsys, source_run, "generate", "--from-bundle", Path(state(source_run)["bundle_dir"]),
                      "--existing-code", "keep", "--dry-run")
    assert out["summary"].startswith("**Start at generate — a new run**") and "bundle `" in out["summary"]


def test_real_start_returns_the_summary_with_the_new_run_id(source_run, capsys):
    code, out = start(capsys, source_run, "generate", "--from-run", state(source_run)["run_id"])
    assert code == 0 and out["summary"].startswith(f"**Start at generate — new run `{out['run_id']}`**")


def test_real_start_from_bundle_summary_is_complete(source_run, capsys):
    bundle = Path(state(source_run)["bundle_dir"])
    code, out = start(capsys, source_run, "emit", "--from-bundle", bundle)
    md = out["summary"]
    assert code == 0 and "`None`" not in md and "Workflow: `mapreduce_word_count`, 5 functions" in md
    assert "with code: generate_text, split, map, shuffle, reduce" in md
    assert "Redoes: emit → finalize" in md and "spec.json" in md
