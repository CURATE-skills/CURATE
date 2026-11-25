"""A tiny local stand-in for Pegasus, for tests: run a Pegasus workflow folder written by
scripts/cwl_pegasus.py (workflow.yml, functions/, inputs/) job by job in dependency order. Each job
runs in its own empty folder with every file in its `uses` copied in under its logical name (what
condorio staging does), through the runner shim with the job's own `arguments`, split the way
HTCondor splits them (single quotes group, as around --params-json). Replicas (code and workflow
input files) come from the workflow's curate.replicas metadata, as the backend's replica catalog does."""
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

IR_DIR = Path(__file__).resolve().parents[1] / "curate_ir"
RUNNER = IR_DIR / "runner" / "curate_run.py"


def run_locally(workflow: Path, work: Path, inputs_dir: Path | None = None) -> dict:
    """Returns {logical file name: path} of every file produced. Fails on the first job that fails.
    `workflow` is FOLDER/workflow.yml; `inputs_dir` replaces FOLDER/inputs/ for the input files."""
    workflow = Path(workflow)
    folder = workflow.parent
    doc = yaml.safe_load(workflow.read_text())
    md = {k[len("curate."):]: json.loads(v) for k, v in (doc.get("metadata") or {}).items() if k.startswith("curate.")}
    files = {}
    for lfn, rel in md["replicas"].items():
        src = (Path(inputs_dir) / Path(rel).name) if inputs_dir and rel.startswith("inputs/") else folder / rel
        if src.is_file():
            files[lfn] = src
    jobs = {j["id"]: j for j in doc["jobs"]}
    writer = {u["lfn"]: j["id"] for j in doc["jobs"] for u in j["uses"] if u["type"] == "output"}
    parents = {jid: {writer[u["lfn"]] for u in j["uses"] if u["type"] == "input" and u["lfn"] in writer}
               for jid, j in jobs.items()}
    for d in doc.get("jobDependencies") or []:
        for child in d["children"]:
            parents[child].add(d["id"])
    done: set = set()
    while len(done) < len(jobs):
        ready = [k for k in jobs if k not in done and parents[k] <= done]
        assert ready, "no job ready: cycle?"
        for jid in ready:
            j = jobs[jid]
            job = work / jid
            job.mkdir(parents=True)
            for u in j["uses"]:
                if u["type"] == "input":
                    assert u["lfn"] in files, f"{jid}: input {u['lfn']} was never written or provided"
                    shutil.copy(files[u["lfn"]], job / u["lfn"])
            args = [sys.executable, str(RUNNER), *shlex.split(" ".join(j["arguments"]))]
            p = subprocess.run(args, cwd=job, capture_output=True, text=True, timeout=120)
            assert p.returncode == 0, (jid, p.stderr[-3000:])
            for u in j["uses"]:
                if u["type"] == "output":
                    assert (job / u["lfn"]).is_file(), f"{jid} did not write {u['lfn']}"
                    files[u["lfn"]] = job / u["lfn"]
            done.add(jid)
    return {k: v for k, v in files.items() if not k.startswith("code__")}  # the workflow's files, not its code
