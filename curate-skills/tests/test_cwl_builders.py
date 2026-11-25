"""What each builder (scripts/cwl_faasr.py, scripts/cwl_pegasus.py) refuses or maps, on the features
example (curate_ir/cwl/examples/features): curate:run_if, curate:after, curate:secrets, folder inputs,
and the Pegasus backend planning a built folder. The builders' output for every reference workflow is
fixed in tests/test_cwl_reference.py and tests/test_real_versions.py (until phase 8 this file also
compared them with the old spec-to-bundle path, emit.write_bundle and pegasus_ir.build_ir)."""
import ast
import json
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "curate_ir"))
sys.path.insert(0, str(PLUGIN / "tests"))
import cwl_faasr as CF  # noqa: E402
import cwl_pegasus as CP  # noqa: E402
import validate as V0  # noqa: E402  (reads the Pegasus folder's expanded ir.yaml until phase 8, commit 4)
from cwl.validate import validate  # noqa: E402

IMAGE = "docker:///python:3.12-slim"
REF = PLUGIN / "tests" / "fixtures" / "cwl-ref"


def e2_wf():
    wf, errors, _ = validate(REF / "e2-stages" / "workflow.yml", REF / "e2-stages" / "inputs.yml")
    assert errors == []
    return wf


# ── what each builder refuses or maps ────────────────────────────────────────

def features_wf(tmp_path, edit=None):
    src = PLUGIN / "curate_ir" / "cwl" / "examples" / "features"
    doc = yaml.safe_load((src / "workflow.yml").read_text())
    if edit:
        edit(doc)
    import shutil
    shutil.copytree(src / "functions", tmp_path / "functions")
    (tmp_path / "workflow.yml").write_text(yaml.safe_dump(doc, sort_keys=False))
    wf, errors, _ = validate(tmp_path / "workflow.yml")
    assert errors == [], errors
    return wf


def test_pegasus_refuses_run_if(tmp_path):
    with pytest.raises(CP.BuildError) as e:
        CP.build(features_wf(tmp_path), "f")
    assert any("step report runs only if check returns True (curate:run_if)" in p and "Deploy to FaaSr" in p
               for p in e.value.problems)


def test_pegasus_after_and_secrets_by_name(tmp_path):
    def no_run_if(doc):
        del doc["steps"]["report"]["hints"]["curate:run_if"]
    built = CP.build(features_wf(tmp_path, no_run_if), "f")
    nodes = {n["id"]: n for n in built["ir"]["nodes"]}
    assert nodes["cleanup"]["after"] == ["report"]
    assert [nodes[f"map__{i}"]["secrets"] for i in (1, 2, 3)] == [["API_TOKEN"]] * 3
    assert "API_TOKEN=" not in yaml.safe_dump(built["ir"])
    folder = CP.write_folder(tmp_path / "out", built)
    v = CP.validate_folder(folder)
    assert v["errors"] == [] and v["jobs"] == 3 + 1 + 5 + 3
    assert built["needed"] == ["batch_1.txt", "batch_2.txt", "batch_3.txt", "notes.txt"]


def test_faasr_maps_run_if_after_and_secrets(tmp_path):
    def single_folder_source(doc):
        doc["steps"]["shuffle"]["in"]["input1"] = {"source": "map/output1", "curate:folder": True}
    built = CF.build(features_wf(tmp_path, single_folder_source), "features", "feat")
    acts = built["workflow"]["ActionList"]
    assert acts["map"]["InvokeNext"] == ["shuffle"]
    assert acts["check"]["InvokeNext"] == [{"True": ["report"]}]
    assert acts["report"]["InvokeNext"] == ["cleanup"]
    # report reads reduce's output too, so reduce also invokes it, as a spec edge reduce -> report would
    assert acts["reduce"]["InvokeNext"] == ["check", "report"]
    assert built["workflow"]["Secrets"] == ["API_TOKEN"]
    assert acts["map"]["Arguments"]["input1"] == "batches__{rank}.txt"
    assert acts["shuffle"]["Arguments"]["output1"] == "shuffle__output1__{rank}"
    tables = wrapper_tables_of(built, "map")
    assert tables["_PARAMS"] == [("rank", None, [1, 2, 3]), ("max_rank", 3, None)]
    assert sorted(e["file"] for e in built["external_inputs"]) == ["batches__1.txt", "batches__2.txt",
                                                                   "batches__3.txt", "notes.txt"]


def wrapper_tables_of(built, step):
    tree = ast.parse(built["files"][f"functions/{step}.py"])
    return {t.targets[0].id: ast.literal_eval(t.value) for t in tree.body if isinstance(t, ast.Assign)
            and isinstance(t.targets[0], ast.Name) and t.targets[0].id in ("_PARAMS", "_INPUTS", "_OUTPUTS")}


def test_faasr_refuses_two_axes_and_a_folder_of_two_sources(tmp_path):
    with pytest.raises(CF.BuildError) as e:
        CF.build(features_wf(tmp_path), "features", "feat")
    assert any("folder input input1 gathers several sources" in p for p in e.value.problems)
    from test_cwl_validate import partial, write as write_cwl
    (tmp_path / "p").mkdir()
    path, _ = write_cwl(tmp_path / "p", partial())
    wf, errors, _ = validate(path)
    assert errors == []
    with pytest.raises(CF.BuildError) as e:
        CF.build(wf, "p", "p")
    assert any("step detect scatters over several axes" in p and "one axis" in p for p in e.value.problems)


def test_faasr_refuses_a_name_inside_a_folder_prefix(tmp_path):
    def clash(doc):
        doc["steps"]["shuffle"]["in"]["input1"] = {"source": "map/output1", "curate:folder": True}
        doc["inputs"]["notes"]["default"]["location"] = "inputs/map__output1__notes.txt"
        doc["steps"]["check"]["in"]["extra"] = "notes"
        doc["steps"]["check"]["run"]["inputs"]["extra"] = "File"
    with pytest.raises(CF.BuildError) as e:
        CF.build(features_wf(tmp_path, clash), "features", "feat")
    assert any("lists every object starting with 'map__output1__'" in p and "map__output1__notes.txt" in p
               for p in e.value.problems)


def test_download_outputs_reads_the_builders_manifest(tmp_path):
    import monitor as M
    wf = {"ActionList": {"reduce": {"Arguments": {"folder": "wc-0101"}}}}
    manifest = {"files": [{"action": "reduce", "file": "reduce__output1__1"}]}
    assert M.built_outputs(wf, manifest) == [{"action": "reduce", "key": "wc-0101/reduce__output1__1"}]
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "outputs.json").write_text(json.dumps(manifest))
    assert M.run_outputs(tmp_path, {"execution": {"bundle_dir": str(tmp_path / "b")}}, wf) == \
        [{"action": "reduce", "key": "wc-0101/reduce__output1__1"}]


# ── the Pegasus backend on a built folder (Pegasus.api from pegasus-wms, a test dependency) ──

def emitted(folder, work):
    sys.path.insert(0, str(PLUGIN / "curate_ir" / "backends"))
    import pegasus_backend as B
    B.emit(V0.load(folder / "ir.yaml"), work, "docker")
    return yaml.safe_load((work / "workflow.yml").read_text())


def test_backend_turns_after_into_dependencies_and_secrets_into_getenv(tmp_path):
    def no_run_if(doc):
        del doc["steps"]["report"]["hints"]["curate:run_if"]
    folder = CP.write_folder(tmp_path / "ir", CP.build(features_wf(tmp_path, no_run_if), "f"))
    wf = emitted(folder, tmp_path / "work")
    jobs = {j["id"]: j for j in wf["jobs"]}
    parents = {d["id"]: set(d["children"]) for d in wf.get("jobDependencies", [])}
    assert "cleanup" in parents["report"]  # curate:after: no file passes between them
    for i in (1, 2, 3):
        assert jobs[f"map__{i}"]["profiles"]["condor"]["getenv"] == "API_TOKEN"
    assert "profiles" not in jobs["cleanup"] or "getenv" not in jobs["cleanup"]["profiles"].get("condor", {})
    assert "API_TOKEN=" not in (tmp_path / "work" / "workflow.yml").read_text()


def test_backend_plans_the_e2_folder(tmp_path):
    folder = CP.write_folder(tmp_path / "pegasus", CP.build(e2_wf(), "t-flow", image=IMAGE))
    wf = emitted(folder, tmp_path / "work")
    assert len(wf["jobs"]) == 11
    reduce3 = next(j for j in wf["jobs"] if j["id"] == "reduce__3")
    assert any(u["lfn"].startswith("shuffle__output1__3__") and u["lfn"].endswith(".json")
               for u in reduce3["uses"] if u["type"] == "input")
