"""Phase 5: the FaaSr and Pegasus builders read workflow.yml, and their bundles match today's
(emit.write_bundle; pegasus_deploy's prepare = pegasus_ir.build_ir + write_folder) for the e2
MapReduce and weather specs, with file names replaced by their roles.

FaaSr: workflow.json (every field; Arguments by role), each wrapper's tables (slot kinds, list
sizes, ranked, params), the shipped functions and deps files, the external inputs; and, for e2,
both bundles run through the FaaSr stand-in from test_faasr_wrapper and every object they write
is byte-identical. Pegasus: the expanded jobs (test_cwl_parity's comparison), and the e2 run
through ir_local gives byte-identical files."""
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
import emit  # noqa: E402
import pegasus_ir as P  # noqa: E402
import validate as V0  # noqa: E402
from curate_models import WorkflowSpec  # noqa: E402
from cwl.from_ir import convert, write  # noqa: E402
from cwl.validate import validate  # noqa: E402
from ir_local import run_locally  # noqa: E402
from test_cwl_from_ir import FIXTURES, spec_ir  # noqa: E402
from test_cwl_parity import canon  # noqa: E402
from test_faasr_wrapper import run_bundle  # noqa: E402

IMAGE = "docker:///python:3.12-slim"


def e2_spec():
    return json.loads((FIXTURES / "e2-stages" / "spec-with-code.json").read_text())


def weather_spec():
    return json.loads((FIXTURES / "weather-spec.json").read_text())


SPECS = [(e2_spec, "e2"), (weather_spec, "weather")]


def cwl_of(spec_json, tmp_path):
    folder = write(convert(V0.load(spec_ir(spec_json, tmp_path))), tmp_path / "cwl")
    wf, errors, _ = validate(folder / "workflow.yml", folder / "inputs.yml")
    assert errors == []
    return wf


def bundles(spec_json, tmp_path):
    spec = WorkflowSpec.model_validate(spec_json)
    res = emit.write_bundle(spec, tmp_path / "old", level="structural")
    assert res["ok"], res
    built = CF.build(cwl_of(spec_json, tmp_path), spec.name, spec.folder, spec.compute_target, spec.data_store)
    return spec, Path(res["bundle_dir"]), built, CF.write(built, tmp_path / "new")


def old_roles(spec):
    """Spec file name -> role: (step, output slot) for a step's output; ("ext", consumer, slot) else."""
    roles = {}
    for n in spec.nodes:
        for k, o in enumerate(n.outputs, 1):
            roles[o.name] = (n.name, f"output{k}")
    for n in spec.nodes:
        for k, i in enumerate(n.inputs, 1):
            roles.setdefault(i.name, ("ext", n.name, f"input{k}"))
    return roles


def new_roles(wf, built):
    roles = {}
    for src, pattern in built["names"].items():
        if "/" in src:
            roles[pattern] = tuple(src.split("/", 1))
    for sid, s in wf.steps.items():
        for slot, e in s.ins.items():
            for src in e.sources:
                if "/" not in src and src in built["names"]:
                    roles.setdefault(built["names"][src], ("ext", sid, slot))
    return roles


def wrapper_tables(path: Path) -> dict:
    tree = ast.parse(path.read_text())
    return {t.targets[0].id: ast.literal_eval(t.value) for t in tree.body
            if isinstance(t, ast.Assign) and isinstance(t.targets[0], ast.Name) and t.targets[0].id.startswith("_")
            and t.targets[0].id in ("_STEP", "_MAIN_FILE", "_MAIN_SHA256", "_RANKED", "_INPUTS", "_OUTPUTS", "_PARAMS")}


@pytest.mark.parametrize("spec_of,label", SPECS, ids=[s[1] for s in SPECS])
def test_faasr_bundle_matches_todays(tmp_path, spec_of, label):
    spec, old, built, new = bundles(spec_of(), tmp_path)
    wf = cwl_of(spec_of(), tmp_path / "again")
    a = json.loads((old / "workflow.json").read_text())
    b = json.loads((new / "workflow.json").read_text())
    ra, rb = old_roles(spec), new_roles(wf, built)
    for key in set(a) | set(b):
        if key != "ActionList":
            assert a.get(key) == b.get(key), key
    assert list(a["ActionList"]) == list(b["ActionList"])
    for action, x in a["ActionList"].items():
        y = b["ActionList"][action]
        assert {k: v for k, v in x.items() if k != "Arguments"} == {k: v for k, v in y.items() if k != "Arguments"}
        assert list(x["Arguments"]) == list(y["Arguments"]) and x["Arguments"]["folder"] == y["Arguments"]["folder"]
        for slot in x["Arguments"]:
            if slot != "folder":
                assert ra[x["Arguments"][slot]] == rb[y["Arguments"][slot]], (action, slot)
    for n in spec.nodes:
        assert (new / "functions" / f"{n.name}_main.py").read_bytes() == n.code.encode()
        assert (new / "functions" / f"{n.name}.deps.txt").read_text() == \
            (old / "functions" / f"{n.name}.deps.txt").read_text()
        assert wrapper_tables(new / "functions" / f"{n.name}.py") == wrapper_tables(old / "functions" / f"{n.name}.py")
    olde = json.loads((old / "external_inputs.json").read_text())
    assert sorted(ra[e["file"]] for e in olde) == sorted(rb[e["file"]] for e in built["external_inputs"])


def test_e2_faasr_bundles_write_the_same_objects(tmp_path):
    spec, old, built, new = bundles(e2_spec(), tmp_path)
    import random
    random.seed(20261123)  # generate_text shuffles with the global random: the same seed, the same text
    run_bundle(tmp_path / "a", old, tmp_path / "s3a")
    random.seed(20261123)
    run_bundle(tmp_path / "b", new, tmp_path / "s3b")
    ra, rb = old_roles(spec), new_roles(cwl_of(e2_spec(), tmp_path / "again"), built)
    fa = {p.name: p for p in (tmp_path / "s3a" / spec.folder).iterdir()}
    fb = {p.name: p for p in (tmp_path / "s3b" / spec.folder).iterdir()}
    assert len(fa) == len(fb) == 17

    def role_of(name, roles):
        import re
        for pattern, r in roles.items():
            if "{rank}" in pattern:  # every {rank} in the pattern is the same rank
                rx = "^" + re.escape(pattern).replace(re.escape("{rank}"), r"(\d+)") + "$"
                m = re.match(rx, name)
                if m and len(set(m.groups())) == 1:
                    return r, int(m.group(1))
            elif name == pattern:
                return r, None
        raise KeyError(name)

    by_a = {role_of(n, ra): p.read_bytes() for n, p in fa.items()}
    by_b = {role_of(n, rb): p.read_bytes() for n, p in fb.items()}
    assert by_a == by_b
    # the deliverables manifest names what download-outputs fetches
    assert [d["deliver_as"] for d in built["outputs"]["deliverables"]] == \
        [f"results/word_count_{i}.json" for i in range(1, 6)]
    assert {f["file"] for f in built["outputs"]["files"]} == set(fb)


@pytest.mark.parametrize("spec_of,label", SPECS, ids=[s[1] for s in SPECS])
def test_pegasus_folder_matches_todays(tmp_path, spec_of, label):
    spec = WorkflowSpec.model_validate(spec_of())
    old = P.write_folder(tmp_path / "old", P.build_ir(spec, name="t-flow", image=IMAGE))
    built = CP.build(cwl_of(spec_of(), tmp_path), "t-flow", image=IMAGE)
    new = CP.write_folder(tmp_path / "new", built)
    assert CP.validate_folder(new)["errors"] == []
    a, _, _, _ = V0.prepare(V0.load(old / "ir.yaml"))
    b, _, _, _ = V0.prepare(V0.load(new / "ir.yaml"))
    assert canon(a) == canon(b)
    assert sorted(p.name for p in (new / "functions").iterdir()) == sorted(p.name for p in (old / "functions").iterdir())
    for p in (old / "functions").iterdir():
        assert (new / "functions" / p.name).read_bytes() == p.read_bytes()


def test_e2_pegasus_runs_give_the_same_files(tmp_path):
    spec = WorkflowSpec.model_validate(e2_spec())
    old = P.write_folder(tmp_path / "old", P.build_ir(spec, name="t-flow", image=IMAGE))
    new = CP.write_folder(tmp_path / "new", CP.build(cwl_of(e2_spec(), tmp_path), "t-flow", image=IMAGE))
    fa = run_locally(old / "ir.yaml", tmp_path / "ja")
    fb = run_locally(new / "ir.yaml", tmp_path / "jb")
    for i in range(1, 6):
        assert fa[f"word_count_{i}.json"].read_bytes() == fb[f"reduce__output1__{i}__word_count_{i}.json"].read_bytes()


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
    assert M.run_outputs(tmp_path, {"bundle_dir": str(tmp_path / "b")}, wf) == \
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
    folder = CP.write_folder(tmp_path / "pegasus", CP.build(cwl_of(e2_spec(), tmp_path), "t-flow", image=IMAGE))
    wf = emitted(folder, tmp_path / "work")
    assert len(wf["jobs"]) == 11
    reduce3 = next(j for j in wf["jobs"] if j["id"] == "reduce__3")
    assert any(u["lfn"].startswith("shuffle__output1__3__") and u["lfn"].endswith(".json")
               for u in reduce3["uses"] if u["type"] == "input")
