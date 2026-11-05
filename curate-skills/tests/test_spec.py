import json
from pathlib import Path

import pytest

import catalog
import spec as S
from conftest import call, ok
from runstate import spec_hash
from curate_models import CatalogEntry, FunctionSpec, IOSpec, WorkflowSpec


def state(run):
    return json.loads((Path(run) / "state.json").read_text())


def write(run, name, data):
    p = Path(run) / "work" / name
    p.write_text(json.dumps(data))
    return str(p)


SKELETON = {
    "name": "word_count", "description": "count words",
    "nodes": [{"name": "split_words", "description": "split text", "rank": 1},
              {"name": "map_words", "description": "count per chunk", "rank": 3},
              {"name": "reduce_counts", "description": "sum counts"}],
    "edges": [{"from_node": "split_words", "to_node": "map_words"},
              {"from_node": "map_words", "to_node": "reduce_counts"},
              {"from_node": "split_words", "to_node": "reduce_counts"}],
}


def approved_run(make_run, capsys, **kw):
    run = make_run(**kw)
    assert call(S, ["skeleton", "--run", str(run), "--json", write(run, "skeleton.json", SKELETON)], capsys)[0] == 0
    assert call(S, ["approve", "--run", str(run), "--gate", "1"], capsys)[0] == 0
    return run


# ── init and settings ──

def test_init_defaults_and_user_model(make_run, tmp_path, capsys):
    model = tmp_path / "PyADM1 Model.py"
    model.write_text('"""Anaerobic digestion model."""\nprint(1)\n')
    run = make_run(extra_args=["--user-model", str(model)])
    s = state(run)
    assert run.parent.name == "runs" and (run / "work").is_dir()
    assert s["stage"] == "skeleton" and s["mga_max_retries"] == 3 and s["stubtest_mode"] == "strict"
    assert s["user_models"] == [{"name": "pyadm1_model", "path": str(model.resolve()),
                                 "description": "Anaerobic digestion model."}]


@pytest.mark.parametrize("flag,env,config,expected", [
    ("0", "5", "7", (0, "flag")),
    (None, "5", "7", (5, "CURATE_MGA_RETRIES")),
    (None, None, "7", (7, "plugin setting")),
    (None, None, "${user_config.mga_retries}", (3, "default")),
])
def test_mga_retries_precedence(monkeypatch, flag, env, config, expected):
    if env:
        monkeypatch.setenv("CURATE_MGA_RETRIES", env)
    assert S.resolve_setting(flag, "CURATE_MGA_RETRIES", config, 3, S._retries) == expected


def test_stubtest_mode_setting(make_run, monkeypatch, capsys):
    monkeypatch.setenv("CURATE_STUBTEST_MODE", "parity")
    assert state(make_run())["stubtest_mode"] == "parity"
    req = make_run().parent.parent.parent / "request.txt"
    code, out = call(S, ["init", "--request-file", str(req), "--stubtest-mode", "loose"], capsys)
    assert code == 2 and "loose" in out["error"] and "run" not in out


# ── skeleton / gate 1 ──

def test_skeleton_strips_io_and_prunes(make_run, capsys):
    run = make_run()
    sk = json.loads(json.dumps(SKELETON))
    sk["nodes"][0]["inputs"] = ["words.txt"]
    code, out = call(S, ["skeleton", "--run", str(run), "--json", write(run, "skeleton.json", sk)], capsys)
    assert code == 0 and any("split_words → reduce_counts" in r for r in out["repairs"])
    draft = state(run)["skeleton_draft"]["spec"]
    assert draft["nodes"][0]["inputs"] == [] and len(draft["edges"]) == 2
    assert state(run)["stage"] == "gate1"


def test_bad_revision_keeps_last_good(make_run, capsys):
    run = make_run()
    ok(S, ["skeleton", "--run", str(run), "--json", write(run, "skeleton.json", SKELETON)], capsys)
    bad = {"nodes": [{"name": "Bad-Name"}]}
    code, out = call(S, ["skeleton", "--run", str(run), "--json", write(run, "skeleton.json", bad)], capsys)
    assert code == 1 and out["kept_previous"]
    s = state(run)
    assert s["skeleton_draft"]["spec"]["name"] == "word_count" and s["stage"] == "gate1"


def test_gate1_refinement_does_not_count(make_run, capsys):
    run = make_run()
    ok(S, ["skeleton", "--run", str(run), "--json", write(run, "skeleton.json", SKELETON)], capsys)
    code, out = call(S, ["directive", "--run", str(run), "--gate", "1", "--text", "split map"], capsys)
    assert code == 0 and out["iteration"] == 0 and out["next_stage"] == "skeleton"
    code, ctx = call(S, ["prompt-context", "--run", str(run), "--stage", "skeleton"], capsys)
    assert ctx["refinement"] == "split map"


def test_approve_gate1_prior_spec_rules(make_run, capsys):
    coded = WorkflowSpec(name="old", nodes=[FunctionSpec(name="split_words", code="def split_words(): pass")])
    run = make_run(spec=coded)
    ok(S, ["skeleton", "--run", str(run), "--json", write(run, "skeleton.json", SKELETON)], capsys)
    ok(S, ["approve", "--run", str(run), "--gate", "1"], capsys)
    assert state(run)["prior_spec"]["name"] == "old"

    run2 = make_run(spec=coded, spec_is_seed=True)
    ok(S, ["skeleton", "--run", str(run2), "--json", write(run2, "skeleton.json", SKELETON)], capsys)
    ok(S, ["approve", "--run", str(run2), "--gate", "1"], capsys)
    assert state(run2)["prior_spec"] is None


# ── candidates ──

@pytest.fixture
def seeded_catalog(tmp_path):
    cat = catalog.Catalog(tmp_path / "catalog")
    e = CatalogEntry(function_spec=FunctionSpec(
        name="split_text", description="split text into chunks",
        inputs=[IOSpec(name="words.txt")], outputs=[IOSpec(name="chunk_{rank}.txt")],
        dependencies=["os", "numpy"], secrets=["TOKEN"], rank=9,
        code="def split_text(folder, input1, output1): pass"))
    cat.save_function(e)
    return e.id


def test_candidate_mapping(make_run, capsys, seeded_catalog):
    run = approved_run(make_run, capsys)
    cands = [
        {"node": "split_words", "decision": "reuse", "catalog_id": seeded_catalog},
        {"node": "map_words", "decision": "adapt", "catalog_id": seeded_catalog,
         "adaptation_notes": "count instead", "updated_outputs": ["counts_{rank}.json"]},
        {"node": "reduce_counts", "decision": "reuse", "catalog_id": "nope"},
    ]
    code, out = call(S, ["apply-candidates", "--run", str(run), "--json", write(run, "candidates.json", cands)], capsys)
    assert code == 0
    enriched = WorkflowSpec.model_validate(state(run)["enriched_spec"])
    by = {n.name: n for n in enriched.nodes}
    # verbatim reuse is renamed to the catalog name, edges follow; rank from skeleton
    assert "split_text" in by and by["split_text"].source == "catalog" and by["split_text"].rank == 1
    assert by["split_text"].dependencies == ["numpy"] and by["split_text"].code
    assert {e.from_node for e in enriched.edges} >= {"split_text"} and enriched.entry == "split_text"
    # adapt keeps the skeleton name and takes the updated outputs
    assert by["map_words"].source == "adapt" and by["map_words"].origin_name == "split_text"
    assert [o.name for o in by["map_words"].outputs] == ["counts_{rank}.json"] and by["map_words"].rank == 3
    assert by["reduce_counts"].source == "new" and any("nope" in n for n in out["notes"])
    assert any("renamed" in r for r in out["repairs"])


def test_rename_collision_downgrades_to_adapt(make_run, capsys, seeded_catalog):
    run = approved_run(make_run, capsys)
    cands = [{"node": "split_words", "decision": "reuse", "catalog_id": seeded_catalog},
             {"node": "map_words", "decision": "reuse", "catalog_id": seeded_catalog},
             {"node": "reduce_counts", "decision": "new"}]
    code, out = call(S, ["apply-candidates", "--run", str(run), "--json", write(run, "candidates.json", cands)], capsys)
    names = [c["name"] for c in out["candidates"]]
    assert names == ["split_words", "map_words", "reduce_counts"]
    assert [c["source"] for c in out["candidates"][:2]] == ["adapt", "adapt"]


def test_cached_and_adapt_from_cache(make_run, capsys):
    prior = WorkflowSpec(name="old", nodes=[
        FunctionSpec(name="split_words", description="old split", code="def split_words(): 1"),
        FunctionSpec(name="map_words", description="old map", code="def map_words(): 2")])
    run = approved_run(make_run, capsys, prior_spec=json.loads(prior.model_dump_json()))
    cands = [{"node": "split_words", "decision": "cached"},
             {"node": "map_words", "decision": "adapt", "adapt_from": "cached", "adaptation_notes": "faster"},
             {"node": "reduce_counts", "decision": "cached"}]
    code, out = call(S, ["apply-candidates", "--run", str(run), "--json", write(run, "candidates.json", cands)], capsys)
    src = {c["name"]: (c["source"], c["origin_kind"]) for c in out["candidates"]}
    assert src == {"split_words": ("cached", "cache"), "map_words": ("adapt", "cache"),
                   "reduce_counts": ("new", None)}


def test_user_model_wins(make_run, capsys, tmp_path):
    model = tmp_path / "reduce_counts.py"
    model.write_text("def model(): return 1\n")
    run = approved_run(make_run, capsys, extra_args=["--user-model", str(model)])
    cands = [{"node": "split_words", "decision": "new"}, {"node": "map_words", "decision": "new"},
             {"node": "reduce_counts", "decision": "new", "user_model_mode": "adapt"}]
    ok(S, ["apply-candidates", "--run", str(run), "--json", write(run, "candidates.json", cands)], capsys)
    node = next(n for n in state(run)["enriched_spec"]["nodes"] if n["name"] == "reduce_counts")
    assert node["source"] == "user_provided" and node["user_model_mode"] == "adapt"
    assert node["code"].startswith("def model")


def test_candidates_must_cover_all_nodes(make_run, capsys):
    run = approved_run(make_run, capsys)
    code, out = call(S, ["apply-candidates", "--run", str(run), "--json",
                         write(run, "candidates.json", [{"node": "split_words", "decision": "new"}])], capsys)
    assert code == 2 and "map_words" in out["error"]


# ── resolve ──

RESOLVED = {
    "assumptions": ["\"count words\" -> whitespace-separated tokens, case-insensitive"],
    "name": "word_count", "description": "count words", "entry": "split_text",
    "nodes": [
        {"name": "split_text", "inputs": ["WRONG.txt"], "outputs": ["chunk_{rank}.txt"]},
        {"name": "map_words", "rank": 3, "inputs": ["chunk_{rank}.txt"], "outputs": ["counts_{rank}.json"]},
        {"name": "reduce_counts", "inputs": ["counts_1.json"], "outputs": ["totals.json"],
         "dependencies": ["json", "pandas"]},
        {"name": "plot", "inputs": ["totals.json"], "outputs": ["hist.png"]},
    ],
    "edges": [{"from_node": "split_text", "to_node": "map_words"},
              {"from_node": "map_words", "to_node": "reduce_counts"}],
    "data_flow": [
        {"from_node": "split_text", "to_node": "map_words", "file": "chunk_{rank}.txt"},
        {"from_node": "map_words", "to_node": "reduce_counts", "file": "counts_{rank}.json"},
        {"from_node": "reduce_counts", "to_node": "plot", "file": "totals.json"},
    ],
}


def resolved_run(make_run, capsys, seeded_catalog):
    sk = json.loads(json.dumps(SKELETON))
    sk["nodes"].append({"name": "plot", "description": "plot"})
    sk["edges"].append({"from_node": "reduce_counts", "to_node": "plot"})
    run = make_run()
    ok(S, ["skeleton", "--run", str(run), "--json", write(run, "skeleton.json", sk)], capsys)
    ok(S, ["approve", "--run", str(run), "--gate", "1"], capsys)
    cands = [{"node": "split_words", "decision": "reuse", "catalog_id": seeded_catalog},
             {"node": "map_words", "decision": "new"}, {"node": "reduce_counts", "decision": "new"},
             {"node": "plot", "decision": "new"}]
    ok(S, ["apply-candidates", "--run", str(run), "--json", write(run, "candidates.json", cands)], capsys)
    resolved = json.loads(json.dumps(RESOLVED))
    resolved["edges"] = resolved["edges"][:2]  # drop reduce->plot: plot is an orphan data consumer
    return run, call(S, ["merge-resolved", "--run", str(run), "--json",
                         write(run, "resolved.json", resolved)], capsys)


def test_merge_restores_catalog_and_repairs(make_run, capsys, seeded_catalog):
    run, (code, out) = resolved_run(make_run, capsys, seeded_catalog)
    assert code == 0, out
    spec = WorkflowSpec.model_validate(state(run)["spec"])
    by = {n.name: n for n in spec.nodes}
    assert [i.name for i in by["split_text"].inputs] == ["words.txt"]  # catalog I/O restored
    assert by["split_text"].code and by["split_text"].source == "catalog"
    assert by["reduce_counts"].dependencies == ["pandas"]
    assert any(e.from_node == "reduce_counts" and e.to_node == "plot" for e in spec.edges)
    assert any("added trigger edge reduce_counts → plot" in r for r in out["repairs"])
    assert out["errors"] == 0 and state(run)["stage"] == "gate2"


def test_merge_rejects_node_mismatch(make_run, capsys, seeded_catalog):
    run, _ = resolved_run(make_run, capsys, seeded_catalog)
    bad = json.loads(json.dumps(RESOLVED))
    bad["nodes"] = bad["nodes"][:3]
    code, out = call(S, ["merge-resolved", "--run", str(run), "--json", write(run, "resolved.json", bad)], capsys)
    assert code == 2 and "plot" in out["error"]


# ── pure helpers ──

def test_prune_keeps_conditional_edges():
    spec = WorkflowSpec(name="w", nodes=[FunctionSpec(name=n) for n in "abc"], edges=[
        {"from_node": "a", "to_node": "b"}, {"from_node": "b", "to_node": "c"},
        {"from_node": "a", "to_node": "c", "condition": "True"}])
    log = []
    assert len(S.prune_transitive_edges(spec, log).edges) == 3 and log == []


def test_check_finds_problems():
    spec = WorkflowSpec(name="w", entry="a", nodes=[
        FunctionSpec(name="a", outputs=[IOSpec(name="x_{part}.csv")]),
        FunctionSpec(name="b", rank=2, inputs=[IOSpec(name="y.csv")], outputs=[IOSpec(name="z.csv")]),
        FunctionSpec(name="c", outputs=[IOSpec(name="q.csv")])],
        edges=[{"from_node": "a", "to_node": "b"}],
        data_flow=[{"from_node": "a", "to_node": "b", "file": "y.csv"},
                   {"from_node": "b", "to_node": "a", "file": "z.csv"}])
    msgs = " | ".join(p["message"] for p in S.check_spec(spec))
    assert "{part}" in msgs and "instances would overwrite" in msgs
    assert "not among a's outputs" in msgs and "can run before" in msgs and "no trigger" in msgs


def test_toposort_and_ancestors():
    spec = WorkflowSpec(name="w", nodes=[FunctionSpec(name=n) for n in "dcba"], edges=[
        {"from_node": "a", "to_node": "b"}, {"from_node": "b", "to_node": "c"},
        {"from_node": "a", "to_node": "d"}])
    order = S.topo_order(spec)
    assert order.index("a") < order.index("b") < order.index("c")
    assert S.ancestors(spec, "c") == ["a", "b"]


# ── directives and approvals ──

def test_directive_routing_and_cap(make_run, capsys):
    spec = WorkflowSpec(name="w", nodes=[FunctionSpec(name="a", code="def a(): pass")])
    run = make_run(spec=spec, stage="gate3", max_iterations=2)
    code, out = call(S, ["directive", "--run", str(run), "--gate", "3", "--text", "use pandas",
                         "--structural", "false"], capsys)
    s = state(run)
    assert out["next_stage"] == "reuse" and s["code_feedback"] == "use pandas"  # v2: functional → reuse
    assert s["prior_spec"]["nodes"][0]["code"] and not out["cap_reached"]
    code, out = call(S, ["directive", "--run", str(run), "--gate", "2", "--text", "add a node",
                         "--structural", "true"], capsys)
    s = state(run)
    assert out["next_stage"] == "skeleton" and s["spec_is_seed"] and not out["cap_reached"]
    assert s["revision_directives"] == ["use pandas", "add a node"]
    assert len(list((run / "history").glob("*.json"))) == 2
    # v2: at the cap the directive is refused before anything changes
    code, out = call(S, ["directive", "--run", str(run), "--gate", "2", "--text", "third",
                         "--structural", "false"], capsys)
    assert code == 1 and out["cap_reached"] and state(run) == s
    code, out = call(S, ["directive", "--run", str(run), "--gate", "2", "--text", "x"], capsys)
    assert code == 2


def test_gate4_requires_validation_and_reports_reuse(make_run, capsys):
    spec = WorkflowSpec(name="w", nodes=[FunctionSpec(name="a", source="catalog", catalog_id="c1"),
                                         FunctionSpec(name="b", source="new")])
    run = make_run(spec=spec)
    assert call(S, ["approve", "--run", str(run), "--gate", "4"], capsys)[0] == 2
    s = state(run)
    s["validation"] = {"ok": True, "level": "structural", "error": None,
                       "spec_hash": spec_hash(s["spec"])}  # v2: the bundle must match the spec
    (run / "state.json").write_text(json.dumps(s))
    code, out = call(S, ["approve", "--run", str(run), "--gate", "4"], capsys)
    assert code == 0 and out["reused_catalog_ids"] == ["c1"] and out["stage"] == "finalize"


def test_show_and_prompt_context(make_run, capsys, seeded_catalog):
    run, _ = resolved_run(make_run, capsys, seeded_catalog)
    code, out = call(S, ["show", "--run", str(run), "--gate", "2"], capsys)
    md = out["markdown"]
    assert "[catalog] ← [catalog] split_text" in md and "map_words ×3" in md and "`workflow_data/words.txt`" in md
    code, ctx = call(S, ["prompt-context", "--run", str(run), "--stage", "mca"], capsys)
    node = next(n for n in ctx["nodes"] if n["name"] == "split_text")
    # one-entry catalog: the inherited BM25 quirk returns no matches (see test_catalog)
    assert node["catalog_matches"] == [] and node["catalog_query"].startswith("split text")
    assert "Original user request (verbatim): count words" in ctx["workflow_context"]
    code, ctx = call(S, ["prompt-context", "--run", str(run), "--stage", "resolve"], capsys)
    assert "[CATALOG]" in ctx["fca_candidates"]
    nodes = ctx["workflow_json"]["nodes"]
    assert len(nodes) == 4 and all("code" not in n for n in nodes)


def test_preflight_requires_foreground_variable(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", raising=False)
    code, out = call(S, ["preflight"], capsys)
    assert code == 1 and "`curate` shell alias" in out["error"]
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "0")
    assert call(S, ["preflight"], capsys)[0] == 1
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "1")
    code, out = call(S, ["preflight"], capsys)
    assert code == 0 and out["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_status_counts_auto_emit_retries(make_run, capsys):
    spec = WorkflowSpec(name="w", nodes=[FunctionSpec(name="a", code="def a(): pass")])
    run = make_run(spec=spec, history=[{"gate": "emit", "decision": "revise"},
                                       {"gate": 3, "decision": "approve"},
                                       {"gate": "emit", "decision": "revise"}])
    assert ok(S, ["status", "--run", str(run)], capsys)["auto_emit_retries"] == 1


def test_gate4_display_flags_missing_code_and_failures(make_run, capsys):
    spec = WorkflowSpec(name="w", nodes=[FunctionSpec(name="a", code="def a(): pass"), FunctionSpec(name="b")])
    run = make_run(spec=spec, validation={"ok": True, "level": "faasr_py", "error": None},
                   mga={"b": {"status": "failed"}})
    md = ok(S, ["show", "--run", str(run), "--gate", "4"], capsys)["markdown"]
    assert "functions without code (not in the bundle): b" in md and "stub test failed: b" in md


def test_gate4_says_deploy_fills_the_placeholders(make_run, capsys, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "README.md").write_text("- `<FAASR_GH_USERNAME>`\n- `<FAASR_S3_BUCKET>`\n- `<FAASR_S3_REGION>`\n")
    spec = WorkflowSpec(name="w", nodes=[FunctionSpec(name="a", code="def a(): pass")])
    run = make_run(spec=spec, bundle_dir=str(bundle), validation={"ok": True, "level": "faasr_py", "error": None})
    md = ok(S, ["show", "--run", str(run), "--gate", "4"], capsys)["markdown"]
    assert ("- deploying: Placeholders are filled from your settings at deploy; run "
            "`/curate-skills:curate-doctor --deploy --online` if you haven't.") in md
    for outdated in ("placeholders to fill", "Secrets to set before deploying", "<FAASR_GH_USERNAME>"):
        assert outdated not in md


def test_summary_markdown(make_run, capsys):
    spec = WorkflowSpec(name="w", description="demo", entry="a",
                        nodes=[FunctionSpec(name="a", code="x\ny\n", dependencies=["numpy"]),
                               FunctionSpec(name="b", rank=2, source="catalog", origin_kind="catalog",
                                            origin_name="b0", code="z")],
                        edges=[{"from_node": "a", "to_node": "b"}])
    run = make_run(spec=spec, stage="finalize", mga={"a": {"status": "passed", "attempts": 2}},
                   validation={"ok": True, "level": "faasr_py", "error": None},
                   history=[{"gate": 3, "decision": "revise", "text": "use numpy", "structural": False}])
    md = ok(S, ["summary", "--run", str(run)], capsys)["markdown"]
    assert "| `a` | [new] | 1 | passed | 2 | 2 | numpy | — |" in md
    assert "| `b` | [catalog] ← [catalog] b0 | 2 | — | 0 | 1 |" in md
    assert "Gate 3: revise — use numpy" in md and "validation passed (level `faasr_py`)" in md


def test_gate3_attention_shows_last_line(make_run, capsys):
    spec = WorkflowSpec(name="w", nodes=[FunctionSpec(name="a", code="x")])
    tail = "=== rank 1/1 ===\nTraceback (most recent call last):\n  File x\nValueError: bad input\n"
    run = make_run(spec=spec, mga={"a": {"status": "failed", "attempts": 1, "last_output_tail": tail}})
    md = ok(S, ["show", "--run", str(run), "--gate", "3"], capsys)["markdown"]
    assert "  last test lines:\n    Traceback (most recent call last):\n      File x\n    ValueError: bad input" in md
