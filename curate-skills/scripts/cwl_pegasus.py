# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "cwl-utils==0.47", "pyyaml==6.0.2", "pegasus-wms.api==5.1.3"]
# ///
"""The Pegasus builder: a CURATE CWL workflow (docs/CWL_IR.md) -> a Pegasus workflow folder.

  cwl_pegasus.py build workflow.yml [--inputs inputs.yml] --out DIR --name NAME [--image URL]

The workflow is expanded here (cwl/expand_cwl.py: one job per copy) and written as Pegasus's own
abstract workflow, `workflow.yml`, with the Pegasus Python API (pegasus-wms.api 5.1.3; the file
declares the 5.0.4 YAML format, which pegasus-plan 5.0.4 and later reads). Next to it: functions/
(the code, unchanged) and inputs/ (the workflow's local input files, copied). That is the folder
pegasus_deploy.py copies to the Pegasus host; curate_ir/backends/pegasus_backend.py plans it there.

Each job runs the runner shim (curate_ir/runner/curate_run.py) through the step's transformation:
`--code <code lfn> --entrypoint main`, then one argument pair per file (`--in`, `--in-list` for a
gather, `--in-dir` for a folder input, `--out`, `--out-list` for a list output), then
`--params-json '<params>'`. Every file a job reads or writes is in its `uses`; the deliverables are
staged out. curate:after becomes a job dependency; curate:secrets an HTCondor `getenv` of those
names (values come from the submit host's environment and never enter a file); curate:run_if is
refused, since Pegasus runs every job of a fixed DAG. `--image` replaces the workflow's default
image for this run (Gate 4's "different image"); a step's own image stays.

Nothing in workflow.yml depends on the host: the catalogs (sites, replicas, transformations), which
need the host's absolute paths, are written there by pegasus_backend.py from the workflow's
metadata, all of it host-independent:
  curate.transformations  {step: {"image", "resources"}}   one transformation per step
  curate.replicas         {lfn: path in this folder}       code files and workflow input files
  curate.deliverables     [{"file", "deliver_as"}]          what collect copies to deliver/
The file has no x-pegasus block (who wrote it, and when), so the same workflow gives the same bytes.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "curate_ir"))

META = "curate."


class BuildError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def files_of(value) -> list[str]:
    """The file names of a job's input or output value: a name, a list (a gather, or an output
    written once per axis value), or a folder input {"folder": [names]}."""
    if isinstance(value, dict):
        return list(value.get("folder") or [])
    return value if isinstance(value, list) else [value]


def runner_args(job: dict) -> list[str]:
    """The runner's file arguments for one job, inputs then outputs (curate_run.py's flags)."""
    out = []
    for name, v in job.get("inputs", {}).items():
        flag = "--in-dir" if isinstance(v, dict) else "--in-list" if isinstance(v, list) else "--in"
        for f in files_of(v):
            out += [flag, f"{name}={f}"]
    for name, v in job.get("outputs", {}).items():
        flag = "--out-list" if isinstance(v, list) else "--out"
        for f in files_of(v):
            out += [flag, f"{name}={f}"]
    return out


def code_lfn(job: dict) -> str:
    return f"code__{job['_origin']}__{Path(job['code']).name}"


def build(wf, name: str, image: str | None = None) -> dict:
    """{"workflow": the Pegasus workflow (dict), "jobs": the expanded jobs (expand_cwl's nodes),
    "deliverables": [{"file", "deliver_as"}], "functions": {code path: text}, "copy": {file: local
    source}, "needed": [files with no local source], "warnings"}. Raises BuildError."""
    from cwl.expand_cwl import expand_cwl

    problems, warnings = [], []
    for sid, s in wf.steps.items():
        if s.run_if:
            problems.append(f"step {sid} runs only if {s.run_if['step']} returns {s.run_if['result']} "
                            "(curate:run_if): Pegasus runs a fixed DAG in which every step runs, so it cannot "
                            "honour the condition. Deploy to FaaSr instead.")
        if s.packages:
            used = image or (s.image and ("docker:///" + s.image if "://" not in s.image else s.image)) \
                or (wf.image and ("docker:///" + wf.image if "://" not in wf.image else wf.image))
            warnings.append(f"step {sid} imports {', '.join(s.packages)}: these must already be in the image "
                            f"{used}; nothing is installed at run time")
    if problems:
        raise BuildError(problems)
    exp = expand_cwl(wf)
    defaults = dict(exp["defaults"])
    if image:
        defaults["image"] = image
    copy, needed, replicas = {}, [], {}
    for inp in exp["inputs"]:
        loc = inp["location"]
        if loc.startswith("file://"):
            src = Path(loc[len("file://"):])
            if src.is_file():
                copy[inp["file"]] = str(src)
            else:
                needed.append(inp["file"])
        replicas[inp["file"]] = "inputs/" + inp["file"]
    functions, transformations = {}, {}
    for n in exp["nodes"]:
        functions.setdefault(n["code"], (wf.base / n["code"]).read_text())
        replicas.setdefault(code_lfn(n), n["code"])
        transformations.setdefault(n["_origin"], {
            "image": n.get("image") or defaults["image"],
            "resources": {**(defaults.get("resources") or {}), **(n.get("resources") or {})}})
    deliverables = [{"file": o["file"], "deliver_as": o.get("deliver_as", o["file"])} for o in exp["outputs"]]
    workflow = native(name, exp["nodes"], {o["file"] for o in deliverables}, transformations, replicas, deliverables)
    return {"workflow": workflow, "jobs": exp["nodes"], "deliverables": deliverables, "functions": functions,
            "copy": copy, "needed": sorted(needed), "warnings": warnings}


def native(name: str, jobs: list[dict], staged: set[str], transformations: dict, replicas: dict,
           deliverables: list[dict]) -> dict:
    """The Pegasus abstract workflow (Pegasus.api), as a dict without its x-pegasus block."""
    import yaml
    from Pegasus.api import File, Job, Namespace, Workflow

    wf = Workflow(name)
    by_id = {}
    for n in jobs:
        lfn = code_lfn(n)
        job = Job(n["_origin"], _id=n["id"])
        job.add_args("--code", lfn, "--entrypoint", n.get("entrypoint", "main"))
        job.add_inputs(File(lfn))
        job.add_args(*runner_args(n))
        for v in n.get("inputs", {}).values():
            for f in files_of(v):
                job.add_inputs(File(f))
        for v in n.get("outputs", {}).values():
            for f in files_of(v):
                job.add_outputs(File(f), stage_out=f in staged)
        job.add_args("--params-json", "'" + json.dumps(n.get("params", {})) + "'")
        if n.get("secrets"):  # by name only: HTCondor copies these variables from the submit environment
            job.add_profiles(Namespace.CONDOR, key="getenv", value=",".join(n["secrets"]))
        wf.add_jobs(job)
        by_id[n["id"]] = job
    for n in jobs:  # control-only edges (curate:after): no file passes between the jobs
        if n.get("after"):
            wf.add_dependency(by_id[n["id"]], parents=[by_id[a] for a in n["after"]])
    wf.add_metadata(**{META + "transformations": json.dumps(transformations),
                       META + "replicas": json.dumps(replicas),
                       META + "deliverables": json.dumps(deliverables)})
    buf = io.StringIO()
    wf.write(buf)
    doc = yaml.safe_load(buf.getvalue())
    doc.pop("x-pegasus", None)  # createdBy / createdOn: who and when, so never the same bytes twice
    # Pegasus.api keeps a job's uses and a job's children in sets, so their order changes with the
    # process's hash seed (Pegasus reads them as sets). Fix it: uses in the order added (code, inputs,
    # outputs), dependencies and children in job order.
    order = {n["id"]: i for i, n in enumerate(jobs)}
    for j, n in zip(doc["jobs"], jobs):
        added = [code_lfn(n)] + [f for v in n.get("inputs", {}).values() for f in files_of(v)] \
            + [f for v in n.get("outputs", {}).values() for f in files_of(v)]
        rank = {f: i for i, f in reversed(list(enumerate(added)))}
        j["uses"].sort(key=lambda u: (rank[u["lfn"]], u["type"]))
    deps = doc.get("jobDependencies") or []
    for d in deps:
        d["children"].sort(key=order.__getitem__)
    deps.sort(key=lambda d: order[d["id"]])
    return doc


def metadata(doc: dict) -> dict:
    """The curate.* metadata of a Pegasus workflow written by build, decoded."""
    md = doc.get("metadata") or {}
    return {k[len(META):]: json.loads(v) for k, v in md.items() if k.startswith(META)}


def dump(doc: dict) -> str:
    import yaml
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)


def write_folder(out, built: dict) -> Path:
    """A NEW folder: workflow.yml, functions/, inputs/ (an existing folder is never overwritten)."""
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} already exists; nothing was written")
    (out / "inputs").mkdir(parents=True)
    for rel, text in built["functions"].items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text)
    for f, src in built["copy"].items():
        shutil.copyfile(src, out / "inputs" / f)
    (out / "workflow.yml").write_text(dump(built["workflow"]))
    return out


def validate_folder(out) -> dict:
    """The folder as the Pegasus host will read it: {"errors", "warnings", "jobs"}. Every job has an
    id and a transformation, every replica is a file in the folder (an input file may still be
    missing: the deploy copies it in), every job's input is a replica or another job's output."""
    import yaml
    out = Path(out)
    errors = []
    try:
        doc = yaml.safe_load((out / "workflow.yml").read_text())
        md = metadata(doc)
    except (OSError, ValueError, AttributeError) as e:
        return {"errors": [f"workflow.yml cannot be read: {e}"], "warnings": [], "jobs": 0}
    jobs = doc.get("jobs") or []
    written = {u["lfn"] for j in jobs for u in j.get("uses", []) if u["type"] == "output"}
    replicas = md.get("replicas") or {}
    for j in jobs:
        if not j.get("id") or not j.get("name"):
            errors.append(f"a job without an id or a transformation: {j}")
        if j.get("name") not in (md.get("transformations") or {}):
            errors.append(f"job {j.get('id')}: transformation {j.get('name')} has no curate.transformations entry")
        for u in j.get("uses", []):
            if u["type"] == "input" and u["lfn"] not in written and u["lfn"] not in replicas:
                errors.append(f"job {j['id']} reads {u['lfn']}, which no job writes and no replica provides")
    for lfn, path in replicas.items():
        if not (out / path).is_file() and not path.startswith("inputs/"):
            errors.append(f"replica {lfn}: {path} is not in the folder")
    for d in md.get("deliverables") or []:
        if d["file"] not in written:
            errors.append(f"deliverable {d['file']} is written by no job")
    return {"errors": errors, "warnings": [], "jobs": len(jobs)}


def main(argv=None) -> int:
    from cwl.validate import validate
    ap = argparse.ArgumentParser(description="CURATE CWL workflow -> Pegasus workflow folder")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("workflow")
    b.add_argument("--inputs")
    b.add_argument("--out", required=True)
    b.add_argument("--name", required=True)
    b.add_argument("--image")
    a = ap.parse_args(argv)
    wf, errors, _ = validate(a.workflow, a.inputs)
    if errors:
        print(json.dumps({"ok": False, "problems": errors}, indent=2))
        return 1
    try:
        built = build(wf, a.name, a.image)
        out = write_folder(a.out, built)
    except BuildError as e:
        print(json.dumps({"ok": False, "problems": e.problems}, indent=2))
        return 1
    except FileExistsError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    v = validate_folder(out)
    print(json.dumps({"ok": not v["errors"], "out": str(out), "jobs": v["jobs"], "inputs_needed": built["needed"],
                      "warnings": built["warnings"], "validation": v}, indent=2))
    return 0 if not v["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
