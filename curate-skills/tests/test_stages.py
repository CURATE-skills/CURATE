import json
import subprocess
import sys
from pathlib import Path

import pytest

import scratch
import spec as S
from conftest import call, ok
from curate_models import FunctionSpec, IOSpec, WorkflowSpec
from runstate import node_basis, spec_hash

FIX = Path(__file__).parent / "fixtures" / "e2-stages"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def state(run):
    return json.loads((Path(run) / "state.json").read_text())


def patch(run, **fields):
    s = state(run)
    s.update(fields)
    (Path(run) / "state.json").write_text(json.dumps(s))
    return s


def resolved_spec():
    return json.loads((FIX / "spec-resolved.json").read_text())


def coded_spec():
    return json.loads((FIX / "spec-with-code.json").read_text())


@pytest.fixture(autouse=True)
def foreground(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "1")


# ── next ──

@pytest.mark.parametrize("stage,skill", [
    ("skeleton", "curate-skeleton"), ("gate1", "curate-skeleton"), ("reuse", "curate-reuse"),
    ("resolve", "curate-resolve"), ("gate2", "curate-resolve"), ("generate", "curate-generate"),
    ("gate3", "curate-generate"), ("emit", "curate-emit"), ("gate4", "curate-emit"),
    ("deploy", "curate-deploy"), ("monitor", "curate-monitor"), ("finalize", "wda-finalize"), ("done", None), ("stopped", None)])
def test_next_names_the_stage_skill(make_run, capsys, stage, skill):
    run = make_run(stage=stage)
    out = ok(S, ["next", "--run", str(run)], capsys)
    assert out["next_skill"] == (f"curate-skills:{skill}" if skill else None)
    if skill:
        assert out["command"] == f"/curate-skills:{skill} --run {state(run)['run_id']}"


# ── require ──

def test_require_names_missing_input_and_producer(make_run, capsys):
    run = make_run()
    rid = state(run)["run_id"]
    assert ok(S, ["require", "--run", str(run), "--stage", "skeleton"], capsys)["problems"] == []
    code, out = call(S, ["require", "--run", str(run), "--stage", "reuse"], capsys)
    assert code == 1 and out["problems"] == [f"no approved skeleton — run /curate-skills:curate-skeleton --run {rid}"]
    patch(run, spec=resolved_spec())
    code, out = call(S, ["require", "--run", str(run), "--stage", "resolve"], capsys)
    assert out["problems"] == [f"no reuse decisions for this skeleton — run /curate-skills:curate-reuse --run {rid}"]
    code, out = call(S, ["require", "--run", str(run), "--stage", "emit"], capsys)
    assert "have no code — run /curate-skills:curate-generate" in out["problems"][0]
    code, out = call(S, ["require", "--run", str(run), "--stage", "finalize"], capsys)
    assert out["problems"] == [f"no validated bundle — run /curate-skills:curate-emit --run {rid}"]


def test_require_detects_stale_inputs(make_run, capsys):
    spec = resolved_spec()
    run = make_run(spec=spec, enriched_spec=spec, enriched_basis="old", spec_basis="old")
    code, out = call(S, ["require", "--run", str(run), "--stage", "resolve"], capsys)
    assert "older version of the spec" in out["problems"][0]
    patch(run, enriched_basis=spec_hash(spec))
    assert ok(S, ["require", "--run", str(run), "--stage", "resolve"], capsys)
    code, out = call(S, ["require", "--run", str(run), "--stage", "generate"], capsys)
    assert any("not resolved from the current reuse decisions" in p for p in out["problems"])

    coded = coded_spec()
    run = make_run(spec=coded, mga={"map": {"status": "passed", "basis": "old"}},
                   validation={"ok": True, "spec_hash": "old"})
    code, out = call(S, ["require", "--run", str(run), "--stage", "emit"], capsys)
    assert "map were generated for an older version" in out["problems"][0]
    code, out = call(S, ["require", "--run", str(run), "--stage", "finalize"], capsys)
    assert "bundle is older than the spec" in out["problems"][0]


def test_require_generate_preflight_only_when_generating(make_run, capsys, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS")
    run = make_run(spec=resolved_spec())
    code, out = call(S, ["require", "--run", str(run), "--stage", "generate"], capsys)
    assert code == 1 and "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" in out["problems"][0]
    reused = coded_spec()
    for n in reused["nodes"]:
        n["source"] = "cached"
    patch(run, spec=reused)
    assert ok(S, ["require", "--run", str(run), "--stage", "generate"], capsys)["problems"] == []


# ── begin / end ──

def test_begin_refuses_before_the_stage_is_reached(make_run, capsys):
    run = make_run(stage="gate2", spec=resolved_spec())
    code, out = call(S, ["begin", "--run", str(run), "--stage", "generate"], capsys)
    rid = state(run)["run_id"]
    assert code == 1 and out["mode"] == "refused" and out["error"] == (
        "the run has not reached generate yet: it is waiting at Gate 2. Approve Gate 2 first: "
        f"/curate-skills:curate-resolve --run {rid}")
    assert state(run)["stage"] == "gate2"
    run = make_run(stage="reuse", spec=resolved_spec())
    code, out = call(S, ["begin", "--run", str(run), "--stage", "resolve"], capsys)
    assert out["error"].endswith("it is at stage reuse. Run "
                                 f"/curate-skills:curate-reuse --run {state(run)['run_id']} first")


def test_begin_fresh_clears_downstream_and_resume_keeps(make_run, capsys):
    spec = coded_spec()
    run = make_run(stage="generate", spec=spec, mga={"map": {"status": "passed"}},
                   validation={"ok": True}, bundle_dir="/old")
    out = ok(S, ["begin", "--run", str(run), "--stage", "generate"], capsys)
    s = state(run)
    assert out["mode"] == "fresh" and set(out["cleared"]) == {"mga", "validation", "bundle_dir"}
    assert s["mga"] == {} and s["validation"] is None and s["begun"] == "generate"
    patch(run, mga={"map": {"status": "passed"}})
    out = ok(S, ["begin", "--run", str(run), "--stage", "generate"], capsys)
    assert out["mode"] == "resume" and state(run)["mga"] == {"map": {"status": "passed"}}


def test_begin_keeps_revision_context(make_run, capsys):
    spec = coded_spec()
    run = make_run(stage="reuse", spec=spec, prior_spec=spec, code_feedback="sort words")
    ok(S, ["begin", "--run", str(run), "--stage", "reuse"], capsys)
    s = state(run)
    assert s["prior_spec"] == spec and s["code_feedback"] == "sort words"


def test_rewind_needs_flag_moves_code_and_counts(make_run, capsys):
    spec = coded_spec()
    run = make_run(stage="gate3", spec=spec, mga={"map": {"status": "passed"}}, max_iterations=1)
    code, out = call(S, ["begin", "--run", str(run), "--stage", "reuse"], capsys)
    assert code == 1 and "--rewind --confirmed" in out["error"] and out["needs_confirmation"]
    assert "Ask the user first" in out["error"] and state(run)["stage"] == "gate3"
    out = ok(S, ["begin", "--run", str(run), "--stage", "reuse", "--rewind", "--confirmed"], capsys)
    s = state(run)
    assert out["mode"] == "rewind" and s["stage"] == "reuse" and s["iteration"] == 1
    assert s["prior_spec"] == spec and s["mga"] == {} and s["history"][-1]["gate"] == "rewind"
    patch(run, stage="gate3")
    code, out = call(S, ["begin", "--run", str(run), "--stage", "reuse", "--rewind", "--confirmed"], capsys)
    assert code == 1 and out["cap_reached"]  # cap 1 already used


def test_stopped_run_resumes_where_it_stopped(make_run, capsys):
    run = make_run(stage="gate2", spec=resolved_spec())
    ok(S, ["set-stage", "--run", str(run), "--stage", "stopped"], capsys)
    s = state(run)
    assert s["stopped_from"] == "gate2"
    assert ok(S, ["next", "--run", str(run)], capsys)["stopped_from"] == "gate2"
    out = ok(S, ["begin", "--run", str(run), "--stage", "resolve"], capsys)
    assert out["mode"] == "resume" and state(run)["stage"] == "gate2"


def test_end_generate_requires_final_statuses(make_run, capsys):
    run = make_run(stage="generate", spec=coded_spec(), mga={"map": {"status": "passed"}})
    code, out = call(S, ["end", "--run", str(run), "--stage", "generate"], capsys)
    assert code == 1 and "generate_text" in out["pending"]
    patch(run, mga={n["name"]: {"status": "passed"} for n in coded_spec()["nodes"]})
    assert ok(S, ["end", "--run", str(run), "--stage", "generate"], capsys)["stage"] == "gate3"


# ── routing ──

def test_routing_through_reuse(make_run, capsys, tmp_path):
    run = make_run()
    (run / "work" / "sk.json").write_text((FIX / "skeleton.json").read_text())
    ok(S, ["skeleton", "--run", str(run), "--json", str(run / "work" / "sk.json")], capsys)
    assert ok(S, ["approve", "--run", str(run), "--gate", "1"], capsys)["stage"] == "reuse"
    ok(S, ["apply-candidates", "--run", str(run), "--json", str(FIX / "candidates.json")], capsys)
    assert state(run)["stage"] == "resolve"
    ok(S, ["merge-resolved", "--run", str(run), "--json", str(FIX / "resolved.json")], capsys)
    s = state(run)
    assert s["stage"] == "gate2" and s["spec_basis"] == spec_hash(s["enriched_spec"])
    out = ok(S, ["directive", "--run", str(run), "--gate", "2", "--text", "rename output",
                 "--structural", "false"], capsys)
    assert out["next_stage"] == "reuse" and out["next_skill"] == "curate-skills:curate-reuse"


def test_gate4_refuses_stale_bundle(make_run, capsys):
    spec = coded_spec()
    run = make_run(stage="gate4", spec=spec, validation={"ok": True, "spec_hash": "stale"})
    code, out = call(S, ["approve", "--run", str(run), "--gate", "4"], capsys)
    assert code == 2 and "different version of the spec" in out["error"]
    patch(run, validation={"ok": True, "spec_hash": spec_hash(spec)})
    assert ok(S, ["approve", "--run", str(run), "--gate", "4"], capsys)["stage"] == "finalize"


# ── v1 state, lock, scratch changes ──

def test_v1_state_is_refused(make_run, capsys):
    run = make_run()
    patch(run, schema_version=1)
    code, out = call(S, ["status", "--run", str(run)], capsys)
    assert code == 2 and "curate-skills v1" in out["error"] and "curate-v1" in out["error"]


def test_concurrent_writers_do_not_lose_updates(make_run, tmp_path):
    nodes = [FunctionSpec(name=f"n{i}", outputs=[IOSpec(name=f"o{i}")]) for i in range(12)]
    run = make_run(spec=WorkflowSpec(name="w", nodes=nodes))
    procs = [subprocess.Popen([sys.executable, str(SCRIPTS / "scratch.py"), "record", "--run", str(run),
                               "--node", f"n{i}", "--status", "passed", "--attempts", "1"],
                              stdout=subprocess.DEVNULL, cwd=SCRIPTS) for i in range(12)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    assert sorted(state(run)["mga"]) == sorted(f"n{i}" for i in range(12))


def test_record_stores_basis_and_harvest_clears_validation(make_run, capsys):
    spec = coded_spec()
    run = make_run(stage="generate", spec=spec, validation={"ok": True, "spec_hash": "x"})
    ok(scratch, ["record", "--run", str(run), "--node", "map", "--status", "passed"], capsys)
    node = next(n for n in spec["nodes"] if n["name"] == "map")
    assert state(run)["mga"]["map"]["basis"] == node_basis(node)
    d = run / "functions" / "map"
    d.mkdir(parents=True)
    (d / "map.py").write_text("def map(folder, input1, output1):\n    pass\n")
    ok(scratch, ["harvest", "--run", str(run), "--node", "map"], capsys)
    assert state(run)["validation"] is None


def test_unlock_only_removes_this_runs_lock(make_run, capsys):
    run = make_run()
    lock = Path(state(run)["project_dir"]) / ".curate" / "mga.lock.json"
    lock.write_text(json.dumps({"run_id": "someone-else", "node": "x"}))
    out = ok(scratch, ["unlock", "--run", str(run)], capsys)
    assert out["removed"] is False and lock.exists()
    lock.write_text(json.dumps({"run_id": state(run)["run_id"], "node": "x"}))
    assert ok(scratch, ["unlock", "--run", str(run)], capsys)["removed"] and not lock.exists()


# ── start at each stage and walk on, with no model ──

def test_walk_from_generate_import_to_finalize(tmp_path, capsys, monkeypatch):
    """Import code to verify at generate, mark it reused, emit, and reach finalize."""
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS")  # all reused: no preflight needed
    code, out = call(S, ["import", "--stage", "generate", "--from", str(FIX / "spec-resolved.json"),
                         "--functions", str(FIX / "functions"), "--existing-code", "keep",
                         "--project-dir", str(tmp_path / "p"), "--catalog-dir", str(tmp_path / "c")], capsys)
    assert code == 0, out
    run = out["run"]
    ok(S, ["begin", "--run", run, "--stage", "generate"], capsys)
    for n in coded_spec()["nodes"]:
        ok(scratch, ["record", "--run", run, "--node", n["name"], "--status", "reused"], capsys)
    ok(S, ["end", "--run", run, "--stage", "generate"], capsys)
    assert ok(S, ["approve", "--run", run, "--gate", "3"], capsys)["stage"] == "emit"
    ok(S, ["begin", "--run", run, "--stage", "emit"], capsys)
    s = state(run)
    s["validation"] = {"ok": True, "level": "structural", "error": None, "spec_hash": spec_hash(s["spec"])}
    s["stage"] = "gate4"
    Path(run, "state.json").write_text(json.dumps(s))
    ok(S, ["approve", "--run", run, "--gate", "4"], capsys)
    assert ok(S, ["require", "--run", run, "--stage", "finalize"], capsys)["problems"] == []
    assert ok(S, ["next", "--run", run], capsys)["next_skill"] == "curate-skills:wda-finalize"


# ── regressions from a user test (results log #18, #19) ──

def test_rewind_without_confirmation_changes_nothing(make_run, capsys):
    spec = coded_spec()
    run = make_run(stage="gate4", spec=spec, mga={"map": {"status": "passed"}},
                   validation={"ok": True})
    before = state(run)
    code, out = call(S, ["begin", "--run", str(run), "--stage", "generate", "--rewind"], capsys)
    assert code == 1 and out["needs_confirmation"] and "explicit OK" in out["error"]
    assert "uses one revision round (0 of 5 used)" in out["error"]
    assert state(run) == before


def test_rewind_to_generate_starts_new_functions_empty(make_run, capsys):
    spec = coded_spec()
    by = {n["name"]: n for n in spec["nodes"]}
    by["split"].update(source="cached", origin_kind="cache", origin_name="split")
    by["shuffle"].update(source="adapt", origin_kind="cache", origin_name="shuffle")
    run = make_run(stage="gate4", spec=spec, mga={n: {"status": "passed"} for n in by})  # past generate
    out = ok(S, ["begin", "--run", str(run), "--stage", "generate", "--rewind", "--confirmed"], capsys)
    assert out["mode"] == "rewind" and out["iteration"] == 1
    s = state(run)
    now = {n["name"]: n for n in s["spec"]["nodes"]}
    assert now["map"]["code"] is None and now["reduce"]["code"] is None          # new: dropped
    assert now["split"]["code"] and now["shuffle"]["code"]                        # cached / adapt: kept
    assert all(n["code"] for n in s["prior_spec"]["nodes"])                       # old code kept here
    out = ok(scratch, ["build", "--run", str(run)], capsys)
    gen = {n["name"]: n["generate"] for n in out["nodes"]}
    assert gen == {"generate_text": True, "split": False, "map": True, "shuffle": True, "reduce": True}
    fns = run / "functions"
    assert not (fns / "map" / "map.py").exists() and not (fns / "generate_text" / "generate_text.py").exists()
    assert "Previous Implementation" not in (fns / "map" / "spec.md").read_text()
    assert (fns / "shuffle" / "shuffle.py").exists()
    assert "Previous Implementation" in (fns / "shuffle" / "spec.md").read_text()
    assert (fns / "split" / "split.py").exists() and not (fns / "split" / "spec.md").exists()


def test_build_never_seeds_a_new_function_with_code(make_run, capsys):
    spec = coded_spec()  # every node: source new, with code (e.g. harvested earlier)
    run = make_run(stage="generate", spec=spec)
    ok(scratch, ["build", "--run", str(run)], capsys)
    for n in spec["nodes"]:
        d = run / "functions" / n["name"]
        assert not (d / f"{n['name']}.py").exists()
        assert "Previous Implementation" not in (d / "spec.md").read_text()


# ── from the user's interactive U1 test ──

def test_stopped_run_names_the_resume_command(make_run, capsys):
    run = make_run(stage="gate2", spec=resolved_spec())
    rid = state(run)["run_id"]
    out = ok(S, ["set-stage", "--run", str(run), "--stage", "stopped"], capsys)
    for out in (out, ok(S, ["next", "--run", str(run)], capsys), ok(S, ["status", "--run", str(run)], capsys)):
        assert out["resume_command"] == f"/curate-skills:curate --resume {rid}"
        assert out["resume_stage_command"] == f"/curate-skills:curate-resolve --run {rid}"
        assert out["stopped_from"] == "gate2"
    assert "resume_command" not in ok(S, ["next", "--run", str(make_run(stage="gate2"))], capsys)


def test_gate3_after_a_kept_import_says_kept_and_passed(tmp_path, capsys, monkeypatch):
    import stubtest
    code, out = call(S, ["import", "--stage", "generate", "--from", str(FIX / "spec-with-code.json"),
                         "--existing-code", "keep", "--project-dir", str(tmp_path / "p"),
                         "--catalog-dir", str(tmp_path / "c")], capsys)
    run = out["run"]
    ok(S, ["begin", "--run", run, "--stage", "generate"], capsys)
    built = ok(scratch, ["build", "--run", run], capsys)
    assert not any(n["generate"] for n in built["nodes"])  # kept: no subagents
    for n in built["order"]:
        assert stubtest.run_stubtest(Path(run), n)["ok"]
        ok(scratch, ["record", "--run", run, "--node", n, "--status", "reused"], capsys)
    ok(S, ["end", "--run", run, "--stage", "generate"], capsys)
    md = ok(S, ["show", "--run", run, "--gate", "3"], capsys)["markdown"]
    for n in built["order"]:
        assert f"| `{n}` | kept: your code | passed (not regenerated) |" in md


# ── from the user's weather run (results log #25) ──

WEATHER = {"name": "weather", "description": "compare winter weather",
           "nodes": [{"name": "download", "description": "Download the station CSV."},
                     {"name": "process_variable", "rank": 3,
                      "description": "Runs in parallel, one instance per variable, selected by rank."},
                     {"name": "plot", "description": "Plot three panels."}],
           "edges": [{"from_node": "download", "to_node": "process_variable"},
                     {"from_node": "process_variable", "to_node": "plot"}]}


@pytest.mark.parametrize("gate", [1, 2, 3, 4])
def test_show_returns_question_header_and_compact_table(make_run, capsys, gate):
    spec = coded_spec()
    run = make_run(skeleton_draft={"spec": spec}, spec=spec, mga={"map": {"status": "reused"}},
                   validation={"ok": True, "level": "faasr_py"}, bundle_dir="/b")
    out = ok(S, ["show", "--run", str(run), "--gate", str(gate)], capsys)
    assert out["header"] == f"Gate {gate}" and out["question"].endswith('(Type in "Other" to request changes.)')
    assert out["markdown"].startswith(f"## Gate {gate}")
    names = [n["name"] for n in spec["nodes"]]
    if gate < 4:
        assert all(n in out["compact"] for n in names)
    else:
        assert "validation: passed" in out["compact"]
    if gate == 3:
        assert "map | " in out["compact"] and "passed (not regenerated)" in out["compact"]


def test_gate1_shows_each_rank_once(make_run, capsys):
    """Bug report: 'Rank: 3' shown twice. The table has one Rank cell per function."""
    run = make_run(skeleton_draft={"spec": WEATHER})
    out = ok(S, ["show", "--run", str(run), "--gate", "1"], capsys)
    rows = [l for l in out["markdown"].splitlines() if l.startswith("| ") and "`process_variable`" in l]
    assert len(rows) == 1 and rows[0].split("|")[3].strip() == "3"
    assert "Rank: 3" not in out["markdown"] and out["markdown"].count("process_variable ×3") == 2  # 2 DAG edges
    compact = out["compact"]
    assert compact.count("process_variable ×3 —") == 1


def test_gate_procedures_always_print_then_ask_with_the_table():
    """Bug report: tables not printed before the question; question tool dropped for chat."""
    root = Path(__file__).resolve().parents[1] / "references"
    gates = (root / "gates.md").read_text()
    assert "Print its `markdown` exactly as a message" in gates
    assert "Ask with AskUserQuestion whenever it is available" in gates and "never switch to plain chat" in gates
    for stage in ("skeleton", "resolve", "generate", "emit"):
        text = (root / "stages" / f"{stage}.md").read_text()
        assert "print its `markdown` verbatim as a message — never re-formatted" in text, stage
        assert "`compact` as `preview`" in text and "every time" in text, stage
    assert "whenever it is available" in (root / "stages" / "finalize.md").read_text()


def test_curate_advice_and_confirmations():
    """Results log #27: single-function advice (bug 4), run after a yes (bug 5), question tool (bug 3)."""
    root = Path(__file__).resolve().parents[1]
    skill = (root / "skills" / "curate" / "SKILL.md").read_text()
    desc = next(l for l in skill.splitlines() if l.startswith("description: "))[len("description: "):]
    assert len(desc) <= 1024
    assert "--start-at generate --from-run <run_id> --existing-code keep" in desc
    assert "only that function is redone" in desc and "--existing-code ignore" in desc
    assert "After an explicit yes" in desc and "invoke this skill" in desc
    assert "never run CURATE's scripts or rewind yourself" in desc
    assert "also when the user already said yes" in skill and "Use AskUserQuestion whenever it is available" in skill
    gates = (root / "references" / "gates.md").read_text()
    assert "--existing-code keep" in gates and "**Yes, rewind** / **No**" in gates


def test_gates_are_the_users_and_displays_are_verbatim():
    """Results log #28: Claude promised to submit a Gate 3 change; a re-rendered Gate 1 table."""
    root = Path(__file__).resolve().parents[1]
    gates = (root / "references" / "gates.md").read_text()
    assert "**Gate decisions are always the user's.**" in gates
    assert "or one you proposed the wording\nfor" in gates or "proposed the wording" in gates
    assert "Copy it verbatim: never\n   re-format" in gates and "run `spec.py show` again" in gates
    skill = (root / "skills" / "curate" / "SKILL.md").read_text()
    desc = next(l for l in skill.splitlines() if l.startswith("description: "))
    assert len(desc) - len("description: ") <= 1024
    assert "Gate decisions are the user's: never approve or submit a change for them." in desc
    assert "the user types the change at Gate 3" in desc
    assert "don't say you will submit it" in skill


def test_deploy_stage_asks_before_every_step_and_never_touches_credentials():
    plugin = Path(__file__).resolve().parents[1]
    text = " ".join((plugin / "references" / "stages" / "deploy.md").read_text().split())
    assert "Each of the four steps is the user's decision, asked right before it runs" in text
    assert "A yes to one step is never a yes to the next" in text
    assert "without** `--confirmed`" in text and "Never pass `--confirmed` without the user's yes" in text
    assert "Never read, print, echo or ask for a credential" in text and "never open a `.env` file" in text
    assert "never pass `--overwrite-own-deploy` unless the user chose it" in text
    skill = (plugin / "skills" / "curate-deploy" / "SKILL.md").read_text()
    assert "disable-model-invocation: true" in skill and "references/stages/deploy.md" in skill
    emit_md = (plugin / "references" / "stages" / "emit.md").read_text()
    assert "**Approve & deploy**" in emit_md and "--deploy yes" in emit_md and "--deploy no" in emit_md


def test_monitor_stage_is_read_only_and_states_the_final_rules():
    plugin = Path(__file__).resolve().parents[1]
    text = " ".join((plugin / "references" / "stages" / "monitor.md").read_text().split())
    assert "never writes to GitHub or S3 and never deploys again" in text
    assert "written after the dispatch" in text and "**stale**" in text
    for result in ("`success`", "`success_unconfirmed`", "`failure`", "`timeout`"):
        assert result in text
    assert "Job-log text is never shown or saved" in text and "never open a `.env` file" in text
    assert "--config-minutes '${user_config.monitor_minutes}'" in text
    skill = (plugin / "skills" / "curate-monitor" / "SKILL.md").read_text()
    assert "disable-model-invocation: true" in skill and "references/stages/monitor.md" in skill
    fin = " ".join((plugin / "references" / "stages" / "finalize.md").read_text().split())
    assert "**Accept the unconfirmed match**" in fin and "accept-unconfirmed --run <run> --confirmed" in fin
