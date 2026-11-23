"""curate_ir/cwl/load.py and validate.py: every profile refusal, every allowed pattern."""
import copy
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "curate_ir"))
from cwl import NS  # noqa: E402
from cwl.load import load  # noqa: E402
from cwl.validate import validate  # noqa: E402

EXAMPLES = PLUGIN / "curate_ir" / "cwl" / "examples"
PLAIN = "def main(inputs, outputs, params):\n    pass\n"


def op(inputs=None, outputs=None):
    return {"class": "Operation", "inputs": inputs or {}, "outputs": outputs or {}}


def step(sid, ins=None, outs=None, op_in=None, op_out=None, **kw):
    s = {"in": ins or {}, "out": outs or [], "run": op(op_in, op_out),
         "hints": {"curate:function": {"code": f"functions/{sid}.py", "entrypoint": "main"}}}
    s.update(kw)
    return s


def base():
    """make -> use: the smallest valid workflow."""
    return {
        "cwlVersion": "v1.2", "class": "Workflow",
        "$namespaces": {"curate": NS},
        "requirements": {"ScatterFeatureRequirement": {}},
        "hints": {"DockerRequirement": {"dockerPull": "python:3.12-slim"}},
        "inputs": {"rank3": {"type": "int[]", "default": [1, 2, 3]}},
        "outputs": {"result": {"type": "File", "outputSource": "use/output1",
                               "curate:deliver_as": "results/result.json"}},
        "steps": {
            "make": step("make", {"rank": "rank3"}, ["output1"], {"rank": "int"}, {"output1": "File"},
                         scatter="rank"),
            "use": step("use", {"input1": "make/output1"}, ["output1"], {"input1": "File[]"}, {"output1": "File"}),
        },
    }


def write(tmp_path, doc, name="workflow.yml", functions=None, inputs=None):
    (tmp_path / "functions").mkdir(exist_ok=True)
    for sid in doc.get("steps", {}) if isinstance(doc.get("steps"), dict) else []:
        f = tmp_path / "functions" / f"{sid}.py"
        if not f.exists():
            f.write_text((functions or {}).get(sid, PLAIN))
    path = tmp_path / name
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    ipath = None
    if inputs is not None:
        ipath = tmp_path / "inputs.yml"
        ipath.write_text(yaml.safe_dump(inputs))
    return path, ipath


def run(tmp_path, doc, **kw):
    path, ipath = write(tmp_path, doc, **kw)
    return validate(path, ipath)


def errors_of(tmp_path, doc, **kw):
    return run(tmp_path, doc, **kw)[1]


def assert_error(errors, *parts):
    assert any(all(p in e for p in parts) for e in errors), errors


# ── allowed patterns ─────────────────────────────────────────────────────────

def test_base_is_valid(tmp_path):
    wf, errors, warnings = run(tmp_path, base())
    assert errors == [] and warnings == []
    assert list(wf.steps) == ["make", "use"]


@pytest.mark.parametrize("name,inputs", [("weather", None), ("features", None), ("features", "inputs.yml")])
def test_examples_are_valid(name, inputs):
    wf, errors, warnings = validate(EXAMPLES / name / "workflow.yml",
                                    EXAMPLES / name / inputs if inputs else None)
    assert errors == [] and warnings == []


def test_loader_reads_the_extensions_into_plain_fields():
    wf = load(EXAMPLES / "features" / "workflow.yml", EXAMPLES / "features" / "inputs.yml")
    assert wf.image == "python:3.12-slim"
    assert wf.inputs["rank5"].value == [1, 2, 3, 4, 5] and wf.inputs["rank5"].type == "int[]"
    assert wf.inputs["batches"].type == "File[]"
    assert wf.inputs["batches"].value[0]["path"] == str(EXAMPLES / "features" / "inputs" / "batch_1.txt")
    m, sh, rp, cl = (wf.steps[k] for k in ("map", "shuffle", "report", "cleanup"))
    assert m.scatter == ["rank", "input1"] and m.scatter_method == "dotproduct"
    assert m.ins["input1"].sources == ["batches"] and m.ins["max_rank"].default == 3
    assert m.secrets == ["API_TOKEN"] and m.function == {"code": "functions/map.py", "entrypoint": "main"}
    assert sh.ins["input1"].folder and sh.ins["input1"].link_merge == "merge_flattened"
    assert sh.ins["input1"].sources == ["map/output1", "notes"]
    assert sh.op_outputs["output1"].type == "File[]" and sh.op_outputs["output1"].axis == "rank5"
    assert rp.run_if == {"step": "check", "result": True} and cl.after == ["report"]
    assert wf.outputs["report_json"].deliver_as == "results/report.json"


def test_loader_reads_identity_images_and_packages():
    wf = load(EXAMPLES / "weather" / "workflow.yml")
    assert (wf.identifier, wf.version) == ("corvallis-winter-weather-v1", "1")
    assert wf.content_hash.startswith("sha256:") and wf.label.startswith("Corvallis")
    assert wf.resources == {"coresMin": 1, "ramMin": 2048}
    plot = wf.steps["plot_weather_comparison"]
    assert plot.image == "ghcr.io/example/python-plot:3.12" and plot.packages == ["pandas", "matplotlib"]
    assert wf.steps["download_ghcn_station"].image is None
    assert wf.outputs["comparisons"].deliver_as == "results/winter_comparison_{rank3}.csv"


def test_any_file_extension(tmp_path):
    wf, errors, _ = run(tmp_path, base(), name="pipeline.cwl.txt")
    assert errors == [] and wf.path.name == "pipeline.cwl.txt"


def test_inputs_file_replaces_defaults_and_sets_the_fan_out(tmp_path):
    doc = base()
    del doc["inputs"]["rank3"]["default"]
    assert_error(errors_of(tmp_path, doc), "workflow input rank3 has no value")
    wf, errors, _ = run(tmp_path, doc, inputs={"rank3": [1, 2, 3, 4]})
    assert errors == [] and wf.inputs["rank3"].value == [1, 2, 3, 4]


def test_string_axis_and_folder_and_list_gather(tmp_path):
    doc = base()
    doc["inputs"] = {"winter": {"type": "string[]", "default": ["2019", "2020"]}}
    doc["steps"]["make"]["in"] = {"w": "winter"}
    doc["steps"]["make"]["run"]["inputs"] = {"w": "string"}
    doc["steps"]["make"]["scatter"] = "w"
    doc["steps"]["use"]["in"] = {"input1": {"source": "make/output1", "curate:folder": True}}
    assert errors_of(tmp_path, doc) == []


def partial(order=("winter", "station")):
    doc = base()
    doc["inputs"] = {"winter": {"type": "string[]", "default": ["2019", "2020"]},
                     "station": {"type": "string[]", "default": ["a", "b", "c"]}}
    doc["steps"] = {
        "detect": step("detect", {"winter": "winter", "station": "station"}, ["ice"],
                       {"winter": "string", "station": "string"}, {"ice": "File"},
                       scatter=list(order), scatterMethod="nested_crossproduct"),
        "assemble": step("assemble", {"winter": "winter", "ice": "detect/ice"}, ["out"],
                         {"winter": "string", "ice": "File[]"}, {"out": "File"},
                         scatter=["winter", "ice"], scatterMethod="dotproduct"),
    }
    doc["outputs"] = {"rows": {"type": "File[]", "outputSource": "assemble/out",
                               "curate:deliver_as": "results/row_{winter}.json"}}
    return doc


def test_partial_gather_with_the_kept_axis_outermost(tmp_path):
    assert errors_of(tmp_path, partial()) == []


def test_full_gather_of_a_two_axis_step_is_nested(tmp_path):
    doc = partial()
    doc["steps"]["assemble"] = step("assemble", {"ice": "detect/ice"}, ["out"], {"ice": "File[][]"}, {"out": "File"})
    doc["outputs"] = {"all": {"type": "File[][]", "outputSource": "detect/ice",
                              "curate:deliver_as": "results/ice_{winter}_{station}.json"},
                      "out": {"type": "File", "outputSource": "assemble/out"}}
    assert errors_of(tmp_path, doc) == []


def test_flat_crossproduct(tmp_path):
    doc = partial()
    doc["steps"]["detect"]["scatterMethod"] = "flat_crossproduct"
    doc["steps"]["assemble"] = step("assemble", {"ice": "detect/ice"}, ["out"], {"ice": "File[]"}, {"out": "File"})
    doc["outputs"] = {"out": {"type": "File", "outputSource": "assemble/out"}}
    assert errors_of(tmp_path, doc) == []


def test_requirements_or_hints_and_step_image_override(tmp_path):
    doc = base()
    doc["requirements"]["DockerRequirement"] = doc.pop("hints")["DockerRequirement"]
    doc["steps"]["use"]["hints"]["DockerRequirement"] = {"dockerPull": "other:1"}
    doc["steps"]["use"]["hints"]["ResourceRequirement"] = {"coresMin": 2, "ramMin": 4096}
    wf, errors, _ = run(tmp_path, doc)
    assert errors == [] and wf.image == "python:3.12-slim" and wf.steps["use"].image == "other:1"


# ── refusals ─────────────────────────────────────────────────────────────────

def mutate(fn):
    doc = base()
    fn(doc)
    return doc


REFUSALS = [
    ("when", lambda d: d["steps"]["use"].update(when="$(true)"), ["`when` is refused", "curate:run_if"]),
    ("pickValue", lambda d: d["steps"]["use"]["in"].update(input1={"source": "make/output1", "pickValue": "all_non_null"}),
     ["`pickValue` is refused"]),
    ("javascript", lambda d: d["requirements"].update(InlineJavascriptRequirement={}), ["InlineJavascriptRequirement is refused"]),
    ("expression", lambda d: d["steps"]["use"]["in"].update(n={"default": "$(inputs.x)"}), ["expressions"]),
    ("expression braces", lambda d: d.update(doc="${return 1}"), ["expressions"]),
    ("expressiontool", lambda d: d["steps"]["use"].update(run={"class": "ExpressionTool", "inputs": {}, "outputs": {}, "expression": "x"}),
     ["is an ExpressionTool"]),
    ("commandlinetool", lambda d: d["steps"]["use"].update(run={"class": "CommandLineTool", "inputs": {}, "outputs": {}}),
     ["is a CommandLineTool"]),
    ("subworkflow inline", lambda d: d["steps"]["use"].update(run={"class": "Workflow", "inputs": {}, "outputs": {}, "steps": {}}),
     ["is a sub-workflow"]),
    ("run file", lambda d: d["steps"]["use"].update(run="tool.cwl"), ["refers to another file"]),
    ("subworkflow requirement", lambda d: d["requirements"].update(SubworkflowFeatureRequirement={}), ["SubworkflowFeatureRequirement is refused"]),
    ("valueFrom", lambda d: d["steps"]["use"]["in"].update(input1={"source": "make/output1", "valueFrom": "x"}), ["`valueFrom` is refused"]),
    ("secondaryFiles", lambda d: d["inputs"].update(f={"type": "File", "secondaryFiles": [".bai"]}), ["`secondaryFiles` is refused"]),
    ("loop", lambda d: d["requirements"].update({"cwltool:Loop": {}}), ["loops are refused"]),
    ("other requirement", lambda d: d["requirements"].update(EnvVarRequirement={"envDef": {}}), ["EnvVarRequirement is not in the CURATE profile"]),
    ("linkMerge flattened without folder", lambda d: d["steps"]["use"]["in"].update(
        input1={"source": ["make/output1"], "linkMerge": "merge_flattened"}), ["merge_flattened is allowed only on a curate:folder"]),
    ("several sources", lambda d: d["steps"]["use"]["in"].update(input1={"source": ["make/output1", "make/output1"]}),
     ["several sources are allowed only on a curate:folder"]),
    ("folder sources without flatten", lambda d: d["steps"]["use"]["in"].update(
        input1={"source": ["make/output1", "make/output1"], "curate:folder": True}), ["needs linkMerge: merge_flattened"]),
    ("cwl version", lambda d: d.update(cwlVersion="v1.0"), ["cwlVersion must be v1.2"]),
    ("top class", lambda d: d.update({"class": "CommandLineTool"}), ["top-level class must be Workflow"]),
    ("duplicate ids", lambda d: d["outputs"].update(use={"type": "File", "outputSource": "use/output1"}), ["ids used twice", "use"]),
    ("step id", lambda d: d["steps"].update({"Bad-Step": step("x")}), ["step ids must match"]),
    ("function in workflow hints", lambda d: d["hints"].update({"curate:function": {"code": "x"}}), ["belongs in a step's hints"]),
    ("curate under requirements", lambda d: d["steps"]["use"].update(requirements={"curate:secrets": {"names": ["A"]}}),
     ["goes under hints, not requirements"]),
]


@pytest.mark.parametrize("name,fn,parts", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_profile_refusals(tmp_path, name, fn, parts):
    assert_error(errors_of(tmp_path, mutate(fn)), *parts)


STRUCTURAL = [
    ("no function", lambda d: d["steps"]["use"]["hints"].pop("curate:function"), ["step use: needs hints: curate:function"]),
    ("code missing", lambda d: d["steps"]["use"]["hints"]["curate:function"].update(code="functions/nope.py"),
     ["code file not found: functions/nope.py"]),
    ("entrypoint", lambda d: d["steps"]["use"]["hints"]["curate:function"].update(entrypoint="run"), ["entrypoint run() not in"]),
    ("function in Operation hints", lambda d: d["steps"]["use"]["run"].update(hints={"curate:after": {"steps": ["make"]}}),
     ["belongs in a step's hints"]),
    ("deliver_as as step hint", lambda d: d["steps"]["use"]["hints"].update({"curate:deliver_as": {"x": 1}}),
     ["curate:deliver_as is not a step extension"]),
    ("no image", lambda d: d.pop("hints"), ["step make: no image"]),
    ("docker build", lambda d: d["hints"].update(DockerRequirement={"dockerPull": "x", "dockerFile": "y"}), ["takes only dockerPull"]),
    ("resource keys", lambda d: d["steps"]["use"]["hints"].update(ResourceRequirement={"coresMax": 4}), ["coresMin and ramMin"]),
    ("directory output", lambda d: d["steps"]["use"]["run"]["outputs"].update(output1="Directory"),
     ["Directory is allowed only as a workflow input"]),
    ("optional type", lambda d: d["steps"]["use"]["run"]["inputs"].update(input1="File[]?"), ["optional and union types"]),
    ("in entry not in op", lambda d: d["steps"]["use"]["in"].update(extra="make/output1"), ["in entry extra is not an input"]),
    ("op input unfed", lambda d: d["steps"]["use"]["run"]["inputs"].update(n="int"), ["Operation input n gets no source and no default"]),
    ("default type", lambda d: (d["steps"]["use"]["run"]["inputs"].update(n="int"), d["steps"]["use"]["in"].update(n={"default": "x"})),
     ["the default of n does not match type int"]),
    ("out missing", lambda d: d["steps"]["use"].update(out=[]), ["Operation output output1 is missing from out"]),
    ("unknown source", lambda d: d["steps"]["use"]["in"].update(input1="nope/output1"), ["source nope/output1 is not an output"]),
    ("type mismatch", lambda d: d["steps"]["use"]["run"]["inputs"].update(input1="File"), ["receives File[] but its Operation input is File"]),
    ("scatter a single file", lambda d: (d["steps"]["use"].update(scatter="input1"),
                                         d["steps"]["use"]["in"].update(input1="make/output1"),
                                         d["steps"]["use"]["run"]["inputs"].update(input1="File[]")),
     ["receives File but its Operation input is File[]"]),
    ("dotproduct lengths", lambda d: (d["inputs"].update(rank2={"type": "int[]", "default": [1, 2]}),
                                      d["steps"]["use"].update(scatter=["input1", "r"], scatterMethod="dotproduct"),
                                      d["steps"]["use"]["in"].update(r="rank2"),
                                      d["steps"]["use"]["run"]["inputs"].update(input1="File", r="int"),
                                      d["outputs"]["result"].update(type="File[]", **{"curate:deliver_as": "r/{rank3}"})),
     ["dotproduct pairs lists of different lengths"]),
    ("axis not file-name safe", lambda d: d["inputs"]["rank3"].update(type="string[]", default=["a b", "c", "d"]),
     ["values not file-name safe"]),
    ("axis duplicates", lambda d: d["inputs"]["rank3"].update(default=[1, 1, 2]), ["axis rank3: duplicate values"]),
    ("axis empty", lambda d: d["inputs"]["rank3"].update(default=[]), ["axis rank3 has no values"]),
    ("deliver list without placeholders", lambda d: d["outputs"].update(
        parts={"type": "File[]", "outputSource": "make/output1", "curate:deliver_as": "results/part.csv"}),
     ["must hold exactly its axes {rank3}"]),
    ("deliver list missing", lambda d: d["outputs"].update(parts={"type": "File[]", "outputSource": "make/output1"}),
     ["is a list: give curate:deliver_as"]),
    ("deliver file with placeholder", lambda d: d["outputs"]["result"].update(**{"curate:deliver_as": "r_{rank3}.json"}),
     ["must hold exactly its axes (none)"]),
    ("deliver absolute", lambda d: d["outputs"]["result"].update(**{"curate:deliver_as": "/tmp/x"}), ["relative path under deliver/"]),
    ("output type", lambda d: d["outputs"]["result"].update(type="File[]", **{"curate:deliver_as": "x_{rank3}"}),
     ["its source carries File but the output is File[]"]),
    ("after unknown", lambda d: d["steps"]["use"]["hints"].update({"curate:after": {"steps": ["ghost"]}}), ["curate:after names 'ghost'"]),
    ("after shape", lambda d: d["steps"]["use"]["hints"].update({"curate:after": {"steps": "make"}}), ["curate:after needs {steps"]),
    ("run_if shape", lambda d: d["steps"]["use"]["hints"].update({"curate:run_if": {"step": "make", "result": "yes"}}),
     ["curate:run_if needs {step"]),
    ("run_if unknown", lambda d: d["steps"]["use"]["hints"].update({"curate:run_if": {"step": "ghost", "result": True}}),
     ["curate:run_if names 'ghost'"]),
    ("cycle", lambda d: d["steps"]["make"]["hints"].update({"curate:after": {"steps": ["use"]}}), ["cycle among steps"]),
    ("secret values", lambda d: d["steps"]["use"]["hints"].update({"curate:secrets": {"names": ["api_key=123"]}}),
     ["names only (UPPER_SNAKE)"]),
    ("folder on a file", lambda d: (d["steps"]["use"]["in"].update(input1={"source": "make/output1", "curate:folder": True}),
                                    d["steps"]["use"]["run"]["inputs"].update(input1="File")),
     ["curate:folder input input1 must be of type File[]"]),
    ("list output without axis", lambda d: d["steps"]["use"]["run"]["outputs"].update(output1="File[]"),
     ["name its axis with curate:axis"]),
    ("axis on a file output", lambda d: d["steps"]["use"]["run"]["outputs"].update(output1={"type": "File", "curate:axis": "rank3"}),
     ["curate:axis is only for File[] outputs"]),
    ("axis not an axis", lambda d: (d["inputs"].update(f={"type": "File", "default": {"class": "File", "location": "x"}}),
                                    d["steps"]["use"]["run"]["outputs"].update(output1={"type": "File[]", "curate:axis": "f"})),
     ["curate:axis f is not an axis"]),
]


@pytest.mark.parametrize("name,fn,parts", STRUCTURAL, ids=[r[0] for r in STRUCTURAL])
def test_structural_refusals(tmp_path, name, fn, parts):
    assert_error(errors_of(tmp_path, mutate(fn)), *parts)


def test_faasr_code_is_refused(tmp_path):
    errors = errors_of(tmp_path, base(), functions={"use": "def main(inputs, outputs, params):\n    faasr_log('x')\n"})
    assert_error(errors, "calls the FaaSr API")


def test_secrets_as_a_bare_list_is_refused(tmp_path):
    doc = mutate(lambda d: d["steps"]["use"]["hints"].update({"curate:secrets": ["API_KEY"]}))
    assert_error(errors_of(tmp_path, doc), "curate:secrets needs {names: [NAME, ...]}")


def test_inputs_file_with_an_unknown_key_or_bad_value(tmp_path):
    assert_error(errors_of(tmp_path, base(), inputs={"nope": 1}), "inputs file: nope is not an input")
    assert_error(errors_of(tmp_path, base(), inputs={"rank3": "x"}), "workflow input rank3: its value does not match")


def test_partial_gather_in_the_wrong_order(tmp_path):
    assert_error(errors_of(tmp_path, partial(order=("station", "winter"))),
                 "partial gather of detect/ice", "must list winter first", "now station is outermost")


def test_two_consumers_needing_different_groupings(tmp_path):
    doc = partial()
    doc["steps"]["by_station"] = step("by_station", {"station": "station", "ice": "detect/ice"}, ["out"],
                                      {"station": "string", "ice": "File[]"}, {"out": "File"},
                                      scatter=["station", "ice"], scatterMethod="dotproduct")
    assert_error(errors_of(tmp_path, doc), "detect/ice: consumers need different groupings",
                 "assemble by winter", "by_station by station", "without JavaScript")


def test_a_curate_axis_list_paired_with_another_axis(tmp_path):
    doc = base()
    doc["inputs"]["rank5"] = {"type": "int[]", "default": [1, 2, 3, 4, 5]}
    doc["steps"] = {
        "split": step("split", {}, ["output1"], {}, {"output1": {"type": "File[]", "curate:axis": "rank5"}}),
        "use": step("use", {"input1": "split/output1", "rank": "rank3"}, ["output1"],
                    {"input1": "File", "rank": "int"}, {"output1": "File"},
                    scatter=["rank", "input1"], scatterMethod="dotproduct"),
    }
    doc["outputs"] = {"result": {"type": "File[]", "outputSource": "use/output1", "curate:deliver_as": "r_{rank3}"}}
    errors = errors_of(tmp_path, doc)
    assert_error(errors, "dotproduct pairs different axes", "same axis")
    doc["steps"]["use"]["in"]["rank"] = "rank5"
    doc["outputs"]["result"]["curate:deliver_as"] = "r_{rank5}"
    assert errors_of(tmp_path, doc) == []


def test_an_unread_axis_list_warns(tmp_path):
    doc = base()
    doc["steps"]["extra"] = step("extra", {}, ["output1"], {}, {"output1": {"type": "File[]", "curate:axis": "rank3"}})
    wf, errors, warnings = run(tmp_path, doc)
    assert errors == [] and any("extra: output output1 writes one file per rank3" in w for w in warnings)


def test_a_folder_from_several_sources_is_allowed_but_not_scattered(tmp_path):
    doc = base()
    doc["requirements"]["MultipleInputFeatureRequirement"] = {}
    doc["steps"]["use"]["in"]["input1"] = {"source": ["make/output1", "make/output1"], "linkMerge": "merge_flattened",
                                           "curate:folder": True}
    doc["steps"]["again"] = step("again", {"input1": {"source": ["make/output1", "make/output1"],
                                                      "linkMerge": "merge_flattened", "curate:folder": True}},
                                 ["output1"], {"input1": "File"}, {"output1": "File"})
    doc["steps"]["again"]["scatter"] = "input1"
    errors = errors_of(tmp_path, doc)
    assert_error(errors, "step again: curate:folder input input1 cannot be scattered over")
    assert not any("step use" in e for e in errors)


def test_raw_errors_stop_before_cwl_utils(tmp_path):
    wf, errors, _ = run(tmp_path, mutate(lambda d: d["steps"]["use"].update(run="sub.cwl")))
    assert wf is None and len(errors) == 1
