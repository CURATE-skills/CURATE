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
    nodes = {n["id"]: n for n in built["jobs"]}
    assert nodes["cleanup"]["after"] == ["report"]
    assert [nodes[f"map__{i}"]["secrets"] for i in (1, 2, 3)] == [["API_TOKEN"]] * 3
    assert "API_TOKEN=" not in yaml.safe_dump(built["workflow"])
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


# ── the Pegasus folder, and the backend writing this host's catalogs for it ──
# (Pegasus.api from pegasus-wms, a test dependency)

def backend():
    sys.path.insert(0, str(PLUGIN / "curate_ir" / "backends"))
    import pegasus_backend as B
    return B


def test_the_native_workflow_has_after_as_dependencies_and_secrets_as_getenv(tmp_path):
    def no_run_if(doc):
        del doc["steps"]["report"]["hints"]["curate:run_if"]
    folder = CP.write_folder(tmp_path / "bundle", CP.build(features_wf(tmp_path, no_run_if), "f"))
    wf = yaml.safe_load((folder / "workflow.yml").read_text())
    assert wf["pegasus"] == "5.0.4" and "x-pegasus" not in wf  # who wrote it and when: never in the file
    jobs = {j["id"]: j for j in wf["jobs"]}
    parents = {d["id"]: set(d["children"]) for d in wf.get("jobDependencies", [])}
    assert "cleanup" in parents["report"]  # curate:after: no file passes between them
    for i in (1, 2, 3):
        assert jobs[f"map__{i}"]["profiles"]["condor"]["getenv"] == "API_TOKEN"
    assert "profiles" not in jobs["cleanup"] or "getenv" not in jobs["cleanup"]["profiles"].get("condor", {})
    assert "API_TOKEN=" not in (folder / "workflow.yml").read_text()
    assert str(tmp_path) not in (folder / "workflow.yml").read_text()  # nothing about this machine


def test_the_same_workflow_gives_the_same_bytes(tmp_path):
    """Also across processes: Pegasus.api keeps uses and children in sets, whose order follows the hash
    seed; the builder fixes the order."""
    import os
    import subprocess
    code = ("import sys; sys.path[:0] = [sys.argv[1] + '/scripts', sys.argv[1] + '/curate_ir', sys.argv[1] + '/tests']\n"
            "from test_cwl_builders import e2_wf, IMAGE\nimport cwl_pegasus as CP\n"
            "sys.stdout.write(CP.dump(CP.build(e2_wf(), 't-flow', image=IMAGE)['workflow']))\n")
    outs = {subprocess.run([sys.executable, "-c", code, str(PLUGIN)], capture_output=True, text=True, check=True,
                           env={**os.environ, "PYTHONHASHSEED": seed}).stdout for seed in ("1", "2", "3")}
    assert len(outs) == 1 and outs.pop().startswith("pegasus: 5.0.4")


def test_backend_writes_only_this_hosts_catalogs(tmp_path):
    folder = CP.write_folder(tmp_path / "pegasus", CP.build(e2_wf(), "t-flow", image=IMAGE))
    before = (folder / "workflow.yml").read_bytes()
    B = backend()
    work = B.emit(B.load(folder / "workflow.yml"), tmp_path / "work", "docker")
    assert sorted(p.name for p in work.iterdir()) == ["pegasus.properties", "replicas.yml", "sites.yml",
                                                       "transformations.yml"]
    assert (folder / "workflow.yml").read_bytes() == before  # the workflow is planned as built
    tc = yaml.safe_load((work / "transformations.yml").read_text())
    assert [t["name"] for t in tc["transformations"]] == ["generate_text", "split", "map", "shuffle", "reduce"]
    assert {s["pfn"] for t in tc["transformations"] for s in t["sites"]} == {"file://" + str(B.SHIM)}
    assert [c["image"] for c in tc["containers"]] == [IMAGE]
    rc = yaml.safe_load((work / "replicas.yml").read_text())
    assert {r["pfns"][0]["pfn"] for r in rc["replicas"]} == \
        {"file://" + str((folder / "functions" / f"{s}.py").resolve()) for s in ("generate_text", "split", "map", "shuffle", "reduce")}
    props = (work / "pegasus.properties").read_text()
    for k in ("site", "replica", "transformation"):
        assert f"pegasus.catalog.{k}.file = {work}/" in props
    wf = yaml.safe_load(before)
    reduce3 = next(j for j in wf["jobs"] if j["id"] == "reduce__3")
    assert any(u["lfn"].startswith("shuffle__output1__3__") and u["lfn"].endswith(".json")
               for u in reduce3["uses"] if u["type"] == "input")


def test_backend_status_and_collect_read_the_native_workflow(tmp_path):
    """status names each job <transformation>_<id> in jobstate.log; collect copies the deliverables the
    workflow's metadata lists. Both read workflow.yml with plain YAML (no Pegasus API)."""
    folder = CP.write_folder(tmp_path / "pegasus", CP.build(e2_wf(), "t-flow", image=IMAGE))
    B = backend()
    wf = B.load(folder / "workflow.yml")
    work, run_dir = tmp_path / "work", tmp_path / "run"
    (work / "output").mkdir(parents=True)
    run_dir.mkdir()
    (work / B.SUBMIT_RECORD).write_text(json.dumps({"workflow": "t-flow", "run_dir": str(run_dir),
                                                    "submitted_at": "x", "runtime": "docker"}))
    (run_dir / "jobstate.log").write_text("1 reduce_reduce__3 POST_SCRIPT_SUCCESS\n1 map_map__1 JOB_FAILURE\n")
    (run_dir / "t.dag.dagman.out").write_text("EXITING WITH STATUS 1\n")
    st = B.status(wf, work)
    assert st["state"] == "failure" and st["jobs"]["reduce__3"] == "done" and st["jobs"]["map__1"] == "failed"
    assert st["jobs"]["split"] == "pending" and len(st["jobs"]) == 11
    names = [d["file"] for d in wf["_meta"]["deliverables"]]
    (work / "output" / names[0]).write_text("{}")
    report = B.collect(wf, work)
    assert report["delivered"] == ["results/word_count_1.json"] and len(report["missing"]) == 4
