# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "cwl-utils==0.47", "pyyaml==6.0.2"]
# ///
"""A CURATE WorkflowSpec as a CURATE CWL workflow (docs/CWL_IR.md), deterministically.

  from spec_cwl import build, content_hash
  built = build(spec)       # {"workflow", "inputs", "functions", "warnings"}

Everything IR v0.2 covers goes through today's mapping: pegasus_ir.build_ir(spec, pegasus=False)
and then cwl/from_ir.convert (steps, files, fan-out, gathers, folders, list outputs, rank
params, deliverables, packages, file-less edges as curate:after). What IR v0.2 cannot carry is
taken from the spec directly:
- a conditional edge a -> b (`condition: "True"|"False"`): curate:run_if {step: a, result} on b
  (and a leaves b's curate:after, which run_if implies);
- a step's `secrets`: curate:secrets {names}.
The agent never writes CWL.

content_hash() is the sha256 the brief defines: the normalized workflow without label, doc,
s: fields, curate:content_hash and input defaults, plus the function files. It only detects
duplicates; it never decides a version.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "curate_ir"))
import pegasus_ir as P  # noqa: E402
from curate_models import WorkflowSpec  # noqa: E402


class BuildError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def build(spec: WorkflowSpec, image: str = P.DEFAULT_IMAGE) -> dict:
    """{"workflow": CWL dict, "inputs": inputs-file dict, "functions": {code path: code}, "warnings"}.
    Raises BuildError."""
    import validate as V0
    from cwl.from_ir import ConvertError, convert

    try:
        built = P.build_ir(spec, name=P.ir_name(spec.name), image=image, pegasus=False)
    except P.MappingError as e:
        raise BuildError(e.problems) from e
    with tempfile.TemporaryDirectory() as tmp:
        folder = P.write_folder(Path(tmp) / "ir", built)
        try:
            conv = convert(V0.load(folder / "ir.yaml"))
        except ConvertError as e:
            raise BuildError(e.problems) from e
        base = "file://" + str(folder.resolve()) + "/"
        doc = _relative(conv["workflow"], base)
        values = _relative(conv["inputs"], base)

    ids = {n.name: n.name.lower() for n in spec.nodes}
    problems = []
    conditional = {}
    for e in spec.edges:
        if e.condition is None:
            continue
        if e.to_node in conditional:
            problems.append(f"step {e.to_node} runs on the result of both {conditional[e.to_node][0]} and "
                            f"{e.from_node}; a step can depend on one step's result (curate:run_if). This is a "
                            "deliberate change: the FaaSr bundle used to allow it, but how FaaSr behaves when "
                            "one branch is taken and the other is not was never tested")
            continue
        conditional[e.to_node] = (e.from_node, e.condition == "True")
    if problems:
        raise BuildError(problems)
    for to, (frm, result) in conditional.items():
        hints = doc["steps"][ids[to]]["hints"]
        hints["curate:run_if"] = {"step": ids[frm], "result": result}
        after = [s for s in (hints.get("curate:after") or {}).get("steps", []) if s != ids[frm]]
        if after:
            hints["curate:after"] = {"steps": after}
        else:
            hints.pop("curate:after", None)
    for n in spec.nodes:
        if n.secrets:
            doc["steps"][ids[n.name]]["hints"]["curate:secrets"] = {"names": sorted(n.secrets)}
    doc["label"] = spec.name
    if spec.description:
        doc["doc"] = " ".join(spec.description.split())
    return {"workflow": doc, "inputs": values, "functions": built["functions"], "warnings": built["warnings"]}


def _relative(value, base: str):
    """Locations inside the temporary IR folder become relative ("inputs/<file>")."""
    if isinstance(value, list):
        return [_relative(v, base) for v in value]
    if isinstance(value, dict):
        out = {k: _relative(v, base) for k, v in value.items()}
        if out.get("class") == "File" and str(out.get("location", "")).startswith(base):
            out["location"] = out["location"][len(base):]
        return out
    return value


def content_hash(workflow: dict, functions: dict[str, str]) -> str:
    """sha256 over the normalized workflow (no label, doc, s: fields, curate:content_hash or input
    defaults) and the function files (by code path)."""
    doc = copy.deepcopy(workflow)
    for k in list(doc):
        if k in ("label", "doc", "curate:content_hash") or k.startswith("s:"):
            del doc[k]
    for i in (doc.get("inputs") or {}).values():
        if isinstance(i, dict):
            i.pop("default", None)
    h = hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode())
    for path in sorted(functions):
        h.update(b"\0" + path.encode() + b"\0" + hashlib.sha256(functions[path].encode()).digest())
    return "sha256:" + h.hexdigest()


def write(built: dict, out) -> Path:
    """A NEW folder: workflow.yml, inputs.yml and functions/ (the code, unchanged)."""
    import yaml

    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} already exists; nothing was written")
    out.mkdir(parents=True)
    for rel, code in built["functions"].items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(code)
    dump = lambda x: yaml.safe_dump(x, sort_keys=False, default_flow_style=False, allow_unicode=True)  # noqa: E731
    (out / "workflow.yml").write_text(dump(built["workflow"]))
    (out / "inputs.yml").write_text(dump(built["inputs"]))
    return out
