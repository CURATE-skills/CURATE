#!/usr/bin/env python3
"""CURATE IR -> Pegasus 5.1 workflow, plus plan/run/deliver.

  python3 pegasus_backend.py emit ir.yaml --work DIR [--runtime docker|singularity]
  python3 pegasus_backend.py run  ir.yaml --work DIR [--runtime ...] [--timeout-min 60]

`run` emits, plans with pegasus-plan, waits, then copies declared outputs to
DIR/deliver/<deliver_as>. That folder is what a benchmark adapter hands to the
verifier (e.g. copy into /app/results of a fresh task container).

Site setup: local HTCondor pool, condorio data configuration (no shared
filesystem), so the same emitted workflow can target a remote/cloud pool by
changing only the site catalog.
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
sys.path.insert(0, str(HERE.parent))
from validate import files_of, load, prepare  # noqa: E402

from Pegasus.api import (  # noqa: E402
    Arch, Container, Directory, File, FileServer, Job, Operation, OS,
    Properties, ReplicaCatalog, Site, SiteCatalog, Transformation,
    TransformationCatalog, Workflow,
)

SHIM = HERE.parent / "runner" / "curate_run.py"


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


def emit(ir, work, runtime):
    ir, errors, warnings, _ = prepare(ir)   # v0.2 fan-out expanded into plain nodes
    for w in warnings:
        print("warn :", w, file=sys.stderr)
    if errors:
        raise SystemExit("IR invalid:\n  " + "\n  ".join(errors))

    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    base = Path(ir["_base"])
    defaults = ir.get("defaults", {})

    props = Properties()
    props["pegasus.data.configuration"] = "condorio"
    props["pegasus.monitord.encoding"] = "json"
    props["pegasus.transfer.worker.package.strict"] = "false"  # use host worker package in any container OS
    props.write(str(work / "pegasus.properties"))

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

    tc, rc = TransformationCatalog(), ReplicaCatalog()
    containers = {}
    for inp in ir.get("inputs", []):
        rc.add_replica("local", inp["file"], inp["location"])

    deliverables = {o["file"] for o in ir.get("outputs", [])}
    wf = Workflow(ir["name"])
    done_origins = set()
    for n in ir["nodes"]:
        origin = n.get("_origin", n["id"])      # fan-out copies share one transformation
        code_lfn = f"code__{origin}__{Path(n['code']).name}"
        if origin not in done_origins:
            done_origins.add(origin)
            image = n.get("image") or defaults["image"]
            c = container_for(image, runtime, containers)
            res = {**defaults.get("resources", {}), **n.get("resources", {})}
            tr = Transformation(origin, site="local", pfn="file://" + str(SHIM),
                                is_stageable=True, container=c)
            if "cpus" in res:
                tr.add_pegasus_profile(cores=int(res["cpus"]))
            if "memory_mb" in res:
                tr.add_pegasus_profile(memory=int(res["memory_mb"]))
            tc.add_transformations(tr)
            rc.add_replica("local", code_lfn, "file://" + str((base / n["code"]).resolve()))
        job = Job(origin, _id=n["id"])
        job.add_args("--code", code_lfn, "--entrypoint", n.get("entrypoint", "main"))
        job.add_inputs(File(code_lfn))
        for name, v in n.get("inputs", {}).items():
            flag = "--in-list" if isinstance(v, list) else "--in"
            for f in files_of(v):
                job.add_args(flag, f"{name}={f}")
                job.add_inputs(File(f))
        for name, f in n.get("outputs", {}).items():
            job.add_args("--out", f"{name}={f}")
            job.add_outputs(File(f), stage_out=f in deliverables)
        job.add_args("--params-json", "'" + json.dumps(n.get("params", {})) + "'")
        wf.add_jobs(job)

    tc.add_containers(*containers.values())
    wf.add_site_catalog(sc)
    wf.add_transformation_catalog(tc)
    wf.add_replica_catalog(rc)
    wf.write(str(work / "workflow.yml"))
    return work


def sh(cmd, cwd):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def run(ir, work, runtime, timeout_min):
    work = emit(ir, work, runtime)
    ir, _, _, _ = prepare(ir)                # expanded outputs (may come from fan-out)
    t0 = time.time()
    rc, out = sh(["pegasus-plan", "--conf", "pegasus.properties", "--sites", "condorpool",
                  "--output-sites", "local", "--dir", "submit", "--cleanup", "leaf",
                  "--submit", "workflow.yml"], work)
    if rc != 0:
        print(out)
        raise SystemExit("pegasus-plan failed")
    run_dir = next((work / "submit").glob(f"*/pegasus/{ir['name']}/run*"))
    # Finished when DAGMan logs "EXITING WITH STATUS n" (independent of pegasus-status/Python env).
    status, pat = "timeout", re.compile(r"EXITING WITH STATUS (\d+)")
    while time.time() - t0 < timeout_min * 60:
        codes = [m.group(1) for f in run_dir.glob("*.dag.dagman.out")
                 for m in pat.finditer(f.read_text(errors="replace"))]
        if codes:
            status = "success" if codes[-1] == "0" else f"failure (DAGMan status {codes[-1]})"
            break
        time.sleep(5)

    deliver = work / "deliver"
    delivered, missing = [], []
    for o in ir.get("outputs", []):
        src = work / "output" / o["file"]
        dst = deliver / o.get("deliver_as", o["file"])
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            delivered.append(str(dst.relative_to(deliver)))
        else:
            missing.append(o["file"])
    report = {"workflow": ir["name"], "status": status, "wall_s": round(time.time() - t0, 1),
              "run_dir": str(run_dir), "delivered": delivered, "missing": missing,
              "runtime": runtime}
    (work / "run-report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0 if status == "success" and not missing else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["emit", "run"])
    ap.add_argument("ir")
    ap.add_argument("--work", required=True)
    ap.add_argument("--runtime", choices=["docker", "singularity"], default="docker")
    ap.add_argument("--timeout-min", type=float, default=60)
    a = ap.parse_args()
    ir = load(a.ir)
    if a.cmd == "emit":
        print(emit(ir, a.work, a.runtime) / "workflow.yml")
    else:
        sys.exit(run(ir, a.work, a.runtime, a.timeout_min))


if __name__ == "__main__":
    main()
