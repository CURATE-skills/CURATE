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
from ir_local import RUNNERS, run_locally  # noqa: E402

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

def test_contract_faasr_is_a_warning_and_unknown_contract_an_error(tmp_path):
    ir = ir_dict(tmp_path, [{"id": "a", "contract": "faasr", "entrypoint": "a", "outputs": {"o": "o.txt"}},
                            {"id": "b", "contract": "wasm", "inputs": {"i": "o.txt"}, "outputs": {"o": "p.txt"}}],
                 outputs=[{"file": "p.txt"}])
    (tmp_path / "functions" / "a.py").write_text(
        "def a(folder, output1):\n    faasr_put_file(local_file='x', remote_folder=folder, remote_file=output1)\n")
    _, errors, warnings, _ = validate.prepare(ir)
    assert errors == ["b: contract must be one of plain, faasr, got 'wasm'"]
    assert any(w.startswith("contract faasr (a):") for w in warnings)


def test_backend_has_a_runner_per_contract():
    src = (IR_DIR / "backends" / "pegasus_backend.py").read_text()
    assert 'RUNNERS = {"plain": SHIM, "faasr": HERE.parent / "runner" / "curate_faasr_run.py"}' in src
    assert set(validate.CONTRACTS) == set(RUNNERS)
    assert all(p.is_file() for p in RUNNERS.values())


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
