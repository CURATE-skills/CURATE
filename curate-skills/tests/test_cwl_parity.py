"""Phase 4 parity: for every workflow phase 3 converts, expand_cwl(from_ir(ir)) equals expand(ir)
apart from the names of the files. Every file is replaced by its role (a workflow input by its
location; a step output by the copy that writes it, the output and its index in a list), so the
comparison checks structure, order, wiring, params (types included), images and deliverables.
Marker files (IR v0.2) and `after` (CWL) are both compared as the set of copies a job waits for."""
import json
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "curate_ir"))
sys.path.insert(0, str(PLUGIN / "tests"))
import validate as V0  # noqa: E402
from cwl.expand_cwl import expand_cwl  # noqa: E402
from cwl.from_ir import convert, write  # noqa: E402
from cwl.validate import validate  # noqa: E402
from test_cwl_from_ir import EXAMPLES, FIXTURES, ir_file, spec_ir  # noqa: E402

MARKER = "curate_done"


def canon(exp):
    """The expanded workflow with every file name replaced by its role."""
    role, marker_of = {}, {}
    for inp in exp["inputs"]:
        role[inp["file"]] = ("input", inp["location"])
    key_of = {}
    for n in exp["nodes"]:
        key = (n["_origin"], tuple(sorted((a, str(v)) for a, v in n.get("_assign", {}).items())))
        key_of[n["id"]] = key
        for k, v in n["outputs"].items():
            if k.startswith(MARKER):
                marker_of[v] = key
            elif isinstance(v, list):
                role.update({f: ("out", key, k, i) for i, f in enumerate(v)})
            else:
                role[v] = ("out", key, k, None)

    def c(v):
        if isinstance(v, dict):
            return ("folder", [role[f] for f in v["folder"]])
        if isinstance(v, list):
            return [role[f] for f in v]
        return role[v]

    nodes = []
    for n in exp["nodes"]:
        waits = {key_of[i] for i in n.get("after", [])}
        for k, v in n["inputs"].items():
            if k.startswith(MARKER):
                waits |= {marker_of[f] for f in (v if isinstance(v, list) else [v])}
        nodes.append({
            "key": key_of[n["id"]], "code": n["code"], "entrypoint": n.get("entrypoint", "main"),
            "inputs": {k: c(v) for k, v in n["inputs"].items() if not k.startswith(MARKER)},
            "outputs": {k: c(v) for k, v in n["outputs"].items() if not k.startswith(MARKER)},
            "params": n.get("params", {}), "image": n.get("image"), "resources": n.get("resources"),
            "packages": n.get("packages"), "waits": sorted(waits)})
    return {"name": exp.get("name"), "description": exp.get("description") or "",
            "defaults": {k: exp.get("defaults", {}).get(k) for k in ("image", "resources")},
            "inputs": sorted(i["location"] for i in exp["inputs"]),
            "outputs": [(role[o["file"]], o.get("deliver_as", o["file"])) for o in exp["outputs"]],
            "nodes": nodes}


def both(ir_path, tmp_path):
    ir = V0.load(ir_path)
    v0, errors, _, _ = V0.prepare(ir)
    assert errors == []
    folder = write(convert(V0.load(ir_path)), tmp_path / "cwl")
    wf, errors, _ = validate(folder / "workflow.yml", folder / "inputs.yml")
    assert errors == []
    return canon(v0), canon(expand_cwl(wf)), wf


def assert_parity(a, b, reordered=()):
    """Equal, except that the copies of a step whose scatter order the converter changed (a partial
    gather's producer) may come in another order."""
    for k in ("name", "description", "defaults", "inputs", "outputs"):
        assert a[k] == b[k], k
    assert [n["key"][0] for n in a["nodes"]] == [n["key"][0] for n in b["nodes"]] or reordered
    by_a = {n["key"]: n for n in a["nodes"]}
    by_b = {n["key"]: n for n in b["nodes"]}
    assert set(by_a) == set(by_b)
    for key, na in by_a.items():
        assert na == by_b[key], key
    for origin in {n["key"][0] for n in a["nodes"]} - set(reordered):
        assert [n["key"] for n in a["nodes"] if n["key"][0] == origin] == \
               [n["key"] for n in b["nodes"] if n["key"][0] == origin], origin


def mapreduce_spec():
    return json.loads((FIXTURES / "e2-stages" / "spec-with-code.json").read_text())


def catalog_mapreduce_spec():
    (entry,) = (FIXTURES / "catalog-e2-mapreduce" / "workflows").glob("*.json")
    return json.loads(entry.read_text())["workflow_spec"]


@pytest.mark.parametrize("example", ["diamond", "fanout"])
def test_examples(tmp_path, example):
    a, b, _ = both(EXAMPLES / example / "ir.yaml", tmp_path)
    assert_parity(a, b)


@pytest.mark.parametrize("spec", [mapreduce_spec, catalog_mapreduce_spec,
                                  lambda: json.loads((FIXTURES / "weather-spec.json").read_text())],
                         ids=["e2-stages", "catalog-e2-mapreduce", "weather"])
def test_specs_through_build_ir(tmp_path, spec):
    a, b, _ = both(spec_ir(spec(), tmp_path), tmp_path)
    assert_parity(a, b)


def test_mapreduce_jobs_in_detail(tmp_path):
    """The parity comparison is not vacuous: the e2 workflow's 11 jobs, rank params as ints."""
    a, b, _ = both(spec_ir(mapreduce_spec(), tmp_path), tmp_path)
    assert len(b["nodes"]) == 11
    reduce3 = next(n for n in b["nodes"] if n["key"] == ("reduce", (("rank5", "3"),)))
    assert reduce3["params"] == {"rank": 3, "max_rank": 5}
    assert reduce3["inputs"] == {"input1": ("out", ("shuffle", ()), "output1", 2)}
    shuffle = next(n for n in b["nodes"] if n["key"][0] == "shuffle")
    assert shuffle["inputs"]["input1"][0] == "folder" and len(shuffle["inputs"]["input1"][1]) == 3


def test_file_less_edge(tmp_path):
    plain = "def main(inputs, outputs, params):\n    open(list(outputs.values())[0], 'w').write('x')\n"
    spec = {"name": "t_flow", "folder": "tf",
            "nodes": [{"name": "a", "code": plain, "rank": 2, "inputs": [], "outputs": [{"name": "p_{rank}.txt"}]},
                      {"name": "b", "code": plain, "rank": 2, "inputs": [], "outputs": [{"name": "q_{rank}.txt"}]}],
            "edges": [{"from_node": "a", "to_node": "b"}]}
    a, b, _ = both(spec_ir(spec, tmp_path), tmp_path)
    assert_parity(a, b)
    assert len(b["nodes"][-1]["waits"]) == 2


def constructed(tmp_path, case):
    if case == "list-folder-markers":
        return ir_file(tmp_path, [
            {"id": "split", "outputs": {"b": "batch_{r}.txt"}},
            {"id": "use", "for_each": ["r"], "inputs": {"b": "batch_{r}.txt"},
             "outputs": {"o": "part_{r}.csv", "curate_done": "curate_done__use_{r}"}, "params": {"rank": "{r}"}},
            {"id": "extra", "outputs": {"x": "part_x.csv"}},
            {"id": "all", "inputs": {"parts": {"folder": ["part_{r}.csv", "part_x.csv"]},
                                     "curate_done_after_use": "curate_done__use_{r_all}"},
             "outputs": {"o": "summary.json"}},
        ], axes={"r": ["1", "2"], "r_all": ["1", "2"]}, outputs=[{"file": "summary.json"}])
    if case == "external-family":
        path = ir_file(tmp_path, [
            {"id": "m", "for_each": ["w"], "inputs": {"d": "data_{w}.csv"}, "outputs": {"o": "o_{w}.json"},
             "params": {"n": 3, "tag": "x", "f": 0.5, "on": True, "ks": [1, 2]}},
        ], axes={"w": ["a", "b"]}, outputs=[{"file": "o_{w}.json", "deliver_as": "results/{w}/o.json"}])
        ir = yaml.safe_load(path.read_text())
        ir["inputs"] = [{"file": "data_{w}.csv", "location": "inputs/data_{w}.csv"}]
        path.write_text(yaml.safe_dump(ir))
        return path
    if case == "two-axis-gather":
        return ir_file(tmp_path, [
            {"id": "m", "for_each": ["r", "w"], "outputs": {"o": "o_{r}_{w}"}, "params": {"rank": "{r}", "name": "{w}"}},
            {"id": "g", "inputs": {"all": "o_{r}_{w}"}, "outputs": {"res": "res.json"}},
        ], axes={"r": ["1", "2"], "w": ["a", "b"]},
            outputs=[{"file": "res.json"}, {"file": "o_{r}_{w}", "deliver_as": "all/{w}/{r}.txt"}])
    if case == "axis-types":
        return ir_file(tmp_path, [
            {"id": "a", "for_each": ["r"], "outputs": {"o": "a_{r}"}, "params": {"rank": "{r}"}},
            {"id": "b", "for_each": ["w"], "outputs": {"o": "b_{w}"}},
            {"id": "c", "inputs": {"x": "a_{r}", "y": "b_{w}"}, "outputs": {"o": "c.json"},
             "params": {"literal": "{w}"}},
        ], axes={"r": ["1", "2"], "w": ["2019", "2020"]}, outputs=[{"file": "c.json"}])
    if case == "partial-gather":
        return ir_file(tmp_path, [
            {"id": "detect", "for_each": ["station", "winter"], "outputs": {"ice": "ice_{winter}_{station}.json"}},
            {"id": "assemble", "for_each": ["winter"], "inputs": {"ice": "ice_{winter}_{station}.json"},
             "outputs": {"row": "row_{winter}.json"}},
        ], axes={"winter": ["2019", "2020"], "station": ["a", "b", "c"]},
            outputs=[{"file": "row_{winter}.json", "deliver_as": "results/row_{winter}.json"}])
    raise KeyError(case)


@pytest.mark.parametrize("case", ["list-folder-markers", "external-family", "two-axis-gather", "axis-types"])
def test_constructed(tmp_path, case):
    a, b, _ = both(constructed(tmp_path, case), tmp_path)
    assert_parity(a, b)


def test_axis_types_are_exactly_v0_2s(tmp_path):
    a, b, _ = both(constructed(tmp_path, "axis-types"), tmp_path)
    p = {n["key"]: n["params"] for n in b["nodes"]}
    assert p[("a", (("r", "1"),))] == {"rank": 1}
    assert p[("b", (("w", "2019"),))] == {"w": "2019"}
    assert p[("c", ())] == {"literal": "{w}"}


def test_partial_gather_differs_only_in_the_producers_copy_order(tmp_path):
    a, b, wf = both(constructed(tmp_path, "partial-gather"), tmp_path)
    assert wf.steps["detect"].scatter == ["winter", "station"]
    assert_parity(a, b, reordered=["detect"])
    order = lambda c: [n["key"] for n in c["nodes"] if n["key"][0] == "detect"]  # noqa: E731
    assert order(a) != order(b) and sorted(order(a)) == sorted(order(b))


def test_canon_sees_a_wiring_difference(tmp_path):
    """A swapped gather order must fail the comparison."""
    a, b, _ = both(EXAMPLES / "fanout" / "ir.yaml", tmp_path)
    asm = next(n for n in b["nodes"] if n["key"][0] == "assemble")
    asm["inputs"]["rows"] = list(reversed(asm["inputs"]["rows"]))
    with pytest.raises(AssertionError):
        assert_parity(a, b)


@pytest.mark.parametrize("ir_of", [lambda t: EXAMPLES / "fanout" / "ir.yaml",
                                   lambda t: spec_ir(mapreduce_spec(), t)], ids=["fanout", "mapreduce"])
def test_job_ids_are_v0_2s(tmp_path, ir_of):
    path = ir_of(tmp_path)
    v0, _, _, _ = V0.prepare(V0.load(path))
    folder = write(convert(V0.load(path)), tmp_path / "cwl")
    wf, _, _ = validate(folder / "workflow.yml", folder / "inputs.yml")
    assert [n["id"] for n in expand_cwl(wf)["nodes"]] == [n["id"] for n in v0["nodes"]]
