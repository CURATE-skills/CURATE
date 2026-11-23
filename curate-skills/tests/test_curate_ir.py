"""curate_ir/: the IR validator, the v0.2 fan-out expander and the node runner, without Pegasus.

The examples run through the real runner shim (curate_ir/runner/curate_run.py) in a small local
executor that does what Pegasus's condorio staging does: each job runs in its own empty folder,
gets its input files copied in under their flat names, and its outputs are picked up by name.
"""
import json
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
from ir_local import RUNNER, run_locally  # noqa: E402

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
    assert any("calls the FaaSr API" in e for e in errors)


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


def test_output_on_an_axis_the_node_does_not_fan_out_over_is_a_list(tmp_path):
    # One step writing one file per value without for_each (a fan-out writer): its output is the
    # list of every copy, the write-side mirror of a gather.
    ir = ir_dict(tmp_path, [{"id": "split", "outputs": {"b": "batch_{r}.txt"}},
                            {"id": "use", "for_each": ["r"], "inputs": {"b": "batch_{r}.txt"},
                             "outputs": {"o": "o_{r}.txt"}}], axes={"r": [1, 2]})
    exp, errors = expand.expand(ir)
    assert errors == []
    split = next(n for n in exp["nodes"] if n["id"] == "split")
    assert split["outputs"] == {"b": ["batch_1.txt", "batch_2.txt"]}
    _, errors, _, deps = validate.prepare(ir)
    assert errors == [] and deps["use__2"] == {"split"}


def test_misspelled_rank_files_are_still_reported(tmp_path):
    """List outputs replaced an error that, by accident, also caught typos: each typo still gets
    a clear message."""
    # misspelled axis: an error, as before
    ir = ir_dict(tmp_path, [{"id": "s", "outputs": {"b": "batch_{rr}.txt"}}], axes={"r": [1, 2]})
    _, errors, _, _ = validate.prepare(ir)
    assert "s output: placeholder {rr} is not a declared axis" in errors
    # misspelled on the writer's side, read by a consumer: the consumer's files have no producer
    ir = ir_dict(tmp_path, [{"id": "s", "outputs": {"b": "btch_{r}.txt"}},
                            {"id": "u", "for_each": ["r"], "inputs": {"b": "batch_{r}.txt"}, "outputs": {"o": "o_{r}"}}],
                 axes={"r": [1, 2]})
    _, errors, _, _ = validate.prepare(ir)
    assert "file batch_2.txt (used by u__2) has no producer and is not an input" in errors
    # misspelled with no consumer at all: a warning names it instead of a silent list output
    ir = ir_dict(tmp_path, [{"id": "s", "outputs": {"b": "btch_{r}.txt", "m": "main.txt"}},
                            {"id": "u", "inputs": {"m": "main.txt"}, "outputs": {"o": "o"}}],
                 axes={"r": [1, 2]}, outputs=[{"file": "o"}])
    _, errors, warnings, _ = validate.prepare(ir)
    assert errors == []
    assert warnings == ["s: output b writes 2 files (btch_1.txt, btch_2.txt) that no node reads and no output "
                        "delivers; is the file name misspelled?"]
    # a list output that is delivered is fine
    ir["outputs"].append({"file": "btch_{r}.txt"})
    _, _, warnings, _ = validate.prepare(ir)
    assert warnings == []


def test_folder_input_expands_like_a_gather(tmp_path):
    ir = ir_dict(tmp_path, [{"id": "m", "for_each": ["r"], "outputs": {"o": "part_{r}.csv"}},
                            {"id": "all", "inputs": {"parts": {"folder": "part_{r}.csv"}}, "outputs": {"o": "x"}},
                            {"id": "two", "inputs": {"parts": {"folder": ["part_1.csv", "part_{r}.csv"]}},
                             "outputs": {"o": "y"}}],
                 axes={"r": ["1", "2", "3"]}, outputs=[{"file": "x"}, {"file": "y"}])
    exp, errors, _, deps = validate.prepare(ir)
    assert errors == []
    nodes = {n["id"]: n for n in exp["nodes"]}
    assert nodes["all"]["inputs"] == {"parts": {"folder": ["part_1.csv", "part_2.csv", "part_3.csv"]}}
    assert nodes["two"]["inputs"]["parts"]["folder"][0] == "part_1.csv"
    assert deps["all"] == {"m__1", "m__2", "m__3"}


def test_empty_folder_input_is_an_error(tmp_path):
    ir = ir_dict(tmp_path, [{"id": "a", "inputs": {"p": {"folder": []}}, "outputs": {"o": "o"}}])
    _, errors, _, _ = validate.prepare(ir)
    assert any("folder input p needs folder" in e for e in errors)


def test_whole_value_param_takes_the_axis_value_as_an_int(tmp_path):
    ir = ir_dict(tmp_path, [{"id": "m", "for_each": ["r", "w"], "outputs": {"o": "o_{r}_{w}"},
                             "params": {"rank": "{r}", "label": "copy {r}", "name": "{w}"}}],
                 axes={"r": ["1", "2"], "w": ["a", "b"]})
    exp, errors = expand.expand(ir)
    assert errors == []
    p = next(n for n in exp["nodes"] if n["id"] == "m__2__b")["params"]
    # r and w are passed on under other names, so they are not added again
    assert p == {"rank": 2, "label": "copy 2", "name": "b"}


def test_fanout_output_must_name_its_axis(tmp_path):
    ir = ir_dict(tmp_path, [{"id": "m", "for_each": ["r"], "outputs": {"o": "same.txt"}}], axes={"r": [1, 2]})
    _, errors = expand.expand(ir)
    assert any("must contain {r}" in e for e in errors)


# ── the runner shim, with condorio-style staging ──

def test_contract_was_removed_and_faasr_code_is_an_error(tmp_path):
    ir = ir_dict(tmp_path, [{"id": "a", "contract": "faasr", "entrypoint": "a", "outputs": {"o": "o.txt"}},
                            {"id": "b", "inputs": {"i": "o.txt"}, "outputs": {"o": "p.txt"}}],
                 outputs=[{"file": "p.txt"}])
    (tmp_path / "functions" / "a.py").write_text(
        "def a(folder, output1):\n    faasr_put_file(local_file='x', remote_folder=folder, remote_file=output1)\n")
    _, errors, warnings, _ = validate.prepare(ir)
    assert "a: `contract` was removed from the IR; every node is a plain main(inputs, outputs, params) " \
           "function" in errors
    assert "a: code calls the FaaSr API; IR functions are plain and use only the local paths they are " \
           "given" in errors
    assert not [w for w in warnings if "contract" in w]


def test_one_runner_for_every_node():
    src = (IR_DIR / "backends" / "pegasus_backend.py").read_text()
    assert "RUNNERS" not in src and 'pfn="file://" + str(SHIM)' in src
    assert RUNNER.is_file() and not (IR_DIR / "runner" / "curate_faasr_run.py").exists()
    assert not hasattr(validate, "CONTRACTS")


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


def run_plain(tmp_path, body, args, files=()):
    (tmp_path / "fn.py").write_text("import json, os\n\ndef main(inputs, outputs, params):\n" + body)
    for f in files:
        (tmp_path / f).write_text(f"content of {f}")
    return subprocess.run([sys.executable, str(RUNNER), "--code", "fn.py", *args],
                          cwd=tmp_path, capture_output=True, text=True, timeout=60)


def test_runner_list_outputs_must_all_be_written(tmp_path):
    body = "    for p in outputs['o'][:1]:\n        open(p, 'w').write('x')\n"
    p = run_plain(tmp_path, body, ["--out-list", "o=a.txt", "--out-list", "o=b.txt"])
    assert p.returncode != 0 and "declared outputs not written: ['b.txt']" in p.stderr
    body = "    assert outputs == {'o': ['a.txt', 'b.txt']}, outputs\n" \
           "    for p in outputs['o']:\n        open(p, 'w').write('x')\n"
    p = run_plain(tmp_path, body, ["--out-list", "o=a.txt", "--out-list", "o=b.txt"])
    assert p.returncode == 0, p.stderr


def test_runner_folder_input_is_one_directory(tmp_path):
    body = ("    d = inputs['parts']\n"
            "    assert os.path.isdir(d), d\n"
            "    names = sorted(os.listdir(d))\n"
            "    texts = [open(os.path.join(d, n)).read() for n in names]\n"
            "    json.dump([names, texts, inputs['one']], open(outputs['o'], 'w'))\n")
    p = run_plain(tmp_path, body, ["--in-dir", "parts=part_2.csv", "--in-dir", "parts=part_1.csv",
                                   "--in", "one=x.txt", "--out", "o=o.json"],
                  files=["part_1.csv", "part_2.csv", "x.txt"])
    assert p.returncode == 0, p.stderr
    names, texts, one = json.loads((tmp_path / "o.json").read_text())
    assert names == ["part_1.csv", "part_2.csv"] and texts == ["content of part_1.csv", "content of part_2.csv"]
    assert one == "x.txt"
    p = run_plain(tmp_path, "    pass\n", ["--in-dir", "parts=missing.csv"])
    assert p.returncode != 0 and "declared inputs not present: ['missing.csv']" in p.stderr


def test_runner_writes_markers_and_hides_them_from_the_function(tmp_path):
    body = "    assert inputs == {'a': 'x.txt'} and outputs == {} and params == {'rank': 2}, (inputs, outputs)\n"
    p = run_plain(tmp_path, body, ["--in", "a=x.txt", "--in-list", "curate_done_after_s=m_1",
                                   "--out", "curate_done=done", "--params-json", '{"rank": 2}'],
                  files=["x.txt", "m_1"])
    assert p.returncode == 0, p.stderr
    assert (tmp_path / "done").read_text() == ""
