"""pegasus_ir.py (spec -> IR for the Pegasus target) and runner/curate_faasr_run.py, without
Pegasus: generated folders are validated and expanded by curate_ir, then run job by job with
condorio-style staging (tests/ir_local.py)."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import emit
import pegasus_ir as P
from curate_models import IOSpec, WorkflowSpec
from ir_local import IR_DIR, run_locally

yaml = pytest.importorskip("yaml")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "e2-stages"
FAASR_RUNNER = IR_DIR / "runner" / "curate_faasr_run.py"
sys.path.insert(0, str(IR_DIR))
import expand  # noqa: E402
import validate  # noqa: E402


def e2_spec() -> WorkflowSpec:
    return WorkflowSpec.model_validate(json.loads((FIXTURES / "spec-with-code.json").read_text()))


def build_folder(tmp_path, spec, **kw) -> Path:
    built = P.build_ir(spec, **kw)
    return P.write_folder(tmp_path / "ir", built)


def expanded(folder):
    exp, errors, _, _ = validate.prepare(validate.load(folder / "ir.yaml"))
    assert errors == []
    return {n["id"]: n for n in exp["nodes"]}


def node(name, code, inputs=(), outputs=(), rank=1, **kw):
    return {"name": name, "code": code, "rank": rank, "inputs": [{"name": i} for i in inputs],
            "outputs": [{"name": o} for o in outputs], **kw}


def spec_of(nodes, edges=None, **kw):
    if edges is None:
        edges = [{"from_node": a["name"], "to_node": b["name"]} for a, b in zip(nodes, nodes[1:])]
    return WorkflowSpec.model_validate({"name": "t_flow", "folder": "tf", "nodes": nodes, "edges": edges, **kw})


# Small FaaSr-style functions for the shape tests.
WRITE_EACH_RANK = '''
def {name}(folder, output1):
    r = faasr_rank()["rank"]
    open("x", "w").write(str(r))
    faasr_put_file(local_file="x", remote_folder=folder, remote_file=output1.replace("{{rank}}", str(r)))
'''
READ_OWN_RANK = '''
def {name}(folder, input1, output1):
    r = faasr_rank()["rank"]
    faasr_get_file(local_file="in", remote_folder=folder, remote_file=input1.replace("{{rank}}", str(r)))
    open("o", "w").write("own:" + open("in").read())
    faasr_put_file(local_file="o", remote_folder=folder, remote_file=output1.replace("{{rank}}", str(r)))
'''
READ_ALL = '''
def {name}(folder, input1, output1):
    r = faasr_rank()["rank"]
    head = input1.split("{{rank}}")[0]
    keys = faasr_get_folder_list(prefix=folder + "/" + head)
    vals = []
    for k in keys:
        b = k.rsplit("/", 1)[-1]
        faasr_get_file(local_file=b + ".l", remote_folder=folder, remote_file=b)
        vals.append(open(b + ".l").read())
    open("o", "w").write(",".join(vals))
    faasr_put_file(local_file="o", remote_folder=folder, remote_file=output1.replace("{{rank}}", str(r)))
'''
UNCLEAR = '''
def {name}(folder, input1, output1):
    r = faasr_rank()["rank"]
    name = input1
    faasr_put_file(local_file="o", remote_folder=folder, remote_file=output1.replace("{{rank}}", str(r)))
'''


def fn(template, name):
    return template.format(name=name)


# ── the e2 MapReduce fixture ──

def test_e2_mapping(tmp_path):
    folder = build_folder(tmp_path, e2_spec())
    ir = yaml.safe_load((folder / "ir.yaml").read_text())
    assert ir["ir_version"] == "0.2" and ir["name"] == "mapreduce-word-count"
    assert ir["axes"] == {"rank3": ["1", "2", "3"], "rank5": ["1", "2", "3", "4", "5"]}
    assert ir["inputs"] == []  # zero workflow inputs is fine
    assert all(n["contract"] == "faasr" and n["entrypoint"] == n["id"] for n in ir["nodes"])
    jobs = expanded(folder)
    assert len(jobs) == 11
    assert jobs["split"]["outputs"] == {f"output1_{i}": f"text_batch_{i}.txt" for i in (1, 2, 3)}
    assert jobs["map__2"]["inputs"] == {"input1": "text_batch_2.txt"}          # paired copy by copy
    assert jobs["shuffle"]["inputs"]["input1"] == [f"map_counts_{i}.json" for i in (1, 2, 3)]  # gather
    assert len(jobs["shuffle"]["outputs"]) == 5
    assert jobs["reduce__4"]["params"]["rank5"] == "4"
    assert jobs["reduce__4"]["params"]["faasr"] == {
        "args": {"folder": "mapreduce_word_count", "input1": "shuffle_partition_{rank}.json",
                 "output1": "word_count_{rank}.json"}, "rank_axis": "rank5", "max_rank": 5}
    assert [o["file"] for o in ir["outputs"]] == ["word_count_{rank5}.json"]
    assert (folder / "functions" / "map.py").read_text() == next(n.code for n in e2_spec().nodes if n.name == "map")
    assert (folder / "inputs").is_dir()


def test_e2_runs_end_to_end_through_the_faasr_runner(tmp_path):
    folder = build_folder(tmp_path, e2_spec())
    files = run_locally(folder / "ir.yaml", tmp_path / "jobs")
    totals = {}
    for i in range(1, 6):
        for word, n in json.loads(files[f"word_count_{i}.json"].read_text()).items():
            assert word not in totals  # each reducer owns its words
            totals[word] = n
    assert totals == {w: 1000 for w in ("cat", "dog", "bird", "horse", "pig")}


def test_faasr_arguments_unchanged_for_the_faasr_emitter():
    spec = e2_spec()
    wf = emit.emit_faasr_json(spec)
    for n in spec.nodes:
        assert wf["ActionList"][emit._to_action_name(n.name)]["Arguments"] == emit.faasr_arguments(spec, n)


# ── same-count ranked steps: paired only when the reader reads its own rank ──

def test_same_count_read_own_rank_is_paired(tmp_path):
    spec = spec_of([node("a", fn(WRITE_EACH_RANK, "a"), outputs=["p_{rank}.txt"], rank=3),
                    node("b", fn(READ_OWN_RANK, "b"), ["p_{rank}.txt"], ["q_{rank}.txt"], rank=3)])
    folder = build_folder(tmp_path, spec)
    jobs = expanded(folder)
    assert jobs["b__2"]["inputs"] == {"input1": "p_2.txt"}
    files = run_locally(folder / "ir.yaml", tmp_path / "jobs")
    assert files["q_2.txt"].read_text() == "own:2"


def test_same_count_all_to_all_becomes_a_gather(tmp_path):
    spec = spec_of([node("a", fn(WRITE_EACH_RANK, "a"), outputs=["p_{rank}.txt"], rank=3),
                    node("b", fn(READ_ALL, "b"), ["p_{rank}.txt"], ["q_{rank}.txt"], rank=3)])
    folder = build_folder(tmp_path, spec)
    ir = yaml.safe_load((folder / "ir.yaml").read_text())
    assert ir["axes"]["rank3_all"] == ["1", "2", "3"]
    jobs = expanded(folder)
    assert len(jobs) == 6
    for r in (1, 2, 3):  # every copy of b gets every copy of a's file, and waits for all of them
        assert jobs[f"b__{r}"]["inputs"] == {"input1": ["p_1.txt", "p_2.txt", "p_3.txt"]}
    files = run_locally(folder / "ir.yaml", tmp_path / "jobs")
    assert all(files[f"q_{r}.txt"].read_text() == "1,2,3" for r in (1, 2, 3))


def test_all_to_all_with_a_different_count_is_a_gather_too(tmp_path):
    spec = spec_of([node("a", fn(WRITE_EACH_RANK, "a"), outputs=["p_{rank}.txt"], rank=3),
                    node("b", fn(READ_ALL, "b"), ["p_{rank}.txt"], ["q_{rank}.txt"], rank=2)])
    files = run_locally(build_folder(tmp_path, spec) / "ir.yaml", tmp_path / "jobs")
    assert files["q_2.txt"].read_text() == "1,2,3"


def test_unclear_reader_is_refused_by_name():
    spec = spec_of([node("a", fn(WRITE_EACH_RANK, "a"), outputs=["p_{rank}.txt"], rank=3),
                    node("b", fn(UNCLEAR, "b"), ["p_{rank}.txt"], ["q_{rank}.txt"], rank=3)])
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    (msg,) = e.value.problems
    assert msg.startswith("ranked step b (×3) reads p_{rank}.txt as input1, but its code neither substitutes")
    assert "faasr_get_folder_list" in msg


def test_read_mode():
    assert P.read_mode(fn(READ_OWN_RANK, "x"), 1) == "rank"
    assert P.read_mode("def f(folder, input1):\n    n = input1.format(rank=2)\n", 1) == "rank"
    assert P.read_mode(fn(READ_ALL, "x"), 1) == "all"
    assert P.read_mode(fn(READ_OWN_RANK, "x"), 2) is None
    assert P.read_mode("def f(:", 1) is None


# ── refusals, each with its own message ──

def test_n_to_m_handoff_refused():
    spec = spec_of([node("a", fn(WRITE_EACH_RANK, "a"), outputs=["p_{rank}.txt"], rank=3),
                    node("b", fn(READ_OWN_RANK, "b"), ["p_{rank}.txt"], ["q_{rank}.txt"], rank=5)])
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    (msg,) = e.value.problems
    assert msg.startswith("N→M ranked handoff: ranked step a (×3) provides 3 copies of p_{rank}.txt, "
                          "but ranked step b runs 5 ranks")
    assert "unranked step between them" in msg


def test_conditional_edge_refused_with_explicit_message():
    spec = spec_of([node("check", "def check(folder):\n    return True\n"),
                    node("then_step", "def then_step(folder):\n    pass\n")],
                   edges=[{"from_node": "check", "to_node": "then_step", "condition": "True"}])
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    assert e.value.problems == [
        "conditional edge check → then_step (taken only when check returns True): Pegasus runs a fixed DAG "
        "in which every step runs, so it cannot honour the condition. Remove the condition with a Gate 4 "
        "change request, or deploy to FaaSr."]


def test_secrets_missing_code_and_bad_names_refused_together():
    spec = spec_of([node("get", "def get(folder, output1):\n    pass\n", outputs=["a.txt"], secrets=["API_KEY"]),
                    node("Use__It", None, ["a.txt"], ["b.txt"])])
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    probs = e.value.problems
    assert any("reads secrets (API_KEY)" in p and "deploy to FaaSr" in p for p in probs)
    assert any(p.startswith("step Use__It has no code") for p in probs)
    assert any(p.startswith("step Use__It: Pegasus needs a name") for p in probs)


def test_ranked_writer_without_rank_placeholder_refused():
    spec = spec_of([node("a", fn(WRITE_EACH_RANK, "a"), outputs=["same.txt"], rank=3)])
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    assert "its ranks would overwrite each other" in e.value.problems[0]


def test_packages_warn_only():
    spec = spec_of([node("a", "def a(folder, output1):\n    pass\n", outputs=["o.txt"], dependencies=["numpy", "json"])])
    built = P.build_ir(spec, image="docker:///me/img:1")
    assert built["warnings"] == ["step a imports numpy: these must already be in the image docker:///me/img:1; "
                                 "nothing is installed at run time"]
    assert built["ir"]["nodes"][0]["packages"] == ["numpy"]


# ── inputs, deliverables, ordering markers ──

def test_workflow_inputs_and_deliverables(tmp_path):
    spec = spec_of([node("a", fn(READ_OWN_RANK, "a"), ["in_{rank}.csv"], ["out_{rank}.txt"], rank=2),
                    node("b", "def b(folder, input1, output1):\n    pass\n", ["out_{rank}.txt"], ["sum.json"])])
    built = P.build_ir(spec)
    assert built["inputs"] == ["in_1.csv", "in_2.csv"]
    assert built["ir"]["inputs"] == [{"file": "in_{rank2}.csv", "location": "inputs/in_{rank2}.csv"}]
    assert built["ir"]["outputs"] == [{"file": "sum.json", "deliver_as": "results/sum.json"}]
    over = P.build_ir(spec, deliverables=["out_{rank}.txt=answers/out_{rank}.txt", "sum.json"])
    assert over["ir"]["outputs"] == [{"file": "out_{rank2}.txt", "deliver_as": "answers/out_{rank2}.txt"},
                                     {"file": "sum.json", "deliver_as": "results/sum.json"}]
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec, deliverables=["nope.txt"])
    assert e.value.problems == ["deliverable nope.txt is not written by any step"]


def test_edge_without_a_file_gets_a_marker_and_waits_for_all_copies(tmp_path):
    spec = spec_of([node("a", fn(WRITE_EACH_RANK, "a"), outputs=["p_{rank}.txt"], rank=2),
                    node("b", fn(WRITE_EACH_RANK, "b"), outputs=["q_{rank}.txt"], rank=2)])
    folder = build_folder(tmp_path, spec)
    jobs = expanded(folder)
    assert jobs["a__1"]["outputs"]["curate_done"] == "curate_done__a_1"
    assert jobs["b__1"]["inputs"] == {"curate_done_after_a": ["curate_done__a_1", "curate_done__a_2"]}
    files = run_locally(folder / "ir.yaml", tmp_path / "jobs")
    assert files["curate_done__a_2"].read_text() == "" and files["q_2.txt"].read_text() == "2"


def test_write_folder_never_overwrites(tmp_path):
    built = P.build_ir(e2_spec())
    P.write_folder(tmp_path / "x", built)
    with pytest.raises(FileExistsError):
        P.write_folder(tmp_path / "x", built)


def test_cli_build(tmp_path, capsys):
    out = tmp_path / "ir"
    assert P.main(["build", "--spec", str(FIXTURES / "spec-with-code.json"), "--out", str(out), "--name", "wc-0117"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["ok"] and res["jobs"] == 11 and res["validation"]["errors"] == []
    faasr = [w for w in res["validation"]["warnings"] if "contract faasr" in w]
    assert faasr == ["contract faasr (generate_text, split, map, shuffle, reduce): FaaSr-style code run through "
                     "runner/curate_faasr_run.py; portable only to backends that ship that runner"]
    assert yaml.safe_load((out / "ir.yaml").read_text())["name"] == "wc-0117"
    assert P.main(["build", "--spec", str(FIXTURES / "spec-with-code.json"), "--out", str(out)]) == 2
    assert "already exists" in json.loads(capsys.readouterr().out)["error"]


# ── the FaaSr runner on its own ──

def run_runner(tmp_path, code, args, inputs=(), outputs=(), lists=(), rank=None, max_rank=1):
    (tmp_path / "fn.py").write_text(code)
    for f in list(inputs) + list(lists):
        (tmp_path / f).write_text(f"content of {f}")
    params = {"faasr": {"args": args, "rank_axis": "rank2" if rank else None, "max_rank": max_rank}}
    if rank:
        params["rank2"] = str(rank)
    cmd = [sys.executable, str(FAASR_RUNNER), "--code", "fn.py", "--entrypoint", "f",
           "--params-json", json.dumps(params)]
    cmd += [x for i, f in enumerate(inputs, 1) for x in ("--in", f"input{i}={f}")]
    cmd += [x for f in lists for x in ("--in-list", f"input9={f}")]
    cmd += [x for k, f in outputs for x in ("--out", f"{k}={f}")]
    return subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=60)


def test_runner_reads_declared_inputs_only(tmp_path):
    p = run_runner(tmp_path, "def f(folder, input1):\n    faasr_get_file(local_file='l', remote_folder=folder, "
                   "remote_file='other.txt')\n", {"folder": "d", "input1": "a.txt"}, inputs=["a.txt"])
    assert p.returncode != 0 and "'other.txt' is not a declared input of this step (declared: a.txt)" in p.stderr


def test_runner_writes_declared_outputs_only(tmp_path):
    p = run_runner(tmp_path, "def f(folder):\n    open('l','w').write('x')\n    faasr_put_file(local_file='l', "
                   "remote_folder=folder, remote_file='sneaky.txt')\n", {"folder": "d"}, outputs=[("output1", "o.txt")])
    assert p.returncode != 0 and "'sneaky.txt' is not a declared output" in p.stderr


def test_runner_requires_every_output_and_writes_markers(tmp_path):
    p = run_runner(tmp_path, "def f(folder):\n    pass\n", {"folder": "d"}, outputs=[("output1", "o.txt")])
    assert p.returncode != 0 and "declared outputs not uploaded with faasr_put_file: ['o.txt']" in p.stderr
    p = run_runner(tmp_path, "def f(folder):\n    pass\n", {"folder": "d"}, outputs=[("curate_done", "m")])
    assert p.returncode == 0, p.stderr
    assert (tmp_path / "m").read_text() == ""


def test_runner_folder_list_rank_and_log(tmp_path):
    code = ("def f(folder, output1):\n"
            "    keys = faasr_get_folder_list(prefix=folder + '/part_')\n"
            "    r = faasr_rank()\n"
            "    faasr_log('hello')\n"
            "    open('l','w').write(repr((keys, r)))\n"
            "    faasr_put_file(local_file='l', remote_folder=folder, remote_file=output1)\n")
    p = run_runner(tmp_path, code, {"folder": "d", "output1": "o.txt"}, lists=["part_2.txt", "part_1.txt", "zz.txt"],
                   outputs=[("output1", "o.txt")], rank=2, max_rank=2)
    assert p.returncode == 0, p.stderr
    assert (tmp_path / "o.txt").read_text() == repr((["d/part_1.txt", "d/part_2.txt"], {"rank": 2, "max_rank": 2}))
    assert "[FaaSr] hello" in p.stderr


def test_runner_covers_the_stub_api():
    import ast
    stubs = Path(__file__).resolve().parents[1] / "scripts" / "faasr_stubs.py"
    stub_api = {n.name for n in ast.parse(stubs.read_text()).body
                if isinstance(n, ast.FunctionDef) and n.name.startswith("faasr_")}
    runner_src = FAASR_RUNNER.read_text()
    assert stub_api and all(f"def {name}(" in runner_src for name in stub_api)


# ── plain functions (plain_contract.py): the same spec shapes, run by the plain IR runner ──

PLAIN_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "e2-stages-plain"


def plain_e2_spec() -> WorkflowSpec:
    return WorkflowSpec.model_validate(json.loads((PLAIN_FIXTURES / "spec-with-code.json").read_text()))


def word_totals(files):
    totals = {}
    for i in range(1, 6):
        for word, n in json.loads(files[f"word_count_{i}.json"].read_text()).items():
            assert word not in totals  # each reducer owns its words
            totals[word] = n
    return totals


def test_plain_fixture_is_plain_and_matches_the_faasr_spec():
    import plain_contract as PC
    plain, faasr = plain_e2_spec(), e2_spec()
    assert all(PC.is_plain(n.code) for n in plain.nodes)
    assert not any(PC.is_plain(n.code) for n in faasr.nodes)
    for p, f in zip(plain.nodes, faasr.nodes):  # same steps, ranks and file names
        assert (p.name, p.rank, [i.name for i in p.inputs], [o.name for o in p.outputs]) == \
               (f.name, f.rank, [i.name for i in f.inputs], [o.name for o in f.outputs])
        assert p.code == (PLAIN_FIXTURES / "functions" / f"{p.name}.py").read_text()


def test_plain_e2_mapping(tmp_path):
    folder = build_folder(tmp_path, plain_e2_spec())
    ir = yaml.safe_load((folder / "ir.yaml").read_text())
    assert ir["axes"] == {"rank3": ["1", "2", "3"], "rank5": ["1", "2", "3", "4", "5"]}
    assert all("contract" not in n and "entrypoint" not in n for n in ir["nodes"])  # plain, main
    by_id = {n["id"]: n for n in ir["nodes"]}
    assert by_id["map"]["params"] == {"rank": "{rank3}", "max_rank": 3}
    assert by_id["split"]["params"] == {}
    assert by_id["shuffle"]["inputs"] == {"input1": {"folder": ["map_counts_{rank3}.json"]}}
    jobs = expanded(folder)
    assert len(jobs) == 11
    assert jobs["split"]["outputs"] == {"output1": [f"text_batch_{i}.txt" for i in (1, 2, 3)]}  # list
    assert jobs["map__2"]["inputs"] == {"input1": "text_batch_2.txt"}                          # own copy
    assert jobs["map__2"]["params"] == {"rank": 2, "max_rank": 3}                              # int, no rank3
    assert jobs["shuffle"]["inputs"] == {"input1": {"folder": [f"map_counts_{i}.json" for i in (1, 2, 3)]}}
    assert jobs["shuffle"]["outputs"] == {"output1": [f"shuffle_partition_{i}.json" for i in range(1, 6)]}
    assert [o["file"] for o in ir["outputs"]] == ["word_count_{rank5}.json"]  # folder-read files are not end files
    assert (folder / "functions" / "map.py").read_text() == next(n.code for n in plain_e2_spec().nodes
                                                                  if n.name == "map")


def test_plain_and_faasr_e2_give_the_same_word_counts(tmp_path):
    plain = run_locally(build_folder(tmp_path / "p", plain_e2_spec()) / "ir.yaml", tmp_path / "pj")
    faasr = run_locally(build_folder(tmp_path / "f", e2_spec()) / "ir.yaml", tmp_path / "fj")
    expected = {w: 1000 for w in ("cat", "dog", "bird", "horse", "pig")}
    assert word_totals(plain) == word_totals(faasr) == expected
    for i in range(1, 6):  # same partition of words over reducers
        assert json.loads(plain[f"word_count_{i}.json"].read_text()).keys() == \
               json.loads(faasr[f"word_count_{i}.json"].read_text()).keys()


def test_mixed_plain_and_faasr_steps(tmp_path):
    spec = plain_e2_spec()
    faasr = {n.name: n.code for n in e2_spec().nodes}
    for n in spec.nodes:
        if n.name in ("split", "reduce"):  # a FaaSr-style writer and reader among plain steps
            n.code = faasr[n.name]
    folder = build_folder(tmp_path, spec)
    by_id = {n["id"]: n for n in yaml.safe_load((folder / "ir.yaml").read_text())["nodes"]}
    assert by_id["split"]["contract"] == "faasr" and "contract" not in by_id["map"]
    assert word_totals(run_locally(folder / "ir.yaml", tmp_path / "jobs")) == \
        {w: 1000 for w in ("cat", "dog", "bird", "horse", "pig")}


PLAIN_WRITE_RANK = '''
def main(inputs, outputs, params):
    open(outputs["output1"], "w").write(str(params["rank"]))
'''
PLAIN_READ = '''
import os

def main(inputs, outputs, params):
    v = inputs["input1"]
    if isinstance(v, list):
        got = ",".join(open(p).read() for p in v)
    elif os.path.isdir(v):
        got = ",".join(open(os.path.join(v, n)).read() for n in sorted(os.listdir(v)))
    else:
        got = "own:" + open(v).read()
    for p in outputs["output1"] if isinstance(outputs["output1"], list) else [outputs["output1"]]:
        open(p, "w").write(got)
'''


def test_plain_read_own_all_and_folder(tmp_path):
    for read, kind, want in [(None, "file", "own:2"), ("all", "file", "1,2,3"), (None, "folder", "1,2,3")]:
        spec = spec_of([node("a", PLAIN_WRITE_RANK, outputs=["p_{rank}.txt"], rank=3),
                        node("b", PLAIN_READ, [], ["q_{rank}.txt"], rank=3)])
        spec.nodes[1].inputs = [IOSpec(name="p_{rank}.txt", read=read, kind=kind)]
        folder = build_folder(tmp_path / f"{read}-{kind}", spec)
        files = run_locally(folder / "ir.yaml", tmp_path / f"{read}-{kind}-jobs")
        assert files["q_2.txt"].read_text() == want, (read, kind)


def test_plain_unranked_reader_gets_a_list_and_writer_writes_one(tmp_path):
    spec = spec_of([node("a", PLAIN_WRITE_RANK, outputs=["p_{rank}.txt"], rank=2),
                    node("b", PLAIN_READ, ["p_{rank}.txt"], ["s.txt"])])
    files = run_locally(build_folder(tmp_path, spec) / "ir.yaml", tmp_path / "jobs")
    assert files["s.txt"].read_text() == "1,2"


def test_plain_folder_input_matching_nothing_is_refused():
    spec = spec_of([node("a", PLAIN_WRITE_RANK, outputs=["p_{rank}.txt"], rank=2),
                    node("b", PLAIN_READ, [], ["s.txt"])])
    spec.nodes[1].inputs = [IOSpec(name="zz_{rank}.txt", kind="folder")]
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    assert any(p.startswith("step b reads the folder input zz_{rank}.txt (every file starting with 'zz_')")
               for p in e.value.problems)


def test_plain_n_to_m_handoff_still_refused():
    spec = spec_of([node("a", PLAIN_WRITE_RANK, outputs=["p_{rank}.txt"], rank=3),
                    node("b", PLAIN_READ, ["p_{rank}.txt"], ["q_{rank}.txt"], rank=5)])
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    assert e.value.problems[0].startswith("N→M ranked handoff: ranked step a (×3) provides 3 copies")


def test_pegasus_refuses_a_file_that_would_land_in_a_folder_input():
    spec = spec_of([node("a", PLAIN_WRITE_RANK, outputs=["p_{rank}.txt"], rank=2),
                    node("b", PLAIN_READ, [], ["p_total.txt"])])
    spec.nodes[1].inputs = [IOSpec(name="p_{rank}.txt", kind="folder")]
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    assert any("so p_total.txt (written by b) would land in it too" in p for p in e.value.problems)
