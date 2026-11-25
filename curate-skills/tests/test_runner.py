"""curate_ir/runner/curate_run.py, the runner shim every Pegasus job runs: it calls the step's
main(inputs, outputs, params) on the files Pegasus staged into the job's folder. (The IR v0.2
validator and expander it used to sit next to were removed in phase 8; the CWL validator's tests are
test_cwl_validate.py, and whole workflows run through tests/ir_local.py.)
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
IR_DIR = PLUGIN / "curate_ir"
RUNNER = IR_DIR / "runner" / "curate_run.py"


def test_one_runner_for_every_node():
    src = (IR_DIR / "backends" / "pegasus_backend.py").read_text()
    assert "RUNNERS" not in src and 'pfn="file://" + str(SHIM)' in src
    assert RUNNER.is_file() and not (IR_DIR / "runner" / "curate_faasr_run.py").exists()


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
