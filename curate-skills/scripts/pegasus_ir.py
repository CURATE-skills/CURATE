# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "pyyaml==6.0.2"]
# ///
"""A CURATE WorkflowSpec as a CURATE IR folder for the Pegasus target (ir.yaml + functions/).

  pegasus_ir.py build (--run RUN | --spec FILE) --out DIR [--name NAME] [--image URL]
                      [--deliver FILE[=PATH]]...

`build` writes a NEW folder (refused if DIR exists): ir.yaml, functions/<step>.py (the
approved code, unchanged) and an empty inputs/ folder for the workflow's input files, then
validates it with curate_ir/validate.py. Prints JSON {"ok", ...}; exit 0 ok, 1 the spec cannot
run on Pegasus (`problems`, one message each), 2 bad input.

The functions keep the FaaSr contract (IR `contract: faasr`): curate_ir/runner/curate_faasr_run.py
calls each one with the same Arguments FaaSr would (emit.faasr_arguments) and serves the FaaSr
API from the job's staged files.

Ranked steps become IR v0.2 fan-out:
- a step with rank N fans out over axis `rankN` (values "1".."N");
- a `{rank}` file gets the axis of its ranked writer, or else of the ranked steps that read it
  one copy per rank; an unranked step that writes it lists every copy as its own output;
- a reader that substitutes its own rank into that input (`input1.replace("{rank}", …)` or
  `.format(rank=…)`) reads one copy per rank and is paired copy by copy (same axis); a ranked
  reader that lists the folder instead (`faasr_get_folder_list`) reads all copies and gets them
  as a list through the alias axis `rankN_all`; an unranked reader always gets the list;
- an edge with no file between its steps gets an empty marker file, written by the runner, that
  the later step waits for (all copies of the earlier step).
Refused, each with its own message: conditional edges, N→M ranked handoffs read per rank,
readers whose code shows neither pattern, secrets, steps without code.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from curate_models import WorkflowSpec, clean_dependencies  # noqa: E402
from emit import faasr_arguments  # noqa: E402

IR_DIR = SCRIPT_DIR.parent / "curate_ir"
DEFAULT_IMAGE = "docker:///python:3.12-slim"  # as doctor.DEFAULT_PEGASUS_IMAGE
DEFAULT_RESOURCES = {"cpus": 1, "memory_mb": 2048}
RANK = "{rank}"
MARKER_KEY = "curate_done"  # runner/curate_faasr_run.py writes outputs whose key starts with this
NODE_ID = re.compile(r"^[a-z0-9_]+$")


class MappingError(Exception):
    """The spec cannot be expressed as a Pegasus IR; `problems` holds one message each."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def axis(count: int) -> str:
    return f"rank{count}"


def all_axis(count: int) -> str:
    return f"rank{count}_all"


def ir_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "workflow"


def _with_axis(f: str, ax: str) -> str:
    return f.replace(RANK, "{" + ax + "}")


def read_mode(code: str, i: int) -> str | None:
    """How a ranked function reads input<i>: "rank" (its own rank's copy), "all" (lists the
    folder and reads every copy), or None (the code shows neither)."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return None
    arg, per, lists = f"input{i}", False, False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == arg:
            if f.attr == "replace" and node.args and isinstance(node.args[0], ast.Constant) \
                    and node.args[0].value == RANK:
                per = True
            if f.attr == "format" and any(k.arg == "rank" for k in node.keywords):
                per = True
        if isinstance(f, ast.Name) and f.id == "faasr_get_folder_list":
            lists = True
    return "rank" if per else "all" if lists else None


def _parse_deliver(items) -> list[tuple[str, str | None]]:
    out = []
    for item in items or []:
        f, _, path = item.partition("=")
        out.append((f.strip(), path.strip() or None))
    return out


def build_ir(spec: WorkflowSpec, name: str | None = None, image: str = DEFAULT_IMAGE,
             deliverables=None) -> dict:
    """{"ir", "functions": {relpath: code}, "inputs": [file names to provide], "warnings"}.
    Raises MappingError with every problem found."""
    problems, warnings = [], []
    nodes = spec.nodes
    by_name = {n.name: n for n in nodes}
    rank = {n.name: max(1, int(n.rank or 1)) for n in nodes}
    ids: dict[str, str] = {}
    for n in nodes:
        nid = n.name.lower()
        if not NODE_ID.match(nid) or "__" in nid:
            problems.append(f"step {n.name}: Pegasus needs a name of letters, digits and single underscores")
        elif nid in ids.values():
            problems.append(f"step {n.name}: two steps have the name {nid} once lower-cased")
        ids[n.name] = nid
        if not n.code:
            problems.append(f"step {n.name} has no code; run curate-generate first")
        if n.secrets:
            problems.append(f"step {n.name} reads secrets ({', '.join(n.secrets)}): the Pegasus target "
                            "cannot pass secrets yet; deploy to FaaSr instead")
        deps = clean_dependencies(n.dependencies)
        if deps:
            warnings.append(f"step {n.name} imports {', '.join(deps)}: these must already be in the image "
                            f"{image}; nothing is installed at run time")
    for e in spec.edges:
        if e.condition is not None:
            problems.append(
                f"conditional edge {e.from_node} → {e.to_node} (taken only when {e.from_node} returns "
                f"{e.condition}): Pegasus runs a fixed DAG in which every step runs, so it cannot honour "
                "the condition. Remove the condition with a Gate 4 change request, or deploy to FaaSr.")

    producers: dict[str, str] = {}
    consumers: dict[str, list[tuple[str, int]]] = {}
    for n in nodes:
        for o in n.outputs:
            if o.name in producers:
                problems.append(f"file {o.name} is written by both {producers[o.name]} and {n.name}")
            producers.setdefault(o.name, n.name)
        for i, inp in enumerate(n.inputs, 1):
            consumers.setdefault(inp.name, []).append((n.name, i))

    mode: dict[tuple[str, int], str | None] = {}
    file_axis: dict[str, int] = {}  # {rank} file -> number of copies
    for f in sorted(set(producers) | set(consumers)):
        p = producers.get(f)
        readers = consumers.get(f, [])
        if RANK not in f:
            if p and rank[p] > 1:
                problems.append(f"ranked step {p} (×{rank[p]}) writes {f} without {RANK} in its name: its "
                                "ranks would overwrite each other")
            continue
        for c, i in readers:
            if rank[c] > 1:
                mode[(c, i)] = read_mode(by_name[c].code, i)
        per = sorted({rank[c] for c, i in readers if rank[c] > 1 and mode[(c, i)] == "rank"})
        if p and rank[p] > 1:
            count = rank[p]
        elif len(per) == 1:
            count = per[0]
        elif per:
            problems.append(f"file {f} is read one copy per rank by ranked steps with different counts "
                            f"({', '.join(map(str, per))}): one set of copies cannot pair with both")
            continue
        else:
            problems.append(f"file {f} has {RANK} in its name, but no ranked step writes it or reads it one "
                            "copy per rank, so its number of copies is unknown")
            continue
        file_axis[f] = count
        source = f"ranked step {p} (×{count})" if p and rank[p] > 1 else (p or "the workflow input")
        for c, i in readers:
            if rank[c] <= 1:
                continue
            m = mode[(c, i)]
            if m is None:
                problems.append(
                    f"ranked step {c} (×{rank[c]}) reads {f} as input{i}, but its code neither substitutes "
                    f"its own rank into input{i} (one copy per rank) nor lists the folder with "
                    "faasr_get_folder_list (all copies), so CURATE cannot tell how to pair its copies "
                    f"on Pegasus. Use input{i}.replace(\"{RANK}\", str(rank)) or faasr_get_folder_list.")
            elif m == "rank" and rank[c] != count:
                problems.append(
                    f"N→M ranked handoff: {source} provides {count} copies of {f}, but ranked step {c} "
                    f"runs {rank[c]} ranks and reads one copy per rank. Pegasus cannot pair them; put an "
                    f"unranked step between them that reads all copies and writes {rank[c]} (like a shuffle).")

    deliver = _parse_deliver(deliverables)
    for f, _ in deliver:
        if f not in producers:
            problems.append(f"deliverable {f} is not written by any step")
    if problems:
        raise MappingError(problems)

    def ir_file(f: str) -> str:
        return _with_axis(f, axis(file_axis[f])) if f in file_axis else f

    # Steps linked by an edge but by no file: a marker file keeps the order.
    markers: dict[str, str] = {}
    waits: dict[str, list[str]] = {}
    for e in spec.edges:
        a, b = e.from_node, e.to_node
        if {o.name for o in by_name[a].outputs} & {i.name for i in by_name[b].inputs}:
            continue
        m = f"{MARKER_KEY}__{ids[a]}" + ("_" + RANK if rank[a] > 1 else "")
        markers[a] = m
        if rank[a] > 1:
            file_axis[m] = rank[a]
        waits.setdefault(b, []).append(a)

    alias: set[int] = set()
    ir_nodes = []
    for n in nodes:
        r = rank[n.name]
        ins, outs = {}, {}
        for i, inp in enumerate(n.inputs, 1):
            f = inp.name
            if f in file_axis and r > 1 and mode.get((n.name, i)) == "all":
                ins[f"input{i}"] = _with_axis(f, all_axis(file_axis[f]))
                alias.add(file_axis[f])
            else:
                ins[f"input{i}"] = ir_file(f)  # same axis: paired; unranked reader: a list
        for a in waits.get(n.name, []):
            m = markers[a]
            if m in file_axis and r == file_axis[m]:
                ins[f"{MARKER_KEY}_after_{ids[a]}"] = _with_axis(m, all_axis(file_axis[m]))  # wait for all
                alias.add(file_axis[m])
            else:
                ins[f"{MARKER_KEY}_after_{ids[a]}"] = ir_file(m)
        for i, out in enumerate(n.outputs, 1):
            f = out.name
            if f in file_axis and r <= 1:  # an unranked step writes every copy itself
                for v in range(1, file_axis[f] + 1):
                    outs[f"output{i}_{v}"] = f.replace(RANK, str(v))
            else:
                outs[f"output{i}"] = ir_file(f)
        if n.name in markers:
            outs[MARKER_KEY] = ir_file(markers[n.name])
        node = {"id": ids[n.name], "contract": "faasr", "code": f"functions/{n.name}.py",
                "entrypoint": n.name}
        if r > 1:
            node["for_each"] = [axis(r)]
        node.update(inputs=ins, outputs=outs,
                    params={"faasr": {"args": faasr_arguments(spec, n), "rank_axis": axis(r) if r > 1 else None,
                                      "max_rank": r}})
        deps = clean_dependencies(n.dependencies)
        if deps:
            node["packages"] = deps
        ir_nodes.append(node)

    counts = sorted({r for r in rank.values() if r > 1} | set(file_axis.values()))
    axes = {axis(c): [str(v) for v in range(1, c + 1)] for c in counts}
    axes.update({all_axis(c): [str(v) for v in range(1, c + 1)] for c in sorted(alias)})

    external = sorted(f for f in consumers if f not in producers)
    inputs = [{"file": ir_file(f), "location": "inputs/" + ir_file(f)} for f in external]
    needed = [f.replace(RANK, str(v)) for f in external for v in range(1, file_axis.get(f, 1) + 1)] \
        if external else []
    needed = sorted(set(needed))

    if deliver:
        chosen = deliver
    else:  # end files: written by a step, read by none
        chosen = [(f, None) for f in sorted(producers) if f not in consumers]
    outputs = [{"file": ir_file(f), "deliver_as": _with_axis(path, axis(file_axis[f])) if path and f in file_axis
                else path or "results/" + ir_file(f)} for f, path in chosen]

    ir = {"ir_version": "0.2", "name": ir_name(name or spec.name),
          "description": " ".join((spec.description or spec.name).split())[:200],
          "defaults": {"image": image, "resources": dict(DEFAULT_RESOURCES)}}
    if axes:
        ir["axes"] = axes
    ir.update(inputs=inputs, nodes=ir_nodes, outputs=outputs)
    functions = {f"functions/{n.name}.py": n.code for n in nodes}
    return {"ir": ir, "functions": functions, "inputs": needed, "warnings": warnings}


def write_folder(out: Path, built: dict) -> Path:
    """Write a new IR folder; an existing one is never overwritten."""
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} already exists; nothing was written")
    (out / "functions").mkdir(parents=True)
    (out / "inputs").mkdir()
    (out / "ir.yaml").write_text(yaml.safe_dump(built["ir"], sort_keys=False, default_flow_style=False,
                                                allow_unicode=True))
    for rel, code in built["functions"].items():
        (out / rel).write_text(code)
    return out


def validate_folder(out: Path) -> dict:
    """curate_ir/validate.py on the folder: {"errors", "warnings", "jobs"}."""
    sys.path.insert(0, str(IR_DIR))
    import validate as V
    exp, errors, warnings, _ = V.prepare(V.load(Path(out) / "ir.yaml"))
    return {"errors": errors, "warnings": warnings, "jobs": len(exp.get("nodes", []))}


def load_spec(args) -> WorkflowSpec:
    if args.spec:
        data = json.loads(Path(args.spec).read_text())
    else:
        state = json.loads((Path(args.run) / "state.json").read_text())
        data = state.get("spec")
        if not data:
            raise ValueError("the run has no approved spec yet")
    return WorkflowSpec.model_validate(data)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="CURATE spec -> CURATE IR folder for the Pegasus target")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    src = b.add_mutually_exclusive_group(required=True)
    src.add_argument("--run")
    src.add_argument("--spec", help="a WorkflowSpec JSON file")
    b.add_argument("--out", required=True)
    b.add_argument("--name")
    b.add_argument("--image", default=DEFAULT_IMAGE)
    b.add_argument("--deliver", action="append", help="FILE or FILE=PATH under deliver/ (repeatable); "
                   "replaces the default, every end file under results/")
    a = p.parse_args(argv)
    try:
        spec = load_spec(a)
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    try:
        built = build_ir(spec, name=a.name, image=a.image, deliverables=a.deliver)
    except MappingError as e:
        print(json.dumps({"ok": False, "problems": e.problems}, indent=2))
        return 1
    try:
        out = write_folder(Path(a.out), built)
    except FileExistsError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    v = validate_folder(out)
    print(json.dumps({"ok": not v["errors"], "out": str(out), "jobs": v["jobs"], "inputs_needed": built["inputs"],
                      "warnings": built["warnings"], "validation": v}, indent=2))
    return 0 if not v["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
