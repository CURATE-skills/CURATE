"""faasr_wrapper.py and the FaaSr bundle of plain steps (spec -> spec_cwl -> cwl_faasr), without FaaSr.

A small stand-in for the FaaSr runtime runs a bundle: for each action (in InvokeNext order) and
each rank, it puts the action's FunctionGitRepo files in one invocation folder, imports every
.py there and picks the callable named FunctionName (as FaaSr_py's faasr_import_function_walk
does), injects the FaaSr API into its globals, and calls it with **Arguments. The API serves a
temp folder as the S3 bucket.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import cwl_faasr as CF
import emit
import faasr_wrapper as W
import plain_contract as PC
import spec_cwl
from curate_models import IOSpec, WorkflowSpec

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "curate_ir"))
from cwl.validate import validate as cwl_validate  # noqa: E402

PLAIN = Path(__file__).resolve().parent / "fixtures" / "e2-stages"
WORDS = {w: 1000 for w in ("cat", "dog", "bird", "horse", "pig")}


def plain_e2() -> WorkflowSpec:
    return WorkflowSpec.model_validate(json.loads((PLAIN / "spec-with-code.json").read_text()))


def built(tmp_path, spec) -> dict:
    """The FaaSr build of a spec, as a deploy gets it (spec -> CWL -> cwl_faasr). Raises
    spec_cwl.BuildError or cwl_faasr.BuildError."""
    folder = spec_cwl.write(spec_cwl.build(spec), tmp_path / "cwl")
    wf, errors, _ = cwl_validate(folder / "workflow.yml", folder / "inputs.yml")
    assert errors == [], errors
    return CF.build(wf, spec.name, spec.folder, spec.compute_target, spec.data_store, "structural")


def bundle(tmp_path, spec) -> Path:
    b = built(tmp_path, spec)
    assert b["validation"]["ok"], b["validation"]
    return CF.write(b, tmp_path / "bundle")


def objects(s3_folder: Path) -> dict:
    """The bucket's objects by the spec file name at the end of their generated name."""
    return {p.name.split("__")[-1]: p for p in s3_folder.iterdir() if p.is_file()}


def wrapper(tmp_path, name, code, inputs=(), outputs=(), params=(), ranked=False) -> Path:
    """One step's FaaSr files (wrapper + unchanged function), rendered as cwl_faasr renders them, in an
    invocation folder."""
    inv = tmp_path / "inv"
    inv.mkdir(parents=True)
    (inv / W.main_file(name)).write_text(code)
    (inv / f"{name}.py").write_text(W.render_step(name, code, ranked, list(inputs), list(outputs), list(params)))
    return inv


class FakeFaaSr:
    """The FaaSr API over a folder standing in for the bucket."""

    def __init__(self, s3: Path):
        self.s3, self.log, self.rank = s3, [], None

    def api(self):
        def faasr_get_file(local_file, remote_file, server_name="", local_folder=".", remote_folder="."):
            src = self.s3 / remote_folder / remote_file
            if src.exists():
                shutil.copyfile(src, os.path.join(local_folder, local_file))

        def faasr_put_file(local_file, remote_file, server_name="", local_folder=".", remote_folder="."):
            dst = self.s3 / remote_folder / remote_file
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(os.path.join(local_folder, local_file), dst)

        def faasr_get_folder_list(server_name="", prefix=""):
            keys = sorted(str(p.relative_to(self.s3)) for p in self.s3.rglob("*") if p.is_file())
            return [k for k in keys if k.startswith(prefix)]

        def faasr_rank():  # FaaSr hands back strings; the wrapper makes them ints
            return {"rank": str(self.rank[0]), "max_rank": str(self.rank[1])}

        def faasr_log(log_message):
            if not log_message:  # FaaSr_py's client exits on an empty message
                raise SystemExit("faasr_log called with empty log_message")
            self.log.append(log_message)

        return {f.__name__: f for f in (faasr_get_file, faasr_put_file, faasr_get_folder_list, faasr_rank,
                                        faasr_log)}


def find_function(folder: Path, func_name: str):
    """faasr_import_function_walk: import every .py in the folder, return the first callable
    with the function's name."""
    sys.path.insert(0, str(folder))
    try:
        for f in sorted(folder.glob("*.py")):
            spec = importlib.util.spec_from_file_location(f"{f.stem}_{uuid.uuid4().hex}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            obj = mod.__dict__.get(func_name)
            if callable(obj):
                return obj
    finally:
        sys.path.remove(str(folder))
    return None


def run_bundle(tmp_path, b: Path, s3: Path):
    """Run every action of the bundle's workflow.json in order; returns the FakeFaaSr."""
    wf = json.loads((b / "workflow.json").read_text())
    fake = FakeFaaSr(s3)
    actions, order, todo = wf["ActionList"], [], [wf["FunctionInvoke"]]
    while todo:  # breadth-first over InvokeNext; every action here runs once
        a = todo.pop(0)
        if a not in order:
            order.append(a)
            todo += [t.split("(")[0] for t in actions[a]["InvokeNext"] if isinstance(t, str)]
    for a in order:
        act = actions[a]
        fn = act["FunctionName"]
        n = int(next((t.split("(")[1].rstrip(")") for x in actions.values() for t in x["InvokeNext"]
                      if isinstance(t, str) and t.split("(")[0] == a and "(" in t), "1"))
        for r in range(1, n + 1):
            inv = tmp_path / "functions" / f"{a}-{r}"
            inv.mkdir(parents=True)
            entry = wf["FunctionGitRepo"][fn]
            for path in [entry] if isinstance(entry, str) else entry:
                shutil.copy(b / "functions" / path.rsplit("/", 1)[1], inv)
            func = find_function(inv, fn)
            assert func is not None, f"no callable {fn} in {sorted(os.listdir(inv))}"
            func.__globals__.update(fake.api())
            fake.rank = (r, n)
            func(**act["Arguments"])
    return fake


# ── the bundle ──

def test_plain_steps_ship_unchanged_next_to_their_wrapper(tmp_path):
    spec = plain_e2()
    b = bundle(tmp_path, spec)
    for n in spec.nodes:
        shipped = b / "functions" / f"{n.name}_main.py"
        assert shipped.read_bytes() == n.code.encode(), n.name  # byte-identical, no header
        wrapper = (b / "functions" / f"{n.name}.py").read_text()
        assert f"_MAIN_SHA256 = {W.sha256(n.code)!r}" in wrapper
        assert "def main(" not in wrapper and f"def {n.name}(folder" in wrapper  # only the wrapper
    wf = json.loads((b / "workflow.json").read_text())
    assert wf["FunctionGitRepo"]["map"] == ["<FAASR_GH_USERNAME>/<FAASR_ACTION_REPO>/main/functions/map.py",
                                            "<FAASR_GH_USERNAME>/<FAASR_ACTION_REPO>/main/functions/map_main.py"]
    args = wf["ActionList"]["map"]["Arguments"]
    assert list(args) == ["folder", "input1", "output1"] and args["folder"] == spec.folder
    assert args["input1"].endswith("__{rank}__text_batch_{rank}.txt")
    assert emit.structural_validate(wf) == (True, None)


def test_a_faasr_style_step_is_refused(tmp_path):
    spec = plain_e2()
    spec.nodes[1].code = "def split(folder, input1, output1):\n    faasr_log('old')\n"
    with pytest.raises(spec_cwl.BuildError) as e:
        built(tmp_path, spec)
    assert e.value.problems == [
        "step split is not a plain main(inputs, outputs, params) function (the older FaaSr style): "
        "regenerate it, or migrate the catalog it came from (scripts/migrate_catalog.py)"]


def test_faasr_py_accepts_the_two_file_function_git_repo(tmp_path):
    wf = built(tmp_path, plain_e2())["workflow"]
    res = emit.faasr_py_validate(wf)
    if res is None:
        pytest.skip("FaaSr_py validator could not run here")
    assert res == (True, None)


# ── running it as FaaSr would ──

def test_e2_runs_through_the_wrappers(tmp_path):
    b = bundle(tmp_path, plain_e2())
    s3 = tmp_path / "s3"
    fake = run_bundle(tmp_path, b, s3)
    got = objects(s3 / "mapreduce_word_count")
    totals = {}
    for i in range(1, 6):
        part = json.loads(got[f"word_count_{i}.json"].read_text())
        assert not set(part) & set(totals)
        totals.update(part)
    assert totals == WORDS
    assert sorted(n for n in got if n.startswith("map_counts_")) == [f"map_counts_{i}.json" for i in (1, 2, 3)]
    assert any("map: rank 2/3 reading" in m for m in fake.log)  # print() reached faasr_log
    assert any(m.startswith("map: main(inputs, outputs, params) from map_main.py") and "rank 2/3" in m
               for m in fake.log)


def small(nodes, edges=None) -> WorkflowSpec:
    if edges is None:
        edges = [{"from_node": a["name"], "to_node": b["name"]} for a, b in zip(nodes, nodes[1:])]
    return WorkflowSpec.model_validate({"name": "t", "folder": "d", "nodes": nodes, "edges": edges})


def test_folder_input_downloads_only_its_prefix(tmp_path):
    code = ("import os\n\ndef main(inputs, outputs, params):\n"
            "    names = sorted(os.listdir(inputs['input1']))\n"
            "    open(outputs['output1'], 'w').write(','.join(names))\n")
    fn = find_function(wrapper(tmp_path, "use", code, [("input1", "folder", None)], [("output1", "one", None)]), "use")
    s3 = tmp_path / "s3" / "d"
    s3.mkdir(parents=True)
    for n in ("part_1.csv", "part_2.csv", "other.csv"):
        (s3 / n).write_text(n)
    fn.__globals__.update(FakeFaaSr(tmp_path / "s3").api())
    fn(folder="d", input1="part_{rank}.csv", output1="o.txt")
    assert (s3 / "o.txt").read_text() == "part_1.csv,part_2.csv"


def test_conditional_return_passes_through_and_secret_from_env(tmp_path, monkeypatch):
    code = ("import os\n\ndef main(inputs, outputs, params):\n"
            "    print()\n    print('   ')\n    print('token', os.environ['API_KEY'][:2])\n"
            "    return False\n")
    monkeypatch.setenv("API_KEY", "xy-secret")
    fn = find_function(wrapper(tmp_path, "check", code), "check")
    fake = FakeFaaSr(tmp_path / "s3")
    fn.__globals__.update(fake.api())
    assert fn(folder="d") is False
    assert "token xy" in fake.log  # blank lines were not sent (FaaSr refuses empty messages)


def test_missing_output_and_exception_are_reported(tmp_path):
    fn = find_function(wrapper(tmp_path, "lazy", "def main(inputs, outputs, params):\n    pass\n",
                               outputs=[("output1", "one", None)]), "lazy")
    fake = FakeFaaSr(tmp_path / "s3")
    fn.__globals__.update(fake.api())
    with pytest.raises(RuntimeError, match=r"lazy: main did not write its declared output\(s\) \['o.txt'\]"):
        fn(folder="d", output1="o.txt")

    fn = find_function(wrapper(tmp_path / "b", "boom", "def main(inputs, outputs, params):\n    raise ValueError('bad')\n"),
                       "boom")
    fn.__globals__.update(fake.api())
    with pytest.raises(ValueError, match="bad"):
        fn(folder="d")
    assert any(m.startswith("boom: main raised:") and "ValueError: bad" in m for m in fake.log)


def test_list_output_and_list_input(tmp_path):
    w = "def main(inputs, outputs, params):\n    for i, p in enumerate(outputs['output1'], 1):\n" \
        "        open(p, 'w').write(str(i))\n"
    r = "def main(inputs, outputs, params):\n    open(outputs['output1'], 'w').write(" \
        "open(inputs['input1']).read() + ':' + str(params))\n"
    g = "def main(inputs, outputs, params):\n    open(outputs['output1'], 'w').write(" \
        "'+'.join(open(p).read() for p in inputs['input1']))\n"
    spec = small([{"name": "w", "outputs": [{"name": "p_{rank}"}], "code": w},
                  {"name": "r", "rank": 2, "inputs": [{"name": "p_{rank}"}], "outputs": [{"name": "q_{rank}"}],
                   "code": r},
                  {"name": "g", "inputs": [{"name": "q_{rank}"}], "outputs": [{"name": "s"}], "code": g}])
    run_bundle(tmp_path, bundle(tmp_path, spec), tmp_path / "s3")
    assert objects(tmp_path / "s3" / "d")["s"].read_text() == \
        "1:{'rank': 1, 'max_rank': 2}+2:{'rank': 2, 'max_rank': 2}"


# ── what FaaSr's loader picks, and the guards ──

def test_guards(tmp_path):
    clash = small([{"name": "x", "code": "def main(inputs, outputs, params):\n    pass\n"},
                   {"name": "x_main", "code": "def main(inputs, outputs, params):\n    pass\n"}])
    with pytest.raises(CF.BuildError) as e:
        built(tmp_path / "a", clash)
    assert "step x_main: its name is step x's function file (x_main.py); rename one of them" in e.value.problems
    shadow = small([{"name": "plot", "code": "def plot():\n    pass\n\ndef main(inputs, outputs, params):\n    plot()\n"}])
    (msg,) = W.code_problems("plot", shadow.nodes[0].code, {"plot"})
    assert msg.startswith("step plot: its function file defines a top-level 'plot'")
    with pytest.raises(CF.BuildError) as e:
        built(tmp_path / "b", shadow)
    assert msg in e.value.problems


def test_real_faasr_py_loader_finds_the_wrapper(tmp_path):
    """FaaSr_py 0.1.13's own faasr_import_function_walk, in its uv env, on a two-file folder."""
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv not on PATH")
    inv = wrapper(tmp_path, "step_a", "def main(inputs, outputs, params):\n    pass\n")
    probe = ("import sys\nfrom FaaSr_py.helpers.py_func_helper import faasr_import_function_walk\n"
             "f = faasr_import_function_walk('step_a', directory=sys.argv[1])\n"
             "print(f.__module__, f.__code__.co_filename.rsplit('/', 1)[-1])\n")
    p = subprocess.run([uv, "run", "--quiet", "--no-project", "--python", "3.13", "--with", "FaaSr_py==0.1.13",
                        "python", "-c", probe, str(inv)], capture_output=True, text=True, timeout=300)
    if p.returncode != 0 and "No module named" in p.stderr + p.stdout:
        pytest.skip("FaaSr_py not installable here")
    assert p.returncode == 0, p.stderr[-2000:]
    assert p.stdout.split()[-1] == "step_a.py"


def test_bundle_refuses_a_file_that_would_land_in_a_folder_input(tmp_path):
    code = "def main(inputs, outputs, params):\n    pass\n"
    spec = small([{"name": "count", "rank": 2, "code": code,
                   "outputs": [{"name": "count_{rank}.json"}, {"name": "count_summary.json"}]},
                  {"name": "sum", "code": code, "inputs": [{"name": "count_{rank}.json", "kind": "folder"}],
                   "outputs": [{"name": "total.json"}]}])
    with pytest.raises(spec_cwl.BuildError) as e:
        built(tmp_path, spec)
    assert any("count_summary.json (written by count) would land in it too" in p for p in e.value.problems)


def test_empty_folder_input_raises_before_main(tmp_path):
    code = ("def main(inputs, outputs, params):\n"
            "    raise AssertionError('main must not run on an empty fan-in')\n")
    fn = find_function(wrapper(tmp_path, "use", code, [("input1", "folder", None)], [("output1", "one", None)]), "use")
    fake = FakeFaaSr(tmp_path / "s3")
    (tmp_path / "s3" / "d").mkdir(parents=True)
    (tmp_path / "s3" / "d" / "other.csv").write_text("x")  # not under the prefix
    fn.__globals__.update(fake.api())
    with pytest.raises(FileNotFoundError, match=r"use: folder input input1 \(part_\{rank\}.csv\) is empty: "
                                                r"no file starting with 'part_' in d"):
        fn(folder="d", input1="part_{rank}.csv", output1="o.txt")
