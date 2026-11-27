# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "cwl-utils==0.47", "pyyaml==6.0.2"]
# ///
"""A CURATE WorkflowSpec as a CURATE CWL workflow (docs/CWL_IR.md), deterministically.

  from spec_cwl import build, content_hash
  built = build(spec)       # {"workflow", "inputs", "functions", "warnings"}

The mapping:
- a step is a CWL step with the spec node's name lower-cased as its id, slots input1.., output1..,
  and its code at functions/<node name>.py (curate:function);
- a step with rank N scatters over the workflow input `rankN` (int[] 1..N) as its `rank`, with
  `max_rank` N as a default; a `{rank}` file family has the count plain_contract.family_counts
  gives it, written as `{rankN}` in its curate:name;
- a ranked reader of a family with its own count reads its own copy (dotproduct with `rank`);
  an unranked reader, or a ranked one whose input says read: all, gets the whole list (File[]);
  an unranked writer of a family writes one list (File[], curate:axis rankN);
- a folder input (curate:folder) gathers every file plain_contract.folder_matches gives it;
- an edge with no file between its steps is curate:after; a conditional edge a -> b is
  curate:run_if {step: a, result} on b (and a leaves b's curate:after, which run_if implies);
- a step's `secrets` are curate:secrets {names}; its imports SoftwareRequirement packages;
- the files read but written by no step are workflow inputs (locations `inputs/<file>`); the files
  written but read by no step are the workflow outputs (curate:deliver_as `results/<file>`).
The agent never writes CWL. Until phase 8 this went through IR v0.2 (pegasus_ir.build_ir (now pegasus_target.py), then
cwl/from_ir.py); the output is byte for byte the same (tests/test_real_versions.py,
tests/test_cwl_reference.py).

content_hash() is the sha256 the brief defines: the normalized workflow without label, doc,
s: fields, curate:content_hash and input defaults, plus the function files. It only detects
duplicates; it never decides a version.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "curate_ir"))
import envfile  # noqa: E402
import plain_contract as PC  # noqa: E402
from curate_models import WorkflowSpec, clean_dependencies  # noqa: E402

DEFAULT_IMAGE = envfile.DEFAULT_PEGASUS_IMAGE
DEFAULT_RESOURCES = {"coresMin": 1, "ramMin": 2048}
RANK = "{rank}"
NODE_ID = re.compile(r"^[a-z0-9_]+$")


class BuildError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _axis(count: int) -> str:
    return f"rank{count}"


def _id(name: str, taken: set) -> str:
    """A CWL id for a workflow input or output, from its file name: lower case, [a-z0-9_]."""
    base = re.sub(r"[^a-z0-9_]+", "_", name.replace("{", "").replace("}", "").lower()).strip("_") or "x"
    if base[0].isdigit():
        base = "f_" + base
    out, n = base, 2
    while out in taken:
        out, n = f"{base}_{n}", n + 1
    taken.add(out)
    return out


def _image(url: str) -> str:
    return url[len("docker:///"):] if url.startswith("docker:///") else url


def _check(spec: WorkflowSpec, image: str) -> tuple[list[str], list[str]]:
    """(problems, warnings): what makes the spec unusable as a workflow, one message each."""
    problems, warnings, ids = [], [], set()
    for n in spec.nodes:
        nid = n.name.lower()
        if not NODE_ID.match(nid) or "__" in nid:
            problems.append(f"step {n.name}: Pegasus needs a name of letters, digits and single underscores")
        elif nid in ids:
            problems.append(f"step {n.name}: two steps have the name {nid} once lower-cased")
        ids.add(nid)
        if not n.code:
            problems.append(f"step {n.name} has no code; run curate-generate first")
        elif not PC.is_plain(n.code):
            problems.append(f"step {n.name} is not a plain main(inputs, outputs, params) function (the older "
                            "FaaSr style): regenerate it, or migrate the catalog it came from "
                            "(scripts/migrate_catalog.py)")
        deps = clean_dependencies(n.dependencies)
        if deps:
            warnings.append(f"step {n.name} imports {', '.join(deps)}: these must already be in the image "
                            f"{image}; nothing is installed at run time")
    return problems, warnings


def build(spec: WorkflowSpec, image: str = DEFAULT_IMAGE) -> dict:
    """{"workflow": CWL dict, "inputs": inputs-file dict, "functions": {code path: code}, "warnings"}.
    Raises BuildError."""
    from cwl import NS

    problems, warnings = _check(spec, image)
    nodes = spec.nodes
    by_name = {n.name: n for n in nodes}
    rank = {n.name: max(1, int(n.rank or 1)) for n in nodes}
    sid = {n.name: n.name.lower() for n in nodes}
    producers: dict[str, tuple[str, int]] = {}  # file -> (writer, output index)
    consumers: set[str] = set()
    folders: dict[tuple[str, int], list[str]] = {}  # folder input -> the files it holds
    for n in nodes:
        for i, o in enumerate(n.outputs, 1):
            if o.name in producers:
                problems.append(f"file {o.name} is written by both {producers[o.name][0]} and {n.name}")
            producers.setdefault(o.name, (n.name, i))
        for i, inp in enumerate(n.inputs, 1):
            if inp.kind == "folder":
                folders[(n.name, i)] = PC.folder_matches(spec, n.name, inp.name)
                if not folders[(n.name, i)]:
                    problems.append(
                        f"step {n.name} reads the folder input {inp.name} (every file starting with "
                        f"{PC.prefix(inp.name)!r}), but no step writes or reads such a file: on Pegasus the "
                        "files must be known when the workflow is built")
            else:
                consumers.add(inp.name)
    count, mode, counted = PC.family_counts(spec)  # {rank} file -> number of copies
    problems += counted
    problems += PC.folder_conflicts(spec)
    if problems:
        raise BuildError(problems)

    def named(f: str) -> str:  # a file name with its family's axis
        return f.replace(RANK, "{" + _axis(count[f]) + "}") if f in count else f

    def reads(b: str) -> set[str]:
        got = {inp.name for i, inp in enumerate(by_name[b].inputs, 1) if (b, i) not in folders}
        return got | {f for (c, _), fs in folders.items() if c == b for f in fs}

    after: dict[str, list[str]] = {}  # an edge with no file between its steps
    for e in spec.edges:
        a, b = e.from_node, e.to_node
        if not {o.name for o in by_name[a].outputs} & reads(b) and sid[a] not in after.get(b, []):
            after.setdefault(b, []).append(sid[a])

    # workflow inputs (files no step writes) and their ids; step ids are taken first
    taken = set(sid.values())
    external = sorted(f for f in consumers if f not in producers)
    in_id = {f: _id(named(f), taken) for f in external}

    def source(f: str) -> tuple[str, int]:
        """(CWL source, list depth) of a file: a step output (a list when a family's unranked writer
        writes every copy, or a ranked writer's copies seen from outside), or a workflow input."""
        if f in producers:
            w, i = producers[f]
            return f"{sid[w]}/output{i}", 1 if f in count else 0
        return in_id[f], 1 if f in count else 0

    used_axes: list[int] = []
    steps, scatter_used, merge_used = {}, False, False
    for n in nodes:
        r = rank[n.name]
        ins, op_in, op_out, outs, scatter = {}, {}, {}, [], []
        if r > 1:
            ins["rank"] = _axis(r)
            op_in["rank"] = "int"
            scatter.append("rank")
            used_axes.append(r)
            ins["max_rank"] = {"default": r}
            op_in["max_rank"] = "int"
        paired = []
        for i, inp in enumerate(n.inputs, 1):
            key = f"input{i}"
            if (n.name, i) in folders:
                srcs = [source(g) for g in folders[(n.name, i)]]
                if len(srcs) == 1 and srcs[0][1] == 1:
                    ins[key] = {"source": srcs[0][0], "curate:folder": True}
                else:
                    ins[key] = {"source": [s for s, _ in srcs], "linkMerge": "merge_flattened", "curate:folder": True}
                    merge_used = True
                op_in[key] = "File[]"
                continue
            f = inp.name
            ref, depth = source(f)
            ins[key] = ref
            if depth and r > 1 and count[f] == r and mode.get((n.name, i)) != "all":
                paired.append(key)  # its own copy
                depth = 0
            op_in[key] = "File" + "[]" * depth
        scatter += paired
        for i, o in enumerate(n.outputs, 1):
            key = f"output{i}"
            if o.name in count and count[o.name] != r:  # an unranked writer of a family: every copy
                op_out[key] = {"type": "File[]", "curate:axis": _axis(count[o.name]), "curate:name": named(o.name)}
                used_axes.append(count[o.name])
            else:
                op_out[key] = {"type": "File", "curate:name": named(o.name)}
            outs.append(key)
        hints = {"curate:function": {"code": f"functions/{n.name}.py", "entrypoint": "main"}}
        deps = clean_dependencies(n.dependencies)
        if deps:
            hints["SoftwareRequirement"] = {"packages": [{"package": p} for p in deps]}
        if after.get(n.name):
            hints["curate:after"] = {"steps": list(after[n.name])}
        step = {}
        if scatter:
            scatter_used = True
            step["scatter"] = scatter if len(scatter) > 1 else scatter[0]
            if len(scatter) > 1:
                step["scatterMethod"] = "dotproduct"
        step.update({"in": ins, "out": outs,
                     "run": {"class": "Operation", "inputs": op_in, "outputs": op_out}, "hints": hints})
        steps[sid[n.name]] = step

    # workflow inputs: the axes in use, then the files
    wf_inputs, values = {}, {}
    for c in sorted(set(used_axes)):
        wf_inputs[_axis(c)] = {"type": "int[]", "default": list(range(1, c + 1))}
        values[_axis(c)] = list(range(1, c + 1))
    for f in external:
        if f in count:
            val = [{"class": "File", "location": "inputs/" + f.replace(RANK, str(v))} for v in range(1, count[f] + 1)]
            wf_inputs[in_id[f]] = {"type": "File[]", "default": val}
        else:
            val = {"class": "File", "location": "inputs/" + f}
            wf_inputs[in_id[f]] = {"type": "File", "default": val}
        values[in_id[f]] = val

    # workflow outputs: the end files (written by a step, read by none; folders read what they hold)
    read = consumers | {f for fs in folders.values() for f in fs}
    wf_outputs = {}
    for f in sorted(producers):
        if f in read:
            continue
        ref, depth = source(f)
        wf_outputs[_id(named(f), taken)] = {"type": "File" + "[]" * depth, "outputSource": ref,
                                            "curate:deliver_as": "results/" + named(f)}

    doc = {"cwlVersion": "v1.2", "class": "Workflow", "$namespaces": {"curate": NS}, "label": spec.name,
           "doc": " ".join((spec.description or spec.name).split()) if spec.description
           else " ".join(spec.name.split())[:200]}
    requirements = {}
    if scatter_used:
        requirements["ScatterFeatureRequirement"] = {}
    if merge_used:
        requirements["MultipleInputFeatureRequirement"] = {}
    if requirements:
        doc["requirements"] = requirements
    doc["hints"] = {"DockerRequirement": {"dockerPull": _image(image)}, "ResourceRequirement": dict(DEFAULT_RESOURCES)}
    doc.update(inputs=wf_inputs, outputs=wf_outputs, steps=steps)

    # conditional edges: curate:run_if (one condition per step), which implies the order
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
        hints = steps[sid[to]]["hints"]
        hints["curate:run_if"] = {"step": sid[frm], "result": result}
        rest = [s for s in (hints.get("curate:after") or {}).get("steps", []) if s != sid[frm]]
        if rest:
            hints["curate:after"] = {"steps": rest}
        else:
            hints.pop("curate:after", None)
    for n in nodes:
        if n.secrets:
            steps[sid[n.name]]["hints"]["curate:secrets"] = {"names": sorted(n.secrets)}
    return {"workflow": doc, "inputs": values, "functions": {f"functions/{n.name}.py": n.code for n in nodes},
            "warnings": warnings}


def content_hash(workflow: dict, functions: dict[str, str]) -> str:
    """sha256 over the normalized workflow (no label, doc, s: fields, curate:content_hash or input
    defaults) and the function files (by code path)."""
    doc = copy.deepcopy(workflow)
    for k in list(doc):
        if k in ("label", "doc", "curate:content_hash") or k.startswith("s:"):
            del doc[k]
    if isinstance(doc.get("$namespaces"), dict):  # the s: namespace only serves the identity fields
        doc["$namespaces"] = {k: v for k, v in doc["$namespaces"].items() if k != "s"}
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
