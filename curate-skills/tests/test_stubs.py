import json
import os
import shutil
from pathlib import Path

import pytest

import faasr_stubs
import stubtest
from curate_models import FunctionSpec, IOSpec, WorkflowSpec

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")


# ── faasr_stubs, called directly ──

def test_stub_lookup_order_and_folder_list(tmp_path, monkeypatch, capsys):
    up = tmp_path / "up"
    for d in ("test_output", "test_data", "up"):
        (tmp_path / d).mkdir()
    (tmp_path / "test_data" / "a.txt").write_text("data")
    (tmp_path / "up" / "a.txt").write_text("upstream")
    (tmp_path / "up" / "b.txt").write_text("upstream-b")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CURATE_UPSTREAM_DIRS", str(up))

    faasr_stubs.faasr_get_file("x", "f", "a.txt")
    assert Path("x").read_text() == "data"
    faasr_stubs.faasr_get_file("y", "f", "b.txt")
    assert Path("y").read_text() == "upstream-b"
    faasr_stubs.faasr_get_file("z", "f", "missing.txt")
    assert Path("z").read_text() == "" and "missing.txt" in capsys.readouterr().err

    Path("out.txt").write_text("result")
    faasr_stubs.faasr_put_file("out.txt", "f", "a.txt")
    faasr_stubs.faasr_get_file("w", "f", "a.txt")
    assert Path("w").read_text() == "result"  # own test_output wins
    assert faasr_stubs.faasr_get_folder_list(prefix="wf/a") == ["wf/a.txt"]
    assert faasr_stubs.faasr_get_folder_list() == ["a.txt", "b.txt"]


def test_stub_rank(monkeypatch):
    monkeypatch.setenv("FAASR_RANK", "2")
    monkeypatch.setenv("FAASR_MAX_RANK", "3")
    assert faasr_stubs.faasr_rank() == {"rank": 2, "max_rank": 3}


# ── stubtest.py ──

def _setup(make_run, nodes, edges=(), mode="strict"):
    spec = WorkflowSpec(name="w", nodes=nodes, edges=list(edges), entry=nodes[0].name)
    run = make_run(spec=spec, stubtest_mode=mode)
    (run / "stubs").mkdir()
    shutil.copy2(Path(stubtest.__file__).parent / "faasr_stubs.py", run / "stubs" / "faasr_stubs.py")
    for n in nodes:
        (run / "functions" / n.name / "test_data").mkdir(parents=True)
    return run


def _write(run, node, code, deps=""):
    d = run / "functions" / node
    (d / f"{node}.py").write_text(code)
    (d / f"{node}.deps.txt").write_text(deps)
    return d


GEN = FunctionSpec(name="gen", outputs=[IOSpec(name="nums.txt")])
GEN_CODE = '''
def gen(folder, output1):
    open("n.txt", "w").write("1 2 3")
    faasr_put_file(local_file="n.txt", remote_folder=folder, remote_file=output1)
'''
RANKED = FunctionSpec(name="part", rank=3, inputs=[IOSpec(name="nums.txt")],
                      outputs=[IOSpec(name="part_{rank}.txt")])
RANKED_CODE = '''
def part(folder, input1, output1):
    r = faasr_rank()
    faasr_get_file(local_file="in.txt", remote_folder=folder, remote_file=input1)
    nums = open("in.txt").read().split()
    open("p.txt", "w").write(nums[r["rank"] - 1])
    faasr_put_file(local_file="p.txt", remote_folder=folder,
                   remote_file=output1.replace("{rank}", str(r["rank"])))
'''


def test_strict_runs_every_rank_on_upstream_output(make_run):
    run = _setup(make_run, [GEN, RANKED], [{"from_node": "gen", "to_node": "part"}])
    _write(run, "gen", GEN_CODE)
    assert stubtest.run_stubtest(run, "gen")["ok"]
    d = _write(run, "part", RANKED_CODE)
    res = stubtest.run_stubtest(run, "part")
    assert res["ok"], res["tail"]
    assert [r["rank"] for r in res["ranks"]] == [1, 2, 3]
    assert sorted(p.name for p in (d / "test_output").iterdir()) == [
        "part_1.txt", "part_2.txt", "part_3.txt"]
    assert (d / "test_output" / "part_3.txt").read_text() == "3"
    assert (d / "stubtest.log").exists()


def test_parity_runs_rank_one_only(make_run):
    run = _setup(make_run, [GEN, RANKED], [{"from_node": "gen", "to_node": "part"}], mode="parity")
    _write(run, "gen", GEN_CODE)
    stubtest.run_stubtest(run, "gen")
    _write(run, "part", RANKED_CODE)
    res = stubtest.run_stubtest(run, "part")
    assert res["ok"] and res["mode"] == "parity" and len(res["ranks"]) == 1


def test_strict_requires_declared_outputs(make_run):
    node = FunctionSpec(name="two", outputs=[IOSpec(name="a.txt"), IOSpec(name="b.txt")])
    run = _setup(make_run, [node])
    _write(run, "two", '''
def two(folder, output1, output2):
    open("a", "w").write("x")
    faasr_put_file(local_file="a", remote_folder=folder, remote_file=output1)
    faasr_put_file(local_file="never_written", remote_folder=folder, remote_file=output2)
''')
    res = stubtest.run_stubtest(run, "two")
    assert not res["ok"] and res["missing_outputs"] == ["b.txt"]
    assert stubtest.run_stubtest(run, "two", mode="parity")["ok"]


def test_put_count_check_in_both_modes(make_run):
    run = _setup(make_run, [GEN])
    _write(run, "gen", "def gen(folder, output1):\n    open(output1, 'w').write('{}')\n")
    for mode in ("strict", "parity"):
        res = stubtest.run_stubtest(run, "gen", mode=mode)
        assert not res["ok"] and "faasr_put_file" in res["tail"]


def test_faasr_import_rejected_only_in_strict(make_run):
    run = _setup(make_run, [GEN])
    _write(run, "gen", "try:\n    import faasr\nexcept ImportError:\n    pass\n" + GEN_CODE)
    assert "faasr import" in stubtest.run_stubtest(run, "gen")["tail"]
    assert stubtest.run_stubtest(run, "gen", mode="parity")["ok"]


def test_module_not_found_is_a_pass_only_in_parity(make_run):
    run = _setup(make_run, [GEN])
    _write(run, "gen", "import curate_no_such_module_xyz\n" + GEN_CODE, deps="curate-no-such-module-xyz\n")
    parity = stubtest.run_stubtest(run, "gen", mode="parity")
    assert parity["ok"] and parity["module_missing_pass"]
    strict = stubtest.run_stubtest(run, "gen", mode="strict")
    assert not strict["ok"] and "Could not install" in strict["tail"]


def test_deployment_secrets_are_scrubbed(make_run, monkeypatch):
    monkeypatch.setenv("GH_PAT", "should-not-leak")
    monkeypatch.setenv("MY_API_KEY", "declared-secret")
    run = _setup(make_run, [GEN])
    _write(run, "gen", '''
import os
def gen(folder, output1):
    assert "GH_PAT" not in os.environ
    assert faasr_secret("MY_API_KEY") == "declared-secret"
    open("n.txt", "w").write("ok")
    faasr_put_file(local_file="n.txt", remote_folder=folder, remote_file=output1)
''')
    res = stubtest.run_stubtest(run, "gen")
    assert res["ok"], res["tail"]


def test_missing_file_and_bad_node(make_run):
    run = _setup(make_run, [GEN])
    assert not stubtest.run_stubtest(run, "gen")["ok"]
    with pytest.raises(stubtest.InputError):
        stubtest.run_stubtest(run, "ghost")


def test_cli_exit_codes(make_run, capsys):
    run = _setup(make_run, [GEN])
    _write(run, "gen", GEN_CODE)
    assert stubtest.main(["run", "--run", str(run), "--node", "gen"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert stubtest.main(["run", "--run", str(run), "--node", "ghost"]) == 2
