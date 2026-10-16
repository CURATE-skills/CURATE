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
    assert (run / "CONTEXT.md").exists() and (run / "stubs" / "faasr_stubs.py").exists()
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
    assert "Parallel Execution (RANK = 2)" in work and "stubtest.py\" run --run" in work
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
    (d / "work.py").write_text('def work(folder, input1, output1):\n    k = faasr_secret("API_KEY")\n')
    (d / "work.deps.txt").write_text("# deps\nnumpy\njson\nfaasr\n")
    code, out = call(scratch, ["harvest", "--run", str(run), "--node", "work"], capsys)
    assert code == 0 and out["secrets"] == ["API_KEY"] and out["dependencies"] == ["numpy"]
    node = next(n for n in state(run)["spec"]["nodes"] if n["name"] == "work")
    assert node["code"].startswith("def work") and node["dependencies"] == ["numpy"]

    (d / "stubtest.log").write_text("boom")
    code, out = call(scratch, ["record", "--run", str(run), "--node", "work", "--status", "failed",
                               "--attempts", "2"], capsys)
    m = state(run)["mga"]["work"]
    assert m["status"] == "failed" and m["attempts"] == 2 and m["last_output_tail"] == "boom"
    assert call(scratch, ["record", "--run", str(run), "--node", "work", "--status", "great"], capsys)[0] == 2
