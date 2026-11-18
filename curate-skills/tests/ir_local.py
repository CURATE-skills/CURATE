"""A tiny local stand-in for Pegasus, for tests: run an IR's expanded jobs in dependency order,
each in its own empty folder, with its input files copied in under their flat names (what
condorio staging does), through the same runner shim the backend would use (by contract)."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

IR_DIR = Path(__file__).resolve().parents[1] / "curate_ir"
sys.path.insert(0, str(IR_DIR))

import validate  # noqa: E402

RUNNERS = {"plain": IR_DIR / "runner" / "curate_run.py", "faasr": IR_DIR / "runner" / "curate_faasr_run.py"}


def run_locally(ir_path: Path, work: Path, inputs_dir: Path | None = None) -> dict:
    """Returns {file name: path} of every file produced. Fails on the first job that fails."""
    ir, errors, _, deps = validate.prepare(validate.load(ir_path))
    assert errors == [], errors
    base = Path(ir["_base"])
    files = {i["file"]: Path(i["location"][len("file://"):]) for i in ir["inputs"]}
    nodes = {n["id"]: n for n in ir["nodes"]}
    done: set = set()
    while len(done) < len(nodes):
        ready = [k for k in nodes if k not in done and deps[k] <= done]
        assert ready, "no job ready: cycle?"
        for nid in ready:
            n = nodes[nid]
            job = work / nid
            job.mkdir(parents=True)
            code = job / ("code__" + Path(n["code"]).name)
            shutil.copy(base / n["code"], code)
            args = [sys.executable, str(RUNNERS[n.get("contract", "plain")]), "--code", code.name,
                    "--entrypoint", n.get("entrypoint", "main")]
            for name, v in n["inputs"].items():
                for f in validate.files_of(v):
                    shutil.copy(files[f], job / f)
                    args += ["--in-list" if isinstance(v, list) else "--in", f"{name}={f}"]
            for name, f in n["outputs"].items():
                args += ["--out", f"{name}={f}"]
            args += ["--params-json", json.dumps(n.get("params", {}))]
            p = subprocess.run(args, cwd=job, capture_output=True, text=True, timeout=120)
            assert p.returncode == 0, (nid, p.stderr[-3000:])
            for f in n["outputs"].values():
                files[f] = job / f
            done.add(nid)
    return files
