import json
from pathlib import Path

import pytest

import scratch
from conftest import call, ok
from curate_models import FunctionSpec, IOSpec, WorkflowSpec


def state(run):
    return json.loads((Path(run) / "state.json").read_text())


def spec_with_sources(tmp_path):
    model = tmp_path / "model.py"
    model.write_text("def model(x): return x\n")
    return WorkflowSpec(
        name="w", entry="load",
        nodes=[
            FunctionSpec(name="load", inputs=[IOSpec(name="raw.csv")], outputs=[IOSpec(name="chunk_{rank}.csv")],
                         source="catalog", code="def load(folder, input1, output1): pass",
                         dependencies=["pandas", "os"]),
            FunctionSpec(name="work", rank=2, inputs=[IOSpec(name="chunk_{rank}.csv")],
                         outputs=[IOSpec(name="out_{rank}.csv")], source="new"),
            FunctionSpec(name="model", inputs=[IOSpec(name="out_1.csv")], outputs=[IOSpec(name="m.json")],
                         source="user_provided", user_model_mode="verbatim", code=model.read_text()),
            FunctionSpec(name="tweak", inputs=[IOSpec(name="m.json")], outputs=[IOSpec(name="t.json")],
                         source="adapt", origin_kind="catalog", origin_name="old_tweak",
                         code="def tweak(folder, input1, output1): pass"),
        ],
        edges=[{"from_node": "load", "to_node": "work"}, {"from_node": "work", "to_node": "model"},
               {"from_node": "model", "to_node": "tweak"}],
        data_flow=[{"from_node": "load", "to_node": "work", "file": "chunk_{rank}.csv"}],
    )


@pytest.fixture
def built(make_run, tmp_path, capsys):
    raw = tmp_path / "raw.csv"
    raw.write_text("a,b\n1,2\n")
    shard = tmp_path / "chunk_2.csv"
    shard.write_text("x\n")
    run = make_run(spec=spec_with_sources(tmp_path),
                   context_files=[{"name": "raw.csv", "path": str(raw), "description": ""},
                                  {"name": "chunk_2.csv", "path": str(shard), "description": ""}])
    stale = run / "functions" / "ghost"
    stale.mkdir(parents=True)
    code, out = call(scratch, ["build", "--run", str(run)], capsys)
    assert code == 0, out
    return run, out


def test_build_layout(built):
    run, out = built
    fns = run / "functions"
    assert sorted(p.name for p in fns.iterdir()) == ["load", "model", "tweak", "work"]  # stale removed
    assert out["order"] == ["load", "work", "model", "tweak"]
    assert (run / "CONTEXT.md").exists() and not (run / "stubs").exists()  # no FaaSr stubs any more
    assert (run / "context" / "raw.csv").exists()
    # reused catalog node: code + cleaned deps, no spec.md
    assert (fns / "load" / "load.py").exists() and not (fns / "load" / "spec.md").exists()
    assert (fns / "load" / "load.deps.txt").read_text() == "pandas\n"
    assert (fns / "load" / "test_data" / "raw.csv").exists()
    # ranked consumer gets matching shard; user model gets original_<name>.py
    assert (fns / "work" / "test_data" / "chunk_2.csv").exists()
    assert (fns / "model" / "original_model.py").exists() and not (fns / "model" / "model.py").exists()
    assert (fns / "tweak" / "tweak.py").exists()
    s = state(run)
    assert {k: v["status"] for k, v in s["mga"].items()} == {
        "load": "reused", "work": "pending", "model": "pending", "tweak": "pending"}


def test_spec_md_content(built):
    run, _ = built
    work = (run / "functions" / "work" / "spec.md").read_text()
    assert "Parallel Execution (RANK = 2)" in work and "<plugin>/scripts/stubtest.py run --run" in work
    assert f"Your folder (the only place you may write): `{run}/functions/work`" in work
    load_ctx = (run / "CONTEXT.md").read_text()
    assert "Every rank 1..N is run" in load_ctx and "count words" in load_ctx
    model = (run / "functions" / "model" / "spec.md").read_text()
    assert "Wrap Verbatim" in model and "parity_report.txt" in model
    tweak = (run / "functions" / "tweak" / "spec.md").read_text()
    assert "Previous Implementation" in tweak and "old_tweak" in tweak


def test_lock_unlock(built, capsys):
    run, _ = built
    code, out = call(scratch, ["lock", "--run", str(run), "--node", "work"], capsys)
    lock = json.loads((Path(state(run)["project_dir"]) / ".curate" / "mga.lock.json").read_text())
    assert code == 0 and lock["allowed_dir"] == str((run / "functions" / "work").resolve())
    code, out = call(scratch, ["lock", "--run", str(run), "--node", "model"], capsys)
    assert code == 1 and out["lock"]["node"] == "work"
    assert call(scratch, ["lock", "--run", str(run), "--node", "model", "--force"], capsys)[0] == 0
    code, out = call(scratch, ["unlock", "--run", str(run)], capsys)
    assert out["removed"] and call(scratch, ["unlock", "--run", str(run)], capsys)[1]["removed"] is False
    assert call(scratch, ["lock", "--run", str(run), "--node", "ghost"], capsys)[0] == 2


def test_snapshot_and_verify_writes(built, capsys, tmp_path):
    run, _ = built
    ok(scratch, ["snapshot", "--run", str(run), "--node", "work"], capsys)
    (run / "functions" / "work" / "work.py").write_text("ok")
    (run / "functions" / "work" / "test_output" / "x").write_text("ok")
    code, out = call(scratch, ["verify-writes", "--run", str(run), "--node", "work"], capsys)
    assert code == 0 and out["violations"] == []

    ok(scratch, ["snapshot", "--run", str(run), "--node", "work"], capsys)
    (run / "functions" / "load" / "load.py").write_text("tampered")
    (run / "CONTEXT.md").unlink()
    (Path(state(run)["project_dir"]) / "stray.txt").write_text("x")
    code, out = call(scratch, ["verify-writes", "--run", str(run), "--node", "work"], capsys)
    changes = {(v["area"], v["change"], Path(v["path"]).name) for v in out["violations"]}
    assert code == 1 and changes == {("run", "modified", "load.py"), ("run", "removed", "CONTEXT.md"),
                                     ("project", "added", "stray.txt")}
    assert state(run)["mga"]["work"]["violations"]


def test_verify_detects_catalog_writes(built, capsys):
    run, _ = built
    cat = Path(state(run)["catalog_dir"])
    cat.mkdir(parents=True, exist_ok=True)
    ok(scratch, ["snapshot", "--run", str(run), "--node", "work"], capsys)
    (cat / "evil.json").write_text("{}")
    code, out = call(scratch, ["verify-writes", "--run", str(run), "--node", "work"], capsys)
    assert code == 1 and out["violations"][0]["area"] == "catalog"


def test_harvest_and_record(built, capsys):
    run, _ = built
    d = run / "functions" / "work"
    code, out = call(scratch, ["harvest", "--run", str(run), "--node", "work"], capsys)
    assert code == 1  # nothing produced yet
    (d / "work.py").write_text('import os\n\ndef main(inputs, outputs, params):\n    k = os.environ["API_KEY"]\n')
    (d / "work.deps.txt").write_text("# deps\nnumpy\njson\nfaasr\n")
    code, out = call(scratch, ["harvest", "--run", str(run), "--node", "work"], capsys)
    assert code == 0 and out["secrets"] == ["API_KEY"] and out["dependencies"] == ["numpy"]
    node = next(n for n in state(run)["spec"]["nodes"] if n["name"] == "work")
    assert node["code"].startswith("import os") and node["dependencies"] == ["numpy"]

    (d / "stubtest.log").write_text("boom")
    code, out = call(scratch, ["record", "--run", str(run), "--node", "work", "--status", "failed",
                               "--attempts", "2"], capsys)
    m = state(run)["mga"]["work"]
    assert m["status"] == "failed" and m["attempts"] == 2 and m["last_output_tail"] == "boom"
    assert call(scratch, ["record", "--run", str(run), "--node", "work", "--status", "great"], capsys)[0] == 2


def test_fan_out_text_depends_on_rank_placeholder(make_run, capsys):
    def spec_md(handed_file):
        wf = WorkflowSpec(
            name="w", entry="src",
            nodes=[FunctionSpec(name="src", outputs=[IOSpec(name=handed_file)]),
                   FunctionSpec(name="par", rank=3, inputs=[IOSpec(name=handed_file)],
                                outputs=[IOSpec(name="o_{rank}.txt")])],
            edges=[{"from_node": "src", "to_node": "par"}],
            data_flow=[{"from_node": "src", "to_node": "par", "file": handed_file}])
        run = make_run(spec=wf)
        ok(scratch, ["build", "--run", str(run)], capsys)
        return (run / "functions" / "src" / "spec.md").read_text()

    shared = spec_md("numbers.txt")
    assert "reads one shared file" in shared and "Write it exactly once" in shared
    assert "Fan-out to ranked successor" not in shared
    sharded = spec_md("chunk_{rank}.txt")
    assert "Fan-out to ranked successor `par` (×3)" in sharded and "reads one shared file" not in sharded
    assert '`outputs["output1"]` is a LIST of 3 paths' in sharded
    assert "a LIST of 3 paths in rank order: write EVERY one (path i is read by rank i of par)" in sharded


def plain_mapreduce_md(make_run, capsys):
    from test_pegasus_ir import e2_spec as plain_e2_spec
    wf = plain_e2_spec()
    for n in wf.nodes:  # all to generate, so each gets a spec.md
        n.source, n.code = "new", None
    wf.nodes[2].secrets = ["API_KEY"]
    run = make_run(spec=wf)
    ok(scratch, ["build", "--run", str(run)], capsys)
    md = {n.name: (run / "functions" / n.name / "spec.md").read_text() for n in wf.nodes}
    return run, md


def test_spec_md_describes_the_plain_slots(make_run, capsys):
    run, md = plain_mapreduce_md(make_run, capsys)
    assert "def main(inputs: dict, outputs: dict, params: dict) -> None" in md["map"]
    assert 'inputs["input1"]   text_batch_{rank}.txt' in md["map"]
    assert "a path: THIS rank's copy (text_batch_2.txt for rank 2)" in md["map"]
    assert 'params["rank"]` is THIS instance\'s rank (1..3)' in md["map"]
    assert 'main({"input1": "stub_in/r1/text_batch_1.txt"}, {"output1": "test_output/map_counts_1.json"}, ' \
           '{"rank": 1, "max_rank": 3})' in md["map"]
    assert 'read with os.environ["API_KEY"]' in md["map"]
    assert ('a FOLDER: a directory holding every file whose name starts with "map_counts_"; list it with '
            "os.listdir") in md["shuffle"]
    assert "Fan-in from ranked predecessor `map` (×3)" in md["shuffle"]
    assert ("don't filter by name, it holds only these files). If it is EMPTY, raise: no files means "
            "the steps before this one wrote nothing") in md["shuffle"]
    assert "If that input holds NO files, raise an error naming it" in md["shuffle"]
    assert "a LIST of 5 paths in rank order: write EVERY one (path i is read by rank i of reduce)" in md["shuffle"]
    assert "a LIST of 3 paths in rank order" in md["split"]
    ctx = (run / "CONTEXT.md").read_text()
    api = ("faasr_get_file", "faasr_put_file", "faasr_rank", "faasr_log", "faasr_secret",
           "faasr_get_folder_list", "remote_file", "remote_folder", "faasr-reference")
    for text in [ctx, *md.values()]:  # nothing tells the MGA to use the FaaSr API any more
        assert not [a for a in api if a in text], [a for a in api if a in text]
    assert "no faasr_* calls or imports" in ctx and "function-reference" in ctx


def test_spec_md_list_input_and_conditional_return(make_run, capsys):
    wf = WorkflowSpec(
        name="w", entry="a",
        nodes=[FunctionSpec(name="a", rank=2, outputs=[IOSpec(name="p_{rank}.txt")]),
               FunctionSpec(name="g", inputs=[IOSpec(name="p_{rank}.txt")], outputs=[IOSpec(name="s.txt")]),
               FunctionSpec(name="yes"), FunctionSpec(name="no")],
        edges=[{"from_node": "a", "to_node": "g"}, {"from_node": "g", "to_node": "yes", "condition": "True"},
               {"from_node": "g", "to_node": "no", "condition": "False"}])
    run = make_run(spec=wf)
    ok(scratch, ["build", "--run", str(run)], capsys)
    g = (run / "functions" / "g" / "spec.md").read_text()
    assert "a LIST of 2 paths, every copy in rank order 1..2" in g
    assert "-> bool" in g and "`False` runs no; `True` runs yes" in g


def test_seed_in_the_older_faasr_style_is_converted(built):
    run, _ = built
    tweak = (run / "functions" / "tweak" / "spec.md").read_text()
    assert "OLDER FaaSr style" in tweak and "Convert it to `main(inputs, outputs, params)`" in tweak


def test_extract_secrets():
    code = 'import os\nk = os.environ["API_KEY"]\nh = os.environ["HOME"]\nt = os.getenv("TOKEN_2")\n'
    assert scratch.extract_secrets(code) == ["API_KEY", "TOKEN_2"]


def test_record_keeps_empty_external_inputs(built, capsys):
    run, _ = built
    d = run / "functions" / "work"
    (d / "stubtest.log").write_text(json.dumps({"ok": True, "empty_external_inputs": ["raw.csv"]}) + "\n\nlines")
    ok(scratch, ["record", "--run", str(run), "--node", "work", "--status", "passed"], capsys)
    assert state(run)["mga"]["work"]["empty_external_inputs"] == ["raw.csv"]


def test_build_resume_keeps_finished_nodes(built, capsys):
    run, _ = built
    (run / "functions" / "work" / "work.py").write_text("def work(): 'done'\n")
    (run / "functions" / "model" / "model.py").write_text("partial")
    ok(scratch, ["record", "--run", str(run), "--node", "work", "--status", "passed", "--attempts", "1"], capsys)
    out = ok(scratch, ["build", "--run", str(run), "--resume"], capsys)
    kept = {n["name"]: n.get("kept") for n in out["nodes"]}
    assert kept["work"] == "passed" and kept["model"] is None
    assert (run / "functions" / "work" / "work.py").read_text() == "def work(): 'done'\n"
    assert not (run / "functions" / "model" / "model.py").exists()  # unfinished node rebuilt
    m = state(run)["mga"]
    assert m["work"]["status"] == "passed" and m["model"]["status"] == "pending"
    ok(scratch, ["build", "--run", str(run)], capsys)  # without --resume everything is rebuilt
    assert not (run / "functions" / "work" / "work.py").exists()


def test_spec_md_stores_no_plugin_path(built):
    run, _ = built
    text = (run / "functions" / "work" / "spec.md").read_text()
    plugin = str(Path(__file__).resolve().parents[1])
    assert f'uv run --quiet <plugin>/scripts/stubtest.py run --run "{run}" --node work' in text
    assert plugin not in text  # not the checkout, nor a versioned plugin-cache path
