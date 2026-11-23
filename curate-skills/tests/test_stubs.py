import json
import shutil

import pytest

import stubtest
from curate_models import FunctionSpec, IOSpec, WorkflowSpec

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")


# ── stubtest.py ──

def _setup(make_run, nodes, edges=(), mode="strict"):
    spec = WorkflowSpec(name="w", nodes=nodes, edges=list(edges), entry=nodes[0].name)
    run = make_run(spec=spec, stubtest_mode=mode)
    for n in nodes:
        (run / "functions" / n.name / "test_data").mkdir(parents=True)
    return run


def _write(run, node, code, deps=""):
    d = run / "functions" / node
    (d / f"{node}.py").write_text(code)
    (d / f"{node}.deps.txt").write_text(deps)
    return d


GEN = FunctionSpec(name="gen", outputs=[IOSpec(name="nums.txt")])
RANKED = FunctionSpec(name="part", rank=3, inputs=[IOSpec(name="nums.txt")],
                      outputs=[IOSpec(name="part_{rank}.txt")])
# The older FaaSr style: no longer stub-tested, only refused.
GEN_CODE = '''
def gen(folder, output1):
    open("n.txt", "w").write("1 2 3")
    faasr_put_file(local_file="n.txt", remote_folder=folder, remote_file=output1)
'''
P_GEN = '''
def main(inputs, outputs, params):
    assert params == {}, params
    open(outputs["output1"], "w").write("1 2 3")
'''


def test_module_not_found_is_a_pass_only_in_parity(make_run):
    run = _setup(make_run, [GEN])
    _write(run, "gen", "import curate_no_such_module_xyz\n" + P_GEN, deps="curate-no-such-module-xyz\n")
    parity = stubtest.run_stubtest(run, "gen", mode="parity")
    assert parity["ok"] and parity["module_missing_pass"]
    strict = stubtest.run_stubtest(run, "gen", mode="strict")
    assert not strict["ok"] and "Could not install" in strict["tail"]


def test_missing_file_and_bad_node(make_run):
    run = _setup(make_run, [GEN])
    res = stubtest.run_stubtest(run, "gen")
    assert not res["ok"] and "gen.py does not exist" in res["tail"]
    with pytest.raises(stubtest.InputError):
        stubtest.run_stubtest(run, "ghost")


def test_cli_exit_codes(make_run, capsys):
    run = _setup(make_run, [GEN])
    _write(run, "gen", P_GEN)
    assert stubtest.main(["run", "--run", str(run), "--node", "gen"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    _write(run, "gen", GEN_CODE)
    assert stubtest.main(["run", "--run", str(run), "--node", "gen"]) == 1
    capsys.readouterr()
    assert stubtest.main(["run", "--run", str(run), "--node", "ghost"]) == 2


# ── plain functions: main(inputs, outputs, params) on local paths ──

P_PART = '''
def main(inputs, outputs, params):
    nums = open(inputs["input1"]).read().split()
    open(outputs["output1"], "w").write(nums[params["rank"] - 1] + f"/{params['max_rank']}")
'''


def plain(make_run, nodes, edges=(), mode="strict"):
    return _setup(make_run, nodes, edges, mode=mode)


def test_plain_every_rank_gets_its_params_and_own_paths(make_run):
    run = plain(make_run, [GEN, RANKED], [{"from_node": "gen", "to_node": "part"}])
    _write(run, "gen", P_GEN)
    res = stubtest.run_stubtest(run, "gen")
    assert res["ok"] and res["harness"] == "plain", res["tail"]
    d = _write(run, "part", P_PART)
    res = stubtest.run_stubtest(run, "part")
    assert res["ok"], res["tail"]
    assert [r["rank"] for r in res["ranks"]] == [1, 2, 3] and res["returns"] == [None] * 3
    assert [(d / "test_output" / f"part_{i}.txt").read_text() for i in (1, 2, 3)] == ["1/3", "2/3", "3/3"]
    call = json.loads((d / "stub_in" / "r2" / "call.json").read_text())
    assert call["inputs"] == {"input1": "stub_in/r2/nums.txt"}
    assert call["outputs"] == {"output1": "test_output/part_2.txt"}
    assert call["params"] == {"rank": 2, "max_rank": 3}


def test_plain_list_output_then_ranked_reader_and_gather(make_run):
    split = FunctionSpec(name="split", outputs=[IOSpec(name="b_{rank}.txt")])
    part = FunctionSpec(name="part", rank=2, inputs=[IOSpec(name="b_{rank}.txt")], outputs=[IOSpec(name="c_{rank}.txt")])
    total = FunctionSpec(name="total", inputs=[IOSpec(name="c_{rank}.txt")], outputs=[IOSpec(name="t.txt")])
    run = plain(make_run, [split, part, total], [{"from_node": "split", "to_node": "part"},
                                                 {"from_node": "part", "to_node": "total"}])
    _write(run, "split", "def main(inputs, outputs, params):\n    for i, p in enumerate(outputs['output1'], 1):\n"
                         "        open(p, 'w').write(str(i * 10))\n")
    _write(run, "part", "def main(inputs, outputs, params):\n"
                        "    open(outputs['output1'], 'w').write(open(inputs['input1']).read())\n")
    _write(run, "total", "def main(inputs, outputs, params):\n"
                         "    open(outputs['output1'], 'w').write('+'.join(open(p).read() for p in inputs['input1']))\n")
    for n in ("split", "part", "total"):
        res = stubtest.run_stubtest(run, n)
        assert res["ok"], (n, res["tail"])
    assert (run / "functions" / "total" / "test_output" / "t.txt").read_text() == "10+20"


def test_plain_folder_input_collects_by_prefix_first_folder_wins(make_run):
    node = FunctionSpec(name="sum", inputs=[IOSpec(name="cnt_{rank}.json", kind="folder")],
                        outputs=[IOSpec(name="total.json")])
    run = plain(make_run, [node])
    d = run / "functions" / "sum"
    (d / "test_data" / "cnt_1.json").write_text("data-1")
    (d / "test_data" / "cnt_2.json").write_text("data-2")
    (d / "test_data" / "other.json").write_text("x")  # not under the prefix: left out
    _write(run, "sum", "import os\n\ndef main(inputs, outputs, params):\n    d = inputs['input1']\n"
                       "    names = sorted(os.listdir(d))\n"
                       "    open(outputs['output1'], 'w').write(','.join(open(os.path.join(d, n)).read() for n in names))\n")
    res = stubtest.run_stubtest(run, "sum")
    assert res["ok"], res["tail"]
    assert (d / "test_output" / "total.json").read_text() == "data-1,data-2"
    res = stubtest.run_stubtest(run, "sum", mode="parity")
    assert res["ok"] and (d / "test_output" / "total.json").read_text() == "data-1,data-2"


def test_empty_folder_of_upstream_outputs_fails_strict(make_run):
    count = FunctionSpec(name="count", rank=2, outputs=[IOSpec(name="cnt_{rank}.json")])
    total = FunctionSpec(name="sum", inputs=[IOSpec(name="cnt_{rank}.json", kind="folder")],
                         outputs=[IOSpec(name="total.json")])
    run = plain(make_run, [count, total], [{"from_node": "count", "to_node": "sum"}])
    _write(run, "sum", "def main(inputs, outputs, params):\n    open(outputs['output1'], 'w').write('0')\n")
    res = stubtest.run_stubtest(run, "sum")  # count's stub test never ran: its files are missing
    assert not res["ok"]
    assert res["missing_inputs"] == ["folder input1: no file starting with 'cnt_' (written by count)"]


def test_plain_missing_output_fails_strict_only(make_run):
    node = FunctionSpec(name="two", outputs=[IOSpec(name="a.txt"), IOSpec(name="b.txt")])
    run = plain(make_run, [node])
    _write(run, "two", "def main(inputs, outputs, params):\n    open(outputs['output1'], 'w').write('x')\n")
    res = stubtest.run_stubtest(run, "two")
    assert not res["ok"] and res["missing_outputs"] == ["b.txt"]
    assert "Declared output(s) not written: ['b.txt']" in res["tail"]
    assert stubtest.run_stubtest(run, "two", mode="parity")["ok"]


def test_generated_faasr_style_code_is_rejected_in_both_modes(make_run):
    run = plain(make_run, [GEN])
    _write(run, "gen", GEN_CODE)
    for mode in ("strict", "parity"):
        res = stubtest.run_stubtest(run, "gen", mode=mode)
        assert not res["ok"] and "does not follow the plain contract" in res["tail"]
        assert "no top-level `def main(inputs, outputs, params)`" in res["tail"]
        assert "uses the FaaSr API (faasr_put_file)" in res["tail"]


def test_missing_upstream_input_is_a_wiring_error_in_strict(make_run):
    run = plain(make_run, [GEN, RANKED], [{"from_node": "gen", "to_node": "part"}])
    _write(run, "part", P_PART)  # gen's stub test never ran: nums.txt is missing
    res = stubtest.run_stubtest(run, "part")
    assert not res["ok"] and res["missing_inputs"] == ["nums.txt (written by gen)"]
    assert "should have written are missing" in res["tail"]
    parity = stubtest.run_stubtest(run, "part", mode="parity")  # today's behaviour: an empty stand-in
    assert "EMPTY stand-in" in parity["tail"] and parity["empty_external_inputs"] == []


def test_missing_external_input_is_empty_and_reported(make_run):
    node = FunctionSpec(name="read", inputs=[IOSpec(name="station.csv")], outputs=[IOSpec(name="o.txt")])
    run = plain(make_run, [node])
    _write(run, "read", "def main(inputs, outputs, params):\n"
                        "    open(outputs['output1'], 'w').write(str(len(open(inputs['input1']).read())))\n")
    res = stubtest.run_stubtest(run, "read")
    assert res["ok"] and res["empty_external_inputs"] == ["station.csv"], res
    assert "EMPTY stand-in" in res["tail"]
    log = (run / "functions" / "read" / "stubtest.log").read_text()
    assert json.loads(log.split("\n\n", 1)[0])["empty_external_inputs"] == ["station.csv"]


def test_plain_parity_runs_rank_one_only(make_run):
    run = plain(make_run, [GEN, RANKED], [{"from_node": "gen", "to_node": "part"}], mode="parity")
    _write(run, "gen", P_GEN)
    stubtest.run_stubtest(run, "gen")
    _write(run, "part", P_PART)
    res = stubtest.run_stubtest(run, "part")
    assert res["ok"] and [r["rank"] for r in res["ranks"]] == [1]


def test_plain_secrets_pass_and_deploy_credentials_are_scrubbed(make_run, monkeypatch):
    monkeypatch.setenv("GH_PAT", "should-not-leak")
    monkeypatch.setenv("MY_API_KEY", "declared-secret")
    run = plain(make_run, [GEN])
    _write(run, "gen", "import os\n\ndef main(inputs, outputs, params):\n"
                       "    assert 'GH_PAT' not in os.environ\n"
                       "    open(outputs['output1'], 'w').write(os.environ['MY_API_KEY'])\n")
    res = stubtest.run_stubtest(run, "gen")
    assert res["ok"], res["tail"]


def test_plain_return_values_are_recorded(make_run):
    run = plain(make_run, [FunctionSpec(name="check")])
    _write(run, "check", "def main(inputs, outputs, params):\n    return False\n")
    res = stubtest.run_stubtest(run, "check")
    assert res["ok"] and res["returns"] == [False]


def test_plain_inputs_are_copies_upstream_outputs_are_safe(make_run):
    run = plain(make_run, [GEN, RANKED], [{"from_node": "gen", "to_node": "part"}])
    _write(run, "gen", P_GEN)
    assert stubtest.run_stubtest(run, "gen")["ok"]
    _write(run, "part", "def main(inputs, outputs, params):\n    open(inputs['input1'], 'w').write('clobbered')\n"
                        "    open(outputs['output1'], 'w').write('x')\n")
    assert stubtest.run_stubtest(run, "part")["ok"]
    assert (run / "functions" / "gen" / "test_output" / "nums.txt").read_text() == "1 2 3"


def test_reused_faasr_style_code_is_refused_too(make_run):
    # Phase 5: no legacy stub path. (Reuse turns such catalog code into an adapt seed first.)
    run = plain(make_run, [GEN.model_copy(update={"source": "catalog"})])
    _write(run, "gen", GEN_CODE)
    res = stubtest.run_stubtest(run, "gen")
    assert not res["ok"] and "does not follow the plain contract" in res["tail"]
    assert "harness" not in res
