"""pegasus_ir.py (spec -> IR for the Pegasus target), without Pegasus: generated folders are
validated and expanded by curate_ir, then run job by job with condorio-style staging through the
plain runner (tests/ir_local.py)."""
import json
import sys
from pathlib import Path

import pytest

import emit
import pegasus_ir as P
import plain_contract as PC
from curate_models import IOSpec, WorkflowSpec
from ir_local import IR_DIR, run_locally

yaml = pytest.importorskip("yaml")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "e2-stages"
sys.path.insert(0, str(IR_DIR))
import validate  # noqa: E402

WORDS = {w: 1000 for w in ("cat", "dog", "bird", "horse", "pig")}


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
    folder = build_folder(tmp_path, e2_spec())
    ir = yaml.safe_load((folder / "ir.yaml").read_text())
    assert ir["ir_version"] == "0.2" and ir["name"] == "mapreduce-word-count"
    assert ir["axes"] == {"rank3": ["1", "2", "3"], "rank5": ["1", "2", "3", "4", "5"]}
    assert ir["inputs"] == []  # zero workflow inputs is fine
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
    assert (folder / "functions" / "map.py").read_text() == next(n.code for n in e2_spec().nodes if n.name == "map")
    assert (folder / "inputs").is_dir()


def test_e2_runs_end_to_end(tmp_path):
    files = run_locally(build_folder(tmp_path, e2_spec()) / "ir.yaml", tmp_path / "jobs")
    assert word_totals(files) == WORDS


def test_faasr_arguments_unchanged_for_the_faasr_emitter():
    spec = e2_spec()
    wf = emit.emit_faasr_json(spec)
    for n in spec.nodes:
        assert wf["ActionList"][emit._to_action_name(n.name)]["Arguments"] == emit.faasr_arguments(spec, n)


# ── ranked readers: their own copy, or every copy ──

def test_same_count_read_own_rank_is_paired(tmp_path):
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=3),
                    node("b", READ, ["p_{rank}.txt"], ["q_{rank}.txt"], rank=3)])
    folder = build_folder(tmp_path, spec)
    jobs = expanded(folder)
    assert jobs["b__2"]["inputs"] == {"input1": "p_2.txt"}
    files = run_locally(folder / "ir.yaml", tmp_path / "jobs")
    assert files["q_2.txt"].read_text() == "own:2"


def read_all_spec(reader_rank):
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=3),
                    node("b", READ, [], ["q_{rank}.txt"], rank=reader_rank)])
    spec.nodes[1].inputs = [IOSpec(name="p_{rank}.txt", read="all")]
    return spec


def test_same_count_read_all_becomes_a_gather(tmp_path):
    folder = build_folder(tmp_path, read_all_spec(3))
    ir = yaml.safe_load((folder / "ir.yaml").read_text())
    assert ir["axes"]["rank3_all"] == ["1", "2", "3"]
    jobs = expanded(folder)
    assert len(jobs) == 6
    for r in (1, 2, 3):  # every copy of b gets every copy of a's file, and waits for all of them
        assert jobs[f"b__{r}"]["inputs"] == {"input1": ["p_1.txt", "p_2.txt", "p_3.txt"]}
    files = run_locally(folder / "ir.yaml", tmp_path / "jobs")
    assert all(files[f"q_{r}.txt"].read_text() == "1,2,3" for r in (1, 2, 3))


def test_read_all_with_a_different_count_is_a_gather_too(tmp_path):
    files = run_locally(build_folder(tmp_path, read_all_spec(2)) / "ir.yaml", tmp_path / "jobs")
    assert files["q_2.txt"].read_text() == "1,2,3"


def test_read_own_all_and_folder_on_one_shape(tmp_path):
    for read, kind, want in [(None, "file", "own:2"), ("all", "file", "1,2,3"), (None, "folder", "1,2,3")]:
        spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=3),
                        node("b", READ, [], ["q_{rank}.txt"], rank=3)])
        spec.nodes[1].inputs = [IOSpec(name="p_{rank}.txt", read=read, kind=kind)]
        folder = build_folder(tmp_path / f"{read}-{kind}", spec)
        files = run_locally(folder / "ir.yaml", tmp_path / f"{read}-{kind}-jobs")
        assert files["q_2.txt"].read_text() == want, (read, kind)


def test_unranked_reader_gets_a_list(tmp_path):
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=2),
                    node("b", READ, ["p_{rank}.txt"], ["s.txt"])])
    files = run_locally(build_folder(tmp_path, spec) / "ir.yaml", tmp_path / "jobs")
    assert files["s.txt"].read_text() == "1,2"


# ── refusals, each with its own message ──

def test_a_step_that_is_not_plain_is_refused():
    old = "def a(folder, output1):\n    faasr_put_file(local_file='x', remote_folder=folder, remote_file=output1)\n"
    spec = spec_of([node("a", old, outputs=["o.txt"])])
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    assert e.value.problems == [
        "step a is not a plain main(inputs, outputs, params) function (the older FaaSr style): regenerate it, "
        "or migrate the catalog it came from (scripts/migrate_catalog.py)"]


def test_n_to_m_handoff_refused():
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=3),
                    node("b", READ, ["p_{rank}.txt"], ["q_{rank}.txt"], rank=5)])
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    (msg,) = e.value.problems
    assert msg.startswith("N→M ranked handoff: ranked step a (×3) provides 3 copies of p_{rank}.txt, "
                          "but ranked step b runs 5 ranks")
    assert "unranked step between them" in msg


def test_conditional_edge_refused_with_explicit_message():
    spec = spec_of([node("check", "def main(inputs, outputs, params):\n    return True\n"),
                    node("then_step", NOTHING)],
                   edges=[{"from_node": "check", "to_node": "then_step", "condition": "True"}])
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    assert e.value.problems == [
        "conditional edge check → then_step (taken only when check returns True): Pegasus runs a fixed DAG "
        "in which every step runs, so it cannot honour the condition. Remove the condition with a Gate 4 "
        "change request, or deploy to FaaSr."]


def test_secrets_missing_code_and_bad_names_refused_together():
    spec = spec_of([node("get", NOTHING, outputs=["a.txt"], secrets=["API_KEY"]),
                    node("Use__It", None, ["a.txt"], ["b.txt"])])
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    probs = e.value.problems
    assert any("reads secrets (API_KEY)" in p and "deploy to FaaSr" in p for p in probs)
    assert any(p.startswith("step Use__It has no code") for p in probs)
    assert any(p.startswith("step Use__It: Pegasus needs a name") for p in probs)


def test_ranked_writer_without_rank_placeholder_refused():
    spec = spec_of([node("a", WRITE_RANK, outputs=["same.txt"], rank=3)])
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    assert "its ranks would overwrite each other" in e.value.problems[0]


def test_folder_input_matching_nothing_is_refused():
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=2),
                    node("b", READ, [], ["s.txt"])])
    spec.nodes[1].inputs = [IOSpec(name="zz_{rank}.txt", kind="folder")]
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    assert any(p.startswith("step b reads the folder input zz_{rank}.txt (every file starting with 'zz_')")
               for p in e.value.problems)


def test_a_file_that_would_land_in_a_folder_input_is_refused():
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=2),
                    node("b", READ, [], ["p_total.txt"])])
    spec.nodes[1].inputs = [IOSpec(name="p_{rank}.txt", kind="folder")]
    with pytest.raises(P.MappingError) as e:
        P.build_ir(spec)
    assert any("so p_total.txt (written by b) would land in it too" in p for p in e.value.problems)


def test_packages_warn_only():
    spec = spec_of([node("a", NOTHING, outputs=["o.txt"], dependencies=["numpy", "json"])])
    built = P.build_ir(spec, image="docker:///me/img:1")
    assert built["warnings"] == ["step a imports numpy: these must already be in the image docker:///me/img:1; "
                                 "nothing is installed at run time"]
    assert built["ir"]["nodes"][0]["packages"] == ["numpy"]


# ── inputs, deliverables, ordering markers ──

def test_workflow_inputs_and_deliverables(tmp_path):
    spec = spec_of([node("a", READ, ["in_{rank}.csv"], ["out_{rank}.txt"], rank=2),
                    node("b", NOTHING, ["out_{rank}.txt"], ["sum.json"])])
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
    spec = spec_of([node("a", WRITE_RANK, outputs=["p_{rank}.txt"], rank=2),
                    node("b", WRITE_RANK, outputs=["q_{rank}.txt"], rank=2)])
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
    assert res["validation"]["warnings"] == []  # plain functions: nothing to warn about
    assert yaml.safe_load((out / "ir.yaml").read_text())["name"] == "wc-0117"
    assert P.main(["build", "--spec", str(FIXTURES / "spec-with-code.json"), "--out", str(out)]) == 2
    assert "already exists" in json.loads(capsys.readouterr().out)["error"]
