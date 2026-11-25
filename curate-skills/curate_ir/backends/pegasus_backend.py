#!/usr/bin/env python3
"""A CURATE Pegasus workflow folder -> catalogs on this host, plus submit/status/collect.

  python3 pegasus_backend.py emit    FOLDER/workflow.yml --work DIR [--runtime docker|singularity]
  python3 pegasus_backend.py submit  FOLDER/workflow.yml --work DIR [--runtime ...]
  python3 pegasus_backend.py status  FOLDER/workflow.yml --work DIR
  python3 pegasus_backend.py collect FOLDER/workflow.yml --work DIR
  python3 pegasus_backend.py run     FOLDER/workflow.yml --work DIR [--runtime ...] [--timeout-min 60]

FOLDER is what scripts/cwl_pegasus.py wrote: workflow.yml (Pegasus's own abstract workflow, with no
host paths), functions/ and inputs/. `emit` writes only what needs this host's absolute paths, from
the workflow's curate.* metadata: DIR/sites.yml, DIR/replicas.yml, DIR/transformations.yml and
DIR/pegasus.properties pointing pegasus-plan at them. `submit` emits, plans and submits with
pegasus-plan and returns at once (DAGMan runs the workflow; nothing waits), recording
DIR/submit.json with the Pegasus run folder. `status` reads DAGMan's exit line and jobstate.log
(read-only). `collect`, once DAGMan has finished, copies the declared outputs to
DIR/deliver/<deliver_as> and writes DIR/run-report.json. `run` is submit + wait + collect, for
manual use. emit/submit need PYTHONPATH=$(pegasus-config --python); status/collect need only
python3 + PyYAML.

Site setup: local HTCondor pool, condorio data configuration (no shared filesystem), so the same
workflow can target a remote/cloud pool by changing only the site catalog.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
META = "curate."

try:  # needed by emit/submit only; status and collect just read files
    from Pegasus.api import (  # noqa: E402
        Arch, Container, Directory, FileServer, Operation, OS,
        Properties, ReplicaCatalog, Site, SiteCatalog, Transformation, TransformationCatalog,
    )
    PEGASUS_IMPORT_ERROR = None
except ImportError as _e:  # PYTHONPATH=$(pegasus-config --python) not set
    PEGASUS_IMPORT_ERROR = _e

SHIM = HERE.parent / "runner" / "curate_run.py"  # calls main(inputs, outputs, params) on staged files


def load(path) -> dict:
    """The Pegasus workflow (dict) with `_folder` (where its functions/ and inputs/ are) and `_meta`
    (its curate.* metadata, decoded)."""
    import yaml
    path = Path(path).resolve()
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict) or "jobs" not in doc:
        raise SystemExit(f"{path} is not a Pegasus workflow written by curate-skills (scripts/cwl_pegasus.py)")
    doc["_folder"] = str(path.parent)
    doc["_meta"] = {k[len(META):]: json.loads(v) for k, v in (doc.get("metadata") or {}).items()
                    if k.startswith(META)}
    return doc


def normalize_image(url):
    """Pegasus parses docker://python:3.12 as host 'python:3.12'; it needs docker:///repo:tag."""
    for scheme in ("docker://", "library://", "shub://", "oras://"):
        if url.startswith(scheme) and not url.startswith(scheme + "/"):
            return scheme + "/" + url[len(scheme):]
    return url


def container_for(image, runtime, cache):
    image = normalize_image(image)
    key = (image, runtime)
    if key not in cache:
        ctype = Container.DOCKER if runtime == "docker" else Container.SINGULARITY
        kw = {"image_site": "local"} if image.startswith("file://") else {}
        cache[key] = Container(f"img{len(cache)}", ctype, image=image, **kw)
    return cache[key]


def emit(wf, work, runtime):
    """Write this host's catalogs and properties for the workflow into WORK; returns WORK."""
    if PEGASUS_IMPORT_ERROR is not None:
        raise SystemExit(f"Pegasus Python API not importable ({PEGASUS_IMPORT_ERROR}); "
                         "run with PYTHONPATH=$(pegasus-config --python)")
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    folder = Path(wf["_folder"])
    meta = wf["_meta"]
    for k in ("transformations", "replicas", "deliverables"):
        if k not in meta:
            raise SystemExit(f"{folder / 'workflow.yml'} has no curate.{k} metadata (not written by cwl_pegasus.py)")

    sc = SiteCatalog()
    local = Site("local", arch=Arch.X86_64, os_type=OS.LINUX)
    local.add_directories(
        Directory(Directory.SHARED_SCRATCH, str(work / "scratch"))
        .add_file_servers(FileServer("file://" + str(work / "scratch"), Operation.ALL)),
        Directory(Directory.LOCAL_STORAGE, str(work / "output"))
        .add_file_servers(FileServer("file://" + str(work / "output"), Operation.ALL)),
    )
    pool = Site("condorpool", arch=Arch.X86_64, os_type=OS.LINUX)
    pool.add_pegasus_profile(style="condor")
    pool.add_condor_profile(universe="vanilla")
    sc.add_sites(local, pool)

    rc = ReplicaCatalog()
    for lfn, rel in meta["replicas"].items():  # code files and the workflow's input files, in FOLDER
        rc.add_replica("local", lfn, "file://" + str((folder / rel).resolve()))

    tc, containers = TransformationCatalog(), {}
    for name, t in meta["transformations"].items():  # one per step; every job runs the runner shim
        c = container_for(t["image"], runtime, containers)
        res = t.get("resources") or {}
        tr = Transformation(name, site="local", pfn="file://" + str(SHIM), is_stageable=True, container=c)
        if "cpus" in res:
            tr.add_pegasus_profile(cores=int(res["cpus"]))
        if "memory_mb" in res:
            tr.add_pegasus_profile(memory=int(res["memory_mb"]))
        tc.add_transformations(tr)
    tc.add_containers(*containers.values())

    sc.write(str(work / "sites.yml"))
    rc.write(str(work / "replicas.yml"))
    tc.write(str(work / "transformations.yml"))
    props = Properties()
    props["pegasus.data.configuration"] = "condorio"
    props["pegasus.monitord.encoding"] = "json"
    props["pegasus.transfer.worker.package.strict"] = "false"  # use host worker package in any container OS
    props["pegasus.catalog.site.file"] = str(work / "sites.yml")
    props["pegasus.catalog.replica"] = "YAML"
    props["pegasus.catalog.replica.file"] = str(work / "replicas.yml")
    props["pegasus.catalog.transformation.file"] = str(work / "transformations.yml")
    props.write(str(work / "pegasus.properties"))
    return work


def sh(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


SUBMIT_RECORD = "submit.json"   # in the work folder: written by submit, read by status / collect
EXIT_RX = re.compile(r"EXITING WITH STATUS (\d+)")
CLUSTER_RX = re.compile(r"submitted to cluster (\d+)")
JOB_WORD = {"SUBMIT": "queued", "EXECUTE": "running", "JOB_TERMINATED": "running",
            "JOB_SUCCESS": "running", "POST_SCRIPT_STARTED": "running", "POST_SCRIPT_TERMINATED": "running",
            "POST_SCRIPT_SUCCESS": "done", "JOB_FAILURE": "failed", "POST_SCRIPT_FAILURE": "failed",
            "JOB_HELD": "held", "JOB_ABORTED": "failed", "SUBMIT_FAILED": "failed"}


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def submit(wf, work, runtime):
    """Emit, plan and submit to HTCondor, then return at once: DAGMan runs the workflow.
    Writes WORK/submit.json (run_dir, submitted_at, ...) and WORK/pegasus-plan.log. Refuses a
    work folder that was already submitted."""
    work = Path(work).resolve()
    if (work / SUBMIT_RECORD).exists():
        raise SystemExit(f"{work} was already submitted ({SUBMIT_RECORD} exists); use a new --work folder")
    work = emit(wf, work, runtime)
    rc, out = sh(["pegasus-plan", "--conf", "pegasus.properties", "--sites", "condorpool",
                  "--output-sites", "local", "--dir", "submit", "--cleanup", "leaf",
                  "--submit", str(Path(wf["_folder"]) / "workflow.yml")], work)
    (work / "pegasus-plan.log").write_text(out)
    if rc != 0:
        raise SystemExit(f"pegasus-plan failed (exit {rc}); see {work / 'pegasus-plan.log'}")
    runs = sorted((work / "submit").glob(f"*/pegasus/{wf['name']}/run*"))
    if not runs:
        raise SystemExit(f"pegasus-plan gave no run folder; see {work / 'pegasus-plan.log'}")
    m = CLUSTER_RX.search(out)
    rec = {"workflow": wf["name"], "workflow_file": str(Path(wf["_folder"]) / "workflow.yml"), "work": str(work),
           "run_dir": str(runs[-1]), "submitted_at": _now(), "runtime": runtime,
           "dagman_cluster": int(m.group(1)) if m else None}
    (work / SUBMIT_RECORD).write_text(json.dumps(rec, indent=2))
    return rec


def _record(work):
    path = Path(work) / SUBMIT_RECORD
    if not path.is_file():
        raise SystemExit(f"{path} not found: this work folder was never submitted")
    return json.loads(path.read_text())


def job_states(run_dir, wf):
    """Each job's state from jobstate.log (Pegasus names the job "<transformation>_<id>"):
    pending / queued / running / held / done / failed."""
    names = {f"{j['name']}_{j['id']}": j["id"] for j in wf["jobs"]}
    last = {}
    log = Path(run_dir) / "jobstate.log"
    if log.is_file():
        for line in log.read_text(errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[1] in names:
                last[names[parts[1]]] = parts[2]
    return {j["id"]: JOB_WORD.get(last.get(j["id"]), "pending") for j in wf["jobs"]}


def status(wf, work):
    """Read-only: DAGMan's exit status (finished once "EXITING WITH STATUS n" is logged) and each
    node's job state. state = running / success / failure."""
    rec = _record(work)
    run_dir = Path(rec["run_dir"])
    codes = [m.group(1) for f in run_dir.glob("*.dag.dagman.out")
             for m in EXIT_RX.finditer(f.read_text(errors="replace"))]
    code = int(codes[-1]) if codes else None
    state = "running" if code is None else "success" if code == 0 else "failure"
    jobs = job_states(run_dir, wf)
    counts = {}
    for v in jobs.values():
        counts[v] = counts.get(v, 0) + 1
    return {"workflow": rec["workflow"], "state": state, "dagman_exit": code, "run_dir": str(run_dir),
            "submitted_at": rec["submitted_at"], "jobs": jobs, "counts": counts}


def collect(wf, work):
    """After DAGMan finished: copy the declared outputs to WORK/deliver/<deliver_as> and write
    WORK/run-report.json. Refuses while the workflow is still running."""
    work = Path(work).resolve()
    st = status(wf, work)
    if st["state"] == "running":
        raise SystemExit("the workflow is still running; collect after DAGMan has finished")
    deliver = work / "deliver"
    delivered, missing = [], []
    for o in wf["_meta"].get("deliverables", []):
        src = work / "output" / o["file"]
        dst = deliver / o.get("deliver_as", o["file"])
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            delivered.append(str(dst.relative_to(deliver)))
        else:
            missing.append(o["file"])
    report = {"workflow": wf["name"],
              "status": "success" if st["state"] == "success" else f"failure (DAGMan status {st['dagman_exit']})",
              "submitted_at": st["submitted_at"], "collected_at": _now(), "run_dir": st["run_dir"],
              "delivered": delivered, "missing": missing, "jobs": st["jobs"],
              "runtime": _record(work).get("runtime")}
    (work / "run-report.json").write_text(json.dumps(report, indent=2))
    return report


def run(wf, work, runtime, timeout_min):
    """For manual use: submit, wait for DAGMan, collect."""
    t0 = time.time()
    rec = submit(wf, work, runtime)
    while status(wf, rec["work"])["state"] == "running":
        if time.time() - t0 > timeout_min * 60:
            report = {"workflow": rec["workflow"], "status": "timeout", "run_dir": rec["run_dir"],
                      "wall_s": round(time.time() - t0, 1), "delivered": [], "missing": [], "runtime": runtime}
            print(json.dumps(report, indent=2))
            return 1
        time.sleep(5)
    report = collect(wf, rec["work"])
    report["wall_s"] = round(time.time() - t0, 1)
    (Path(rec["work"]) / "run-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "success" and not report["missing"] else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["emit", "submit", "status", "collect", "run"])
    ap.add_argument("workflow", help="FOLDER/workflow.yml (scripts/cwl_pegasus.py)")
    ap.add_argument("--work", required=True)
    ap.add_argument("--runtime", choices=["docker", "singularity"], default="docker")
    ap.add_argument("--timeout-min", type=float, default=60, help="run only")
    a = ap.parse_args()
    wf = load(a.workflow)
    if a.cmd == "emit":
        print(emit(wf, a.work, a.runtime) / "pegasus.properties")
    elif a.cmd == "submit":
        print(json.dumps(submit(wf, a.work, a.runtime), indent=2))
    elif a.cmd == "status":
        print(json.dumps(status(wf, a.work), indent=2))
    elif a.cmd == "collect":
        r = collect(wf, a.work)
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["status"] == "success" and not r["missing"] else 1)
    else:
        sys.exit(run(wf, a.work, a.runtime, a.timeout_min))


if __name__ == "__main__":
    main()
