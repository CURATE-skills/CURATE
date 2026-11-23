"""curate_ir/cwl/from_ir.py: every IR v0.2 test workflow converts, and the result passes the CWL
validator; what plain CWL cannot express is refused with its own message."""
import json
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "curate_ir"))
sys.path.insert(0, str(PLUGIN / "scripts"))
import validate as V0  # noqa: E402
from cwl.from_ir import ConvertError, convert, write  # noqa: E402
from cwl.load import load  # noqa: E402
from cwl.validate import validate  # noqa: E402

EXAMPLES = PLUGIN / "curate_ir" / "examples"
FIXTURES = PLUGIN / "tests" / "fixtures"
PLAIN = "def main(inputs, outputs, params):\n    pass\n"


def to_cwl(ir_path, out):
    conv = convert(V0.load(ir_path))
    folder = write(conv, out)
    wf, errors, warnings = validate(folder / "workflow.yml", folder / "inputs.yml")
    assert errors == [], errors
    return wf, warnings


def spec_ir(spec_json, tmp_path):
    """An IR folder built from a WorkflowSpec by pegasus_ir (the production path)."""
    import pegasus_ir as P
    from curate_models import WorkflowSpec
    spec = WorkflowSpec.model_validate(spec_json)
    folder = P.write_folder(tmp_path / "ir", P.build_ir(spec, image="docker:///python:3.12-slim"))
    return folder / "ir.yaml"


def ir_file(tmp_path, nodes, **extra):
    (tmp_path / "functions").mkdir(exist_ok=True)
    for n in nodes:
        n.setdefault("code", f"functions/{n['id']}.py")
        (tmp_path / n["code"]).write_text(PLAIN)
    ir = {"ir_version": "0.2", "name": "t", "defaults": {"image": "docker:///python:3.12-slim"},
          "inputs": [], "nodes": nodes, **extra}
    path = tmp_path / "ir.yaml"
    path.write_text(yaml.safe_dump(ir, sort_keys=False))
    return path


# ── conversions ──────────────────────────────────────────────────────────────

def test_diamond(tmp_path):
    wf, warnings = to_cwl(EXAMPLES / "diamond" / "ir.yaml", tmp_path / "out")
    assert warnings == []
    assert list(wf.steps) == ["load_data", "freeze_up", "ice_off", "assemble"]
    assert wf.steps["freeze_up"].ins["run_days"].default == 5 and wf.steps["freeze_up"].op_inputs["run_days"].type == "int"
    assert wf.inputs["params_json"].value["path"] == str(EXAMPLES / "diamond" / "input" / "params.json")
    assert wf.outputs["ice_phenology_result_json"].deliver_as == "results/ice_phenology_result.json"
    assert wf.image == "python:3.12-slim" and wf.resources == {"coresMin": 1, "ramMin": 1024}
    assert (tmp_path / "out" / "functions" / "assemble.py").read_text() == \
        (EXAMPLES / "diamond" / "functions" / "assemble.py").read_text()


def test_fanout_pairs_and_gathers(tmp_path):
    wf, _ = to_cwl(EXAMPLES / "fanout" / "ir.yaml", tmp_path / "out")
    assert wf.inputs["winter"].type == "int[]" and wf.inputs["winter"].value == [2019, 2020, 2021, 2022]
    det, add, asm = wf.steps["detect_ice"], wf.steps["add_duration"], wf.steps["assemble"]
    assert det.scatter == ["winter"] and det.ins["daily"].sources == ["load_data/daily"]
    assert add.scatter == ["winter", "ice"] and add.scatter_method == "dotproduct"
    assert asm.op_inputs["rows"].type == "File[]" and asm.scatter == []


@pytest.mark.parametrize("which", ["e2-stages", "catalog-e2-mapreduce"])
def test_mapreduce_spec_through_build_ir(tmp_path, which):
    if which == "e2-stages":
        spec = json.loads((FIXTURES / "e2-stages" / "spec-with-code.json").read_text())
    else:
        (entry,) = (FIXTURES / "catalog-e2-mapreduce" / "workflows").glob("*.json")
        spec = json.loads(entry.read_text())["workflow_spec"]
    wf, warnings = to_cwl(spec_ir(spec, tmp_path), tmp_path / "out")
    assert warnings == []
    split, mp, shuffle, reduce_ = (wf.steps[k] for k in ("split", "map", "shuffle", "reduce"))
    assert split.op_outputs["output1"].type == "File[]" and split.op_outputs["output1"].axis == "rank3"
    assert mp.scatter == ["rank", "input1"] and mp.ins["rank"].sources == ["rank3"]
    assert mp.ins["max_rank"].default == 3
    assert shuffle.ins["input1"].folder and shuffle.ins["input1"].sources == ["map/output1"]
    assert shuffle.op_outputs["output1"].axis == "rank5"
    assert reduce_.scatter == ["rank", "input1"] and reduce_.ins["input1"].sources == ["shuffle/output1"]
    (out,) = wf.outputs.values()
    assert out.type == "File[]" and out.deliver_as == "results/word_count_{rank5}.json"


def test_list_output_folder_of_two_families_and_markers(tmp_path):
    path = ir_file(tmp_path, [
        {"id": "split", "outputs": {"b": "batch_{r}.txt"}},
        {"id": "use", "for_each": ["r"], "inputs": {"b": "batch_{r}.txt"},
         "outputs": {"o": "part_{r}.csv", "curate_done": "curate_done__use_{r}"}, "params": {"rank": "{r}"}},
        {"id": "extra", "outputs": {"x": "part_x.csv"}},
        {"id": "all", "inputs": {"parts": {"folder": ["part_{r}.csv", "part_x.csv"]},
                                 "curate_done_after_use": "curate_done__use_{r_all}"},
         "outputs": {"o": "summary.json"}},
    ], axes={"r": ["1", "2"], "r_all": ["1", "2"]}, outputs=[{"file": "summary.json"}])
    wf, _ = to_cwl(path, tmp_path / "out")
    use, al = wf.steps["use"], wf.steps["all"]
    assert use.ins["rank"].sources == ["r"] and use.op_inputs["rank"].type == "int"
    assert "curate_done" not in use.op_outputs and al.after == ["use"]
    assert al.ins["parts"].sources == ["use/o", "extra/x"] and al.ins["parts"].link_merge == "merge_flattened"
    assert "r_all" not in wf.inputs


def test_external_family_paired_and_string_axis(tmp_path):
    path = ir_file(tmp_path, [
        {"id": "m", "for_each": ["w"], "inputs": {"d": "data_{w}.csv"}, "outputs": {"o": "o_{w}.json"}},
    ], axes={"w": ["a", "b"]}, outputs=[{"file": "o_{w}.json", "deliver_as": "results/{w}/o.json"}])
    ir = yaml.safe_load(path.read_text())
    ir["inputs"] = [{"file": "data_{w}.csv", "location": "inputs/data_{w}.csv"}]
    path.write_text(yaml.safe_dump(ir))
    wf, _ = to_cwl(path, tmp_path / "out")
    m = wf.steps["m"]
    assert wf.inputs["w"].type == "string[]" and m.op_inputs["w"].type == "string"
    assert wf.inputs["data_w_csv"].type == "File[]"
    assert [v["path"] for v in wf.inputs["data_w_csv"].value] == [str(tmp_path / "inputs" / f"data_{x}.csv") for x in "ab"]
    assert m.scatter == ["w", "d"] and m.scatter_method == "dotproduct"
    assert wf.outputs["o_w_json"].deliver_as == "results/{w}/o.json"


def test_partial_gather_puts_the_kept_axis_outermost(tmp_path):
    path = ir_file(tmp_path, [
        {"id": "detect", "for_each": ["station", "winter"], "outputs": {"ice": "ice_{winter}_{station}.json"}},
        {"id": "assemble", "for_each": ["winter"], "inputs": {"ice": "ice_{winter}_{station}.json"},
         "outputs": {"row": "row_{winter}.json"}},
    ], axes={"winter": ["2019", "2020"], "station": ["a", "b", "c"]},
        outputs=[{"file": "row_{winter}.json", "deliver_as": "results/row_{winter}.json"}])
    wf, _ = to_cwl(path, tmp_path / "out")
    det = wf.steps["detect"]
    assert det.scatter == ["winter", "station"] and det.scatter_method == "nested_crossproduct"
    assert wf.steps["assemble"].op_inputs["ice"].type == "File[]"


def test_full_gather_over_two_axes(tmp_path):
    path = ir_file(tmp_path, [
        {"id": "m", "for_each": ["r", "w"], "outputs": {"o": "o_{r}_{w}"}, "params": {"rank": "{r}", "name": "{w}"}},
        {"id": "g", "inputs": {"all": "o_{r}_{w}"}, "outputs": {"res": "res.json"}},
    ], axes={"r": ["1", "2"], "w": ["a", "b"]}, outputs=[{"file": "res.json"}])
    wf, _ = to_cwl(path, tmp_path / "out")
    assert wf.steps["m"].ins["name"].sources == ["w"] and wf.steps["g"].op_inputs["all"].type == "File[][]"


# ── refusals ─────────────────────────────────────────────────────────────────

def refused(tmp_path, nodes, *parts, **extra):
    with pytest.raises(ConvertError) as e:
        convert(V0.load(ir_file(tmp_path, nodes, **extra)))
    assert any(all(p in msg for p in parts) for msg in e.value.problems), e.value.problems


def test_refuses_an_invalid_ir(tmp_path):
    refused(tmp_path, [{"id": "a", "inputs": {"i": "missing.txt"}, "outputs": {"o": "o"}}], "the IR is not valid")


def test_refuses_a_literal_member_of_a_family(tmp_path):
    refused(tmp_path, [{"id": "m", "for_each": ["r"], "outputs": {"o": "part_{r}.csv"}},
                       {"id": "u", "inputs": {"p": "part_1.csv"}, "outputs": {"o": "x"}}],
            "u input p", "matches no file family", axes={"r": ["1", "2"]})


def test_refuses_a_partial_placeholder_param(tmp_path):
    refused(tmp_path, [{"id": "m", "for_each": ["r"], "outputs": {"o": "o_{r}"}, "params": {"label": "copy {r}"}}],
            "param label='copy {r}' only partly holds an axis value", axes={"r": ["1", "2"]})


def test_refuses_a_dict_param(tmp_path):
    refused(tmp_path, [{"id": "m", "outputs": {"o": "o"}, "params": {"cfg": {"a": 1}}}], "param cfg is dict")


def test_refuses_a_per_copy_file_on_a_two_axis_step(tmp_path):
    refused(tmp_path, [{"id": "m", "for_each": ["r"], "outputs": {"o": "o_{r}"}},
                       {"id": "u", "for_each": ["r", "w"], "inputs": {"i": "o_{r}"}, "outputs": {"o": "u_{r}_{w}"}}],
            "one file per copy of a step over several axes", axes={"r": ["1", "2"], "w": ["a", "b"]})


def test_refuses_a_list_over_two_axes(tmp_path):
    refused(tmp_path, [{"id": "s", "outputs": {"o": "o_{r}_{w}"}}], "a list over several axes",
            axes={"r": ["1", "2"], "w": ["a", "b"]})


def test_refuses_two_groupings_of_one_producer(tmp_path):
    refused(tmp_path, [
        {"id": "d", "for_each": ["w", "s"], "outputs": {"o": "o_{w}_{s}"}},
        {"id": "byw", "for_each": ["w"], "inputs": {"i": "o_{w}_{s}"}, "outputs": {"o": "w_{w}"}},
        {"id": "bys", "for_each": ["s"], "inputs": {"i": "o_{w}_{s}"}, "outputs": {"o": "s_{s}"}},
    ], "consumers need different groupings", axes={"w": ["1", "2"], "s": ["a", "b"]})


def test_refuses_a_folder_that_changes_per_copy(tmp_path):
    refused(tmp_path, [{"id": "m", "for_each": ["r"], "outputs": {"o": "p_{r}"}},
                       {"id": "u", "for_each": ["r"], "inputs": {"f": {"folder": "p_{r}"}}, "outputs": {"o": "u_{r}"}}],
            "folder input f changes per copy", axes={"r": ["1", "2"]})


def test_write_refuses_an_existing_folder(tmp_path):
    conv = convert(V0.load(EXAMPLES / "diamond" / "ir.yaml"))
    (tmp_path / "out").mkdir()
    with pytest.raises(FileExistsError):
        write(conv, tmp_path / "out")


def test_a_file_less_edge_from_build_ir_becomes_curate_after(tmp_path):
    """pegasus_ir writes a marker file for an edge with no file; the CWL has curate:after instead."""
    plain = "def main(inputs, outputs, params):\n    open(list(outputs.values())[0], 'w').write('x')\n"
    spec = {"name": "t_flow", "folder": "tf",
            "nodes": [{"name": "a", "code": plain, "rank": 2, "inputs": [], "outputs": [{"name": "p_{rank}.txt"}]},
                      {"name": "b", "code": plain, "rank": 2, "inputs": [], "outputs": [{"name": "q_{rank}.txt"}]}],
            "edges": [{"from_node": "a", "to_node": "b"}]}
    ir = yaml.safe_load(spec_ir(spec, tmp_path).read_text())
    assert any(k.startswith("curate_done") for n in ir["nodes"] for k in n["outputs"])
    wf, _ = to_cwl(tmp_path / "ir" / "ir.yaml", tmp_path / "out")
    assert wf.steps["b"].after == ["a"]
    assert not any(k.startswith("curate_done") for s in wf.steps.values() for k in list(s.op_outputs) + list(s.ins))
