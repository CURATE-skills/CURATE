# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "cwl-utils==0.47", "pyyaml==6.0.2"]
# ///
"""The Pegasus builder: a CURATE CWL workflow (docs/CWL_IR.md) -> a Pegasus IR folder.

  cwl_pegasus.py build workflow.yml [--inputs inputs.yml] --out DIR --name NAME [--image URL]

The workflow is expanded here (cwl/expand_cwl.py) and written as an already-expanded IR
(`ir.yaml` with `expanded: true`: its nodes are the jobs), next to functions/ (the code,
unchanged) and inputs/ (the workflow's local input files, copied; the IR names them
`inputs/<file>`). That is the folder pegasus_deploy.py copies to the Pegasus host and
curate_ir/backends/pegasus_backend.py plans, so the host never reads CWL.

curate:after becomes a job dependency; curate:secrets an HTCondor `getenv` of those names (values
come from the submit host's environment and never enter a file); curate:run_if is refused, since
Pegasus runs every job of a fixed DAG. `--image` replaces the workflow's default image for this
run (Gate 4's "different image"); a step's own image stays.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "curate_ir"))


class BuildError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def build(wf, name: str, image: str | None = None) -> dict:
    """{"ir", "functions": {code path: text}, "copy": {file: local source}, "needed": [files with no local
    source], "warnings"}. Raises BuildError."""
    from cwl.expand_cwl import expand_cwl

    problems, warnings = [], []
    for sid, s in wf.steps.items():
        if s.run_if:
            problems.append(f"step {sid} runs only if {s.run_if['step']} returns {s.run_if['result']} "
                            "(curate:run_if): Pegasus runs a fixed DAG in which every step runs, so it cannot "
                            "honour the condition. Deploy to FaaSr instead.")
        if s.packages:
            warnings.append(f"step {sid} imports {', '.join(s.packages)}: these must already be in the image; "
                            "nothing is installed at run time")
    if problems:
        raise BuildError(problems)
    exp = expand_cwl(wf)
    copy, needed = {}, []
    for inp in exp["inputs"]:
        loc = inp["location"]
        if loc.startswith("file://"):
            src = Path(loc[len("file://"):])
            if src.is_file():
                copy[inp["file"]] = str(src)
            else:
                needed.append(inp["file"])
            inp["location"] = "inputs/" + inp["file"]
    ir = {"ir_version": "0.2", "expanded": True, "name": name,
          "description": " ".join((exp["description"] or name).split())[:200], "defaults": exp["defaults"]}
    if image:
        ir["defaults"]["image"] = image
    ir.update(inputs=exp["inputs"], nodes=[{k: v for k, v in n.items()} for n in exp["nodes"]],
              outputs=exp["outputs"])
    functions = {}
    for n in exp["nodes"]:
        functions.setdefault(n["code"], (wf.base / n["code"]).read_text())
    return {"ir": ir, "functions": functions, "copy": copy, "needed": sorted(needed), "warnings": warnings}


def write_folder(out, built: dict) -> Path:
    """A NEW folder: ir.yaml, functions/, inputs/ (an existing folder is never overwritten)."""
    import yaml

    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} already exists; nothing was written")
    (out / "inputs").mkdir(parents=True)
    for rel, text in built["functions"].items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text)
    for f, src in built["copy"].items():
        shutil.copyfile(src, out / "inputs" / f)
    (out / "ir.yaml").write_text(yaml.safe_dump(built["ir"], sort_keys=False, default_flow_style=False,
                                                allow_unicode=True))
    return out


def validate_folder(out) -> dict:
    """curate_ir/validate.py on the folder: {"errors", "warnings", "jobs"}."""
    import validate as V0
    exp, errors, warnings, _ = V0.prepare(V0.load(Path(out) / "ir.yaml"))
    return {"errors": errors, "warnings": warnings, "jobs": len(exp.get("nodes", []))}


def main(argv=None) -> int:
    from cwl.validate import validate
    ap = argparse.ArgumentParser(description="CURATE CWL workflow -> Pegasus IR folder")
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
