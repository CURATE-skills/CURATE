"""curate_ir/: the IR validator, the v0.2 fan-out expander and the node runner, without Pegasus.

The examples run through the real runner shim (curate_ir/runner/curate_run.py) in a small local
executor that does what Pegasus's condorio staging does: each job runs in its own empty folder,
gets its input files copied in under their flat names, and its outputs are picked up by name.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
IR_DIR = PLUGIN / "curate_ir"
EXAMPLES = IR_DIR / "examples"
RUNNER = IR_DIR / "runner" / "curate_run.py"
sys.path.insert(0, str(IR_DIR))

import expand  # noqa: E402
import validate  # noqa: E402

yaml = pytest.importorskip("yaml")


def ir_dict(tmp_path, nodes, **extra):
    """A minimal IR whose node code files exist (plain main)."""
    fdir = tmp_path / "functions"
    fdir.mkdir(exist_ok=True)
    for n in nodes:
        n.setdefault("code", f"functions/{n['id']}.py")
        (tmp_path / n["code"]).write_text("def main(inputs, outputs, params):\n    pass\n")
    return {"ir_version": "0.2", "name": "t", "defaults": {"image": "docker:///python:3.12-slim"},
            "inputs": [], "nodes": nodes, "_base": str(tmp_path), **extra}


# ── loading and validation ──

@pytest.mark.parametrize("example", ["diamond", "fanout"])
def test_examples_valid_and_relative_inputs_resolve(example):
    ir = validate.load(EXAMPLES / example / "ir.yaml")
    (inp,) = ir["inputs"]
    assert inp["location"] == "file://" + str(EXAMPLES / example / "input" / "params.json")
    assert Path(inp["location"][len("file://"):]).is_file()
    _, errors, warnings, _ = validate.prepare(ir)
    assert errors == []
    assert not any("absolute path" in w for w in warnings)


def test_no_example_names_a_home_folder():
    for f in EXAMPLES.rglob("*"):
        if f.is_file():
            assert "/home/" not in f.read_text() and "/Users/" not in f.read_text(), f


def test_absolute_and_url_locations_unchanged(tmp_path):
    f = tmp_path / "ir.json"
    f.write_text('{"ir_version": "0.1", "name": "x", "inputs": ['
                 '{"file": "a", "location": "/abs/a"}, {"file": "b", "location": "s3://bkt/b"},'
                 '{"file": "c", "location": "https://h/c"}, {"file": "d", "location": "sub/d.csv"}]}')
    locs = [i["location"] for i in validate.load(f)["inputs"]]
    assert locs == ["/abs/a", "s3://bkt/b", "https://h/c", "file://" + str(tmp_path / "sub" / "d.csv")]


def test_faasr_calls_are_an_error_for_plain_functions(tmp_path):
    ir = ir_dict(tmp_path, [{"id": "a", "outputs": {"o": "o.txt"}}], outputs=[{"file": "o.txt"}])
    (tmp_path / "functions" / "a.py").write_text(
        "def main(inputs, outputs, params):\n    faasr_put_file('x', 'f', 'o.txt')\n")
    _, errors, _, _ = validate.prepare(ir)
    assert any("calls FaaSr API" in e for e in errors)


def test_missing_producer_and_cycle(tmp_path):
    ir = ir_dict(tmp_path, [{"id": "a", "inputs": {"i": "b.txt"}, "outputs": {"o": "a.txt"}},
                            {"id": "b", "inputs": {"i": "a.txt"}, "outputs": {"o": "b.txt"}},
                            {"id": "c", "inputs": {"i": "nowhere.txt"}, "outputs": {"o": "c.txt"}}],
                 outputs=[{"file": "c.txt"}])
    _, errors, _, _ = validate.prepare(ir)
    assert any("nowhere.txt" in e and "no producer" in e for e in errors)
    assert any(e.startswith("cycle in dataflow") for e in errors)


# ── v0.2 fan-out ──

def test_fanout_expands_and_gathers_in_axis_order():
    ir = validate.load(EXAMPLES / "fanout" / "ir.yaml")
    exp, errors = expand.expand(ir)
    assert errors == []
    ids = [n["id"] for n in exp["nodes"]]
    assert len(ids) == 10 and "detect_ice__2019" in ids and "add_duration__2022" in ids
    copy = next(n for n in exp["nodes"] if n["id"] == "add_duration__2020")
    assert copy["inputs"] == {"ice": "ice_2020.json"} and copy["params"]["winter"] == "2020"
    gather = next(n for n in exp["nodes"] if n["id"] == "assemble")
    assert gather["inputs"]["rows"] == [f"row_{w}.json" for w in (2019, 2020, 2021, 2022)]


def test_output_on_an_axis_the_node_does_not_fan_out_over_is_refused(tmp_path):
    # One step writing one file per value without for_each: today's IR refuses it; the CURATE
    # emitter lists such outputs one by one instead.
    ir = ir_dict(tmp_path, [{"id": "split", "outputs": {"b": "batch_{r}.txt"}}], axes={"r": [1, 2]})
    _, errors = expand.expand(ir)
    assert any("does not fan out over it" in e for e in errors)


def test_fanout_output_must_name_its_axis(tmp_path):
    ir = ir_dict(tmp_path, [{"id": "m", "for_each": ["r"], "outputs": {"o": "same.txt"}}], axes={"r": [1, 2]})
    _, errors = expand.expand(ir)
    assert any("must contain {r}" in e for e in errors)


# ── the runner shim, with condorio-style staging ──

def run_locally(ir_path: Path, work: Path) -> dict:
    """Run every expanded node through curate_run.py, each in its own job folder. Returns
    {file name: path} of everything produced."""
    ir, errors, _, deps = validate.prepare(validate.load(ir_path))
    assert errors == []
    base = Path(ir["_base"])
    files = {i["file"]: Path(i["location"][len("file://"):]) for i in ir["inputs"]}
    nodes = {n["id"]: n for n in ir["nodes"]}
    done = set()
    while len(done) < len(nodes):
        ready = [k for k in nodes if k not in done and deps[k] <= done]
        assert ready, "no node ready: cycle?"
        for nid in ready:
            n = nodes[nid]
            job = work / nid
            job.mkdir(parents=True)
            code = job / ("code__" + Path(n["code"]).name)
            shutil.copy(base / n["code"], code)
            args = [sys.executable, str(RUNNER), "--code", code.name]
            for name, v in n["inputs"].items():
                for f in validate.files_of(v):
                    shutil.copy(files[f], job / f)
                    args += ["--in-list" if isinstance(v, list) else "--in", f"{name}={f}"]
            for name, f in n["outputs"].items():
                args += ["--out", f"{name}={f}"]
            import json
            args += ["--params-json", json.dumps(n.get("params", {}))]
            p = subprocess.run(args, cwd=job, capture_output=True, text=True, timeout=60)
            assert p.returncode == 0, (nid, p.stderr)
            for f in n["outputs"].values():
                files[f] = job / f
            done.add(nid)
    return files


def test_diamond_and_fanout_give_the_same_result(tmp_path):
    d = run_locally(EXAMPLES / "diamond" / "ir.yaml", tmp_path / "diamond")
    f = run_locally(EXAMPLES / "fanout" / "ir.yaml", tmp_path / "fanout")
    a = d["ice_phenology_result.json"].read_bytes()
    assert a and a == f["ice_phenology_result.json"].read_bytes()


def test_runner_refuses_missing_inputs_and_unwritten_outputs(tmp_path):
    code = tmp_path / "fn.py"
    code.write_text("def main(inputs, outputs, params):\n    pass\n")
    p = subprocess.run([sys.executable, str(RUNNER), "--code", str(code), "--in", "a=missing.txt"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0 and "declared inputs not present" in p.stderr
    p = subprocess.run([sys.executable, str(RUNNER), "--code", str(code), "--out", "o=o.txt"],
                       cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert p.returncode != 0 and "declared outputs not written" in p.stderr
