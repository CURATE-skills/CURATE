"""scripts/spec_cwl.py: the spec -> CWL step emit uses. Its output for the reference specs is fixed byte
for byte in tests/test_cwl_reference.py and tests/test_real_versions.py."""
import json
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "curate_ir"))
sys.path.insert(0, str(PLUGIN / "tests"))
import spec_cwl as SC  # noqa: E402
from curate_models import WorkflowSpec  # noqa: E402
from cwl.validate import validate  # noqa: E402
FIXTURES = Path(__file__).resolve().parent / "fixtures"

PLAIN = "def main(inputs, outputs, params):\n    open(list(outputs.values())[0], 'w').write('x')\n"
CHECK = "def main(inputs, outputs, params):\n    return True\n"


def fixture(name):
    if name == "e2":
        return WorkflowSpec.model_validate(json.loads((FIXTURES / "e2-stages" / "spec-with-code.json").read_text()))
    return WorkflowSpec.model_validate(json.loads((FIXTURES / "weather-spec.json").read_text()))


def valid(tmp_path, built):
    folder = SC.write(built, tmp_path / "cwl")
    wf, errors, warnings = validate(folder / "workflow.yml", folder / "inputs.yml")
    assert errors == [], errors
    return wf


def spec_with_conditions_and_secrets():
    node = lambda name, code, ins=(), outs=(), **kw: {  # noqa: E731
        "name": name, "code": code, "inputs": [{"name": i} for i in ins], "outputs": [{"name": o} for o in outs], **kw}
    return WorkflowSpec.model_validate({
        "name": "cond_flow", "folder": "cf", "description": "a check, a report on True, a cleanup after it",
        "nodes": [node("make", PLAIN, outs=["data.txt"], secrets=["API_TOKEN"]),
                  node("check", CHECK, ins=["data.txt"]),
                  node("report", PLAIN, ins=["data.txt"], outs=["report.txt"]),
                  node("cleanup", PLAIN, outs=["done.txt"])],
        "edges": [{"from_node": "make", "to_node": "check"}, {"from_node": "check", "to_node": "report", "condition": "True"},
                  {"from_node": "make", "to_node": "report"}, {"from_node": "report", "to_node": "cleanup"}]})


def test_conditions_secrets_and_file_less_edges_come_from_the_spec(tmp_path):
    built = SC.build(spec_with_conditions_and_secrets())
    steps = built["workflow"]["steps"]
    assert steps["report"]["hints"]["curate:run_if"] == {"step": "check", "result": True}
    assert "curate:after" not in steps["report"]["hints"]  # run_if implies it
    assert steps["make"]["hints"]["curate:secrets"] == {"names": ["API_TOKEN"]}
    assert steps["cleanup"]["hints"]["curate:after"] == {"steps": ["report"]}
    wf = valid(tmp_path, built)
    assert wf.steps["report"].run_if == {"step": "check", "result": True} and wf.steps["make"].secrets == ["API_TOKEN"]
    assert not any(k.startswith("curate_done") for s in steps.values() for k in s["run"]["outputs"])


def test_a_step_on_two_results_is_refused():
    spec = spec_with_conditions_and_secrets()
    spec.edges.append(type(spec.edges[0]).model_validate({"from_node": "make", "to_node": "report", "condition": "False"}))
    with pytest.raises(SC.BuildError) as e:
        SC.build(spec)
    assert "step report runs on the result of both check and make" in e.value.problems[0]
    assert "deliberate change" in e.value.problems[0] and "never tested" in e.value.problems[0]


def test_content_hash_ignores_labels_identity_and_defaults_only():
    built = SC.build(fixture("e2"))
    h = SC.content_hash(built["workflow"], built["functions"])
    assert h.startswith("sha256:") and len(h) == 71
    doc = json.loads(json.dumps(built["workflow"]))
    doc.update({"label": "x", "doc": "y", "s:identifier": "mapreduce-v9", "s:version": "9",
                "curate:content_hash": "sha256:0"})
    for i in doc["inputs"].values():
        i["default"] = "changed"
    assert SC.content_hash(doc, built["functions"]) == h
    fns = dict(built["functions"])
    fns["functions/map.py"] += "\n# changed\n"
    assert SC.content_hash(built["workflow"], fns) != h
    doc["steps"]["map"]["in"]["max_rank"]["default"] = 4
    assert SC.content_hash(doc, built["functions"]) != h
