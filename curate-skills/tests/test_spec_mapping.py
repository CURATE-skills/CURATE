"""How a spec becomes a workflow (scripts/spec_cwl.py), checked through what the builders make of it:
the CWL validator, the Pegasus build (cwl_pegasus: one job per copy), and a local run job by job
with condorio-style staging through the plain runner (tests/ir_local.py). Until phase 8 these were
the tests of pegasus_ir.build_ir (now pegasus_target.py), spec -> IR v0.2; the shapes and refusals are the same.

Generated file names are <step>__<output>[__<rank>]__<spec file name>, so the checks name a file by
the step and output that write it and its rank."""
import json
import sys
from pathlib import Path

import pytest

import cwl_pegasus as CP
import plain_contract as PC
import spec_cwl
from curate_models import IOSpec, WorkflowSpec
from ir_local import IR_DIR, run_locally

sys.path.insert(0, str(IR_DIR))
from cwl.validate import validate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "e2-stages"
WORDS = {w: 1000 for w in ("cat", "dog", "bird", "horse", "pig")}


def e2_spec() -> WorkflowSpec:
    return WorkflowSpec.model_validate(json.loads((FIXTURES / "spec-with-code.json").read_text()))


def workflow(tmp_path, spec):
    folder = spec_cwl.write(spec_cwl.build(spec), tmp_path / "cwl")
    wf, errors, _ = validate(folder / "workflow.yml", folder / "inputs.yml")
    assert errors == [], errors
    return wf


def pegasus(tmp_path, spec) -> tuple[dict, Path]:
    """({job id: job}, the Pegasus folder) for a spec."""
    built = CP.build(workflow(tmp_path, spec), "t")
    folder = CP.write_folder(tmp_path / "pegasus", built)
    return {n["id"]: n for n in built["jobs"]}, folder


def run(tmp_path, spec) -> dict:
    """Every file the jobs write, by the spec's own file name with {rank} filled ({name: path})."""
    _, folder = pegasus(tmp_path, spec)
    files = run_locally(folder / "workflow.yml", tmp_path / "jobs")
    return {spec_name(k): v for k, v in files.items()}


def spec_name(generated: str) -> str:
    """The spec's file name at the end of a generated name (markers have none)."""
    parts = generated.split("__")
    return parts[-1] if len(parts) >= 3 else generated


def out(step: str, rank=None) -> str:
    """The generated name's start for a step's output1 (and its copy)."""
    return f"{step}__output1__" + (f"{rank}__" if rank else "")


def node(name, code, inputs=(), outputs=(), rank=1, **kw):
    return {"name": name, "code": code, "rank": rank, "inputs": [{"name": i} for i in inputs],
            "outputs": [{"name": o} for o in outputs], **kw}


def spec_of(nodes, edges=None, **kw):
    if edges is None:
        edges = [{"from_node": a["name"], "to_node": b["name"]} for a, b in zip(nodes, nodes[1:])]
    return WorkflowSpec.model_validate({"name": "t_flow", "folder": "tf", "nodes": nodes, "edges": edges, **kw})


def refused(spec) -> list[str]:
    with pytest.raises(spec_cwl.BuildError) as e:
        spec_cwl.build(spec)
    return e.value.problems


def word_totals(files):
    totals = {}
    for i in range(1, 6):
        for word, n in json.loads(files[f"word_count_{i}.json"].read_text()).items():
            assert word not in totals  # each reducer owns its words
            totals[word] = n
    return totals


# Small plain functions for the shape tests.
WRITE_RANK = '''
def main(inputs, outputs, params):
    open(outputs["output1"], "w").write(str(params["rank"]))
'''
READ = '''
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
NOTHING = "def main(inputs, outputs, params):\n    pass\n"


# ── the e2 MapReduce fixture ──

def test_e2_fixture_is_plain():
    spec = e2_spec()
    assert all(PC.is_plain(n.code) for n in spec.nodes)
    for n in spec.nodes:
        assert n.code == (FIXTURES / "functions" / f"{n.name}.py").read_text()


def test_e2_mapping(tmp_path):
    wf = workflow(tmp_path, e2_spec())
    assert {a: wf.inputs[a].value for a in ("rank3", "rank5")} == {"rank3": [1, 2, 3], "rank5": [1, 2, 3, 4, 5]}
    assert not [i for i in wf.inputs if not i.startswith("rank")]  # zero workflow input files is fine
    assert wf.steps["map"].ins["max_rank"].default == 3 and wf.steps["map"].scatter == ["rank", "input1"]
    assert wf.steps["split"].scatter == []
    assert wf.steps["shuffle"].ins["input1"].folder
    jobs, folder = pegasus(tmp_path / "p", e2_spec())
    assert len(jobs) == 11
    split = jobs["split"]["outputs"]["output1"]
    assert [spec_name(f) for f in split] == [f"text_batch_{i}.txt" for i in (1, 2, 3)]           # a list
    assert jobs["map__2"]["inputs"]["input1"].startswith(out("split")) and \
        spec_name(jobs["map__2"]["inputs"]["input1"]) == "text_batch_2.txt"                       # own copy
    assert jobs["map__2"]["params"] == {"rank": 2, "max_rank": 3}                                 # ints
    assert [spec_name(f) for f in jobs["shuffle"]["inputs"]["input1"]["folder"]] == \
        [f"map_counts_{i}.json" for i in (1, 2, 3)]
    assert [spec_name(f) for f in jobs["shuffle"]["outputs"]["output1"]] == \
        [f"shuffle_partition_{i}.json" for i in range(1, 6)]
    assert [o.deliver_as for o in wf.outputs.values()] == ["results/word_count_{rank5}.json"]  # folder-read files are not end files
    assert (folder / "functions" / "map.py").read_text() == next(n.code for n in e2_spec().nodes if n.name == "map")


def test_e2_runs_end_to_end(tmp_path):
    assert word_totals(run(tmp_path, e2_spec())) == WORDS


# ── ranked readers: their own copy, or every copy ──

def test_same_count_read_own_rank_is_paired(tmp_path):
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=3),
                    node("b", READ, ["p_{rank}.txt"], ["q_{rank}.txt"], rank=3)])
    jobs, _ = pegasus(tmp_path / "p", spec)
    assert jobs["b__2"]["inputs"]["input1"].startswith(out("a", 2))
    assert run(tmp_path, spec)["q_2.txt"].read_text() == "own:2"


def read_all_spec(reader_rank):
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=3),
                    node("b", READ, [], ["q_{rank}.txt"], rank=reader_rank)])
    spec.nodes[1].inputs = [IOSpec(name="p_{rank}.txt", read="all")]
    return spec


def test_same_count_read_all_becomes_a_gather(tmp_path):
    jobs, _ = pegasus(tmp_path / "p", read_all_spec(3))
    assert len(jobs) == 6
    for r in (1, 2, 3):  # every copy of b gets every copy of a's file, and waits for all of them
        assert [f[:len(out("a", 1))] for f in jobs[f"b__{r}"]["inputs"]["input1"]] == [out("a", i) for i in (1, 2, 3)]
    files = run(tmp_path, read_all_spec(3))
    assert all(files[f"q_{r}.txt"].read_text() == "1,2,3" for r in (1, 2, 3))


def test_read_all_with_a_different_count_is_a_gather_too(tmp_path):
    assert run(tmp_path, read_all_spec(2))["q_2.txt"].read_text() == "1,2,3"


def test_read_own_all_and_folder_on_one_shape(tmp_path):
    for read, kind, want in [(None, "file", "own:2"), ("all", "file", "1,2,3"), (None, "folder", "1,2,3")]:
        spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=3),
                        node("b", READ, [], ["q_{rank}.txt"], rank=3)])
        spec.nodes[1].inputs = [IOSpec(name="p_{rank}.txt", read=read, kind=kind)]
        assert run(tmp_path / f"{read}-{kind}", spec)["q_2.txt"].read_text() == want, (read, kind)


def test_unranked_reader_gets_a_list(tmp_path):
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=2),
                    node("b", READ, ["p_{rank}.txt"], ["s.txt"])])
    assert run(tmp_path, spec)["s.txt"].read_text() == "1,2"


# ── refusals, each with its own message ──

def test_a_step_that_is_not_plain_is_refused():
    old = "def a(folder, output1):\n    faasr_put_file(local_file='x', remote_folder=folder, remote_file=output1)\n"
    assert refused(spec_of([node("a", old, outputs=["o.txt"])])) == [
        "step a is not a plain main(inputs, outputs, params) function (the older FaaSr style): regenerate it, "
        "or migrate the catalog it came from (scripts/migrate_catalog.py)"]


def test_n_to_m_handoff_refused():
    (msg,) = refused(spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=3),
                              node("b", READ, ["p_{rank}.txt"], ["q_{rank}.txt"], rank=5)]))
    assert msg.startswith("N→M ranked handoff: ranked step a (×3) provides 3 copies of p_{rank}.txt, "
                          "but ranked step b runs 5 ranks")
    assert "unranked step between them" in msg


def test_a_conditional_edge_is_carried_and_pegasus_refuses_it(tmp_path):
    spec = spec_of([node("check", "def main(inputs, outputs, params):\n    return True\n"),
                    node("then_step", NOTHING)],
                   edges=[{"from_node": "check", "to_node": "then_step", "condition": "True"}])
    wf = workflow(tmp_path, spec)
    assert wf.steps["then_step"].run_if == {"step": "check", "result": True}
    with pytest.raises(CP.BuildError) as e:
        CP.build(wf, "t")
    assert any("step then_step runs only if check returns True (curate:run_if)" in p and "Deploy to FaaSr" in p
               for p in e.value.problems)


def test_missing_code_and_bad_names_refused_together_secrets_carried(tmp_path):
    probs = refused(spec_of([node("get", NOTHING, outputs=["a.txt"], secrets=["API_KEY"]),
                             node("Use__It", None, ["a.txt"], ["b.txt"])]))
    assert any(p.startswith("step Use__It has no code") for p in probs)
    assert any(p.startswith("step Use__It: Pegasus needs a name") for p in probs)
    assert not any("API_KEY" in p for p in probs)
    wf = workflow(tmp_path, spec_of([node("get", NOTHING, outputs=["a.txt"], secrets=["API_KEY"])]))
    assert wf.steps["get"].secrets == ["API_KEY"]


def test_ranked_writer_without_rank_placeholder_refused():
    assert "its ranks would overwrite each other" in refused(spec_of([node("a", WRITE_RANK, outputs=["same.txt"], rank=3)]))[0]


def test_folder_input_matching_nothing_is_refused():
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=2),
                    node("b", READ, [], ["s.txt"])])
    spec.nodes[1].inputs = [IOSpec(name="zz_{rank}.txt", kind="folder")]
    assert any(p.startswith("step b reads the folder input zz_{rank}.txt (every file starting with 'zz_')")
               for p in refused(spec))


def test_a_file_that_would_land_in_a_folder_input_is_refused():
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=2),
                    node("b", READ, [], ["p_total.txt"])])
    spec.nodes[1].inputs = [IOSpec(name="p_{rank}.txt", kind="folder")]
    assert any("so p_total.txt (written by b) would land in it too" in p for p in refused(spec))


def test_packages_warn_only():
    built = spec_cwl.build(spec_of([node("a", NOTHING, outputs=["o.txt"], dependencies=["numpy", "json"])]),
                           image="docker:///me/img:1")
    assert built["warnings"] == ["step a imports numpy: these must already be in the image docker:///me/img:1; "
                                 "nothing is installed at run time"]
    assert built["workflow"]["steps"]["a"]["hints"]["SoftwareRequirement"] == {"packages": [{"package": "numpy"}]}


# ── inputs, deliverables, ordering ──

def test_workflow_inputs_and_deliverables(tmp_path):
    spec = spec_of([node("a", READ, ["in_{rank}.csv"], ["out_{rank}.txt"], rank=2),
                    node("b", NOTHING, ["out_{rank}.txt"], ["sum.json"])])
    built = CP.build(workflow(tmp_path, spec), "t")
    assert built["needed"] == ["in_1.csv", "in_2.csv"]
    assert [f for f, p in CP.metadata(built["workflow"])["replicas"].items() if p.startswith("inputs/")] == \
        ["in_1.csv", "in_2.csv"]
    assert [o["deliver_as"] for o in built["deliverables"]] == ["results/sum.json"]


def test_edge_without_a_file_makes_the_later_step_wait_for_all_copies(tmp_path):
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=2),
                    node("b", WRITE_RANK, outputs=["q_{rank}.txt"], rank=2)])
    wf = workflow(tmp_path / "w", spec)
    assert wf.steps["b"].after == ["a"]
    jobs, _ = pegasus(tmp_path / "p", spec)
    assert jobs["b__1"]["after"] == ["a__1", "a__2"]
    assert run(tmp_path, spec)["q_2.txt"].read_text() == "2"
