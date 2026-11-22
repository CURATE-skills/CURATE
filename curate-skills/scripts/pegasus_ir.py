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

A step whose code follows the plain contract (scripts/plain_contract.py) becomes a plain IR node:
the IR runner (curate_ir/runner/curate_run.py) calls main(inputs, outputs, params) on the files
Pegasus stages, with slots input1.., output1.. and params {rank, max_rank} on a ranked step.
FaaSr-style code (until it is migrated) keeps IR `contract: faasr`:
curate_ir/runner/curate_faasr_run.py calls it with the same Arguments FaaSr would
(emit.faasr_arguments) and serves the FaaSr API from the job's staged files.

Ranked steps become IR v0.2 fan-out:
- a step with rank N fans out over axis `rankN` (values "1".."N");
- a `{rank}` file gets the axis of its ranked writer, or else of the ranked steps that read it
  one copy per rank; an unranked step that writes it writes every copy (plain: one list
  output; FaaSr-style: one output per copy);
- a ranked reader that reads its own copy (plain: the default; FaaSr-style: its code
  substitutes its rank, `input1.replace("{rank}", …)` or `.format(rank=…)`) is paired copy by
  copy (same axis); one that reads all copies (plain: the input says read: all; FaaSr-style:
  `faasr_get_folder_list`) gets them as a list through the alias axis `rankN_all`; an unranked
  reader always gets the list;
- a plain folder input gets every workflow file whose name starts with its prefix, as one
  directory (IR `{folder: [...]}`);
- an edge with no file between its steps gets an empty marker file, written by the runner, that
  the later step waits for (all copies of the earlier step).
Refused, each with its own message: conditional edges, N→M ranked handoffs read per rank,
FaaSr-style readers whose code shows neither pattern, folder inputs that match no file,
secrets, steps without code.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import envfile  # noqa: E402
import plain_contract as PC  # noqa: E402
from curate_models import WorkflowSpec, clean_dependencies  # noqa: E402
from emit import faasr_arguments  # noqa: E402

IR_DIR = SCRIPT_DIR.parent / "curate_ir"
DEFAULT_IMAGE = envfile.DEFAULT_PEGASUS_IMAGE
RUN_TS_RX = re.compile(r"^\d{4}(\d{2})(\d{2})-(\d{2})(\d{2})")  # run ids: YYYYMMDD-HHMMSS-<name>
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

    plain = {n.name: PC.is_plain(n.code) for n in nodes}
    producers: dict[str, str] = {}
    consumers: dict[str, list[tuple[str, int]]] = {}
    folders: dict[tuple[str, int], list[str]] = {}  # plain folder input -> the files it holds
    for n in nodes:
        for o in n.outputs:
            if o.name in producers:
                problems.append(f"file {o.name} is written by both {producers[o.name]} and {n.name}")
            producers.setdefault(o.name, n.name)
        for i, inp in enumerate(n.inputs, 1):
            if inp.kind == "folder" and plain[n.name]:
                folders[(n.name, i)] = PC.folder_matches(spec, n.name, inp.name)
                if not folders[(n.name, i)]:
                    problems.append(
                        f"step {n.name} reads the folder input {inp.name} (every file starting with "
                        f"{PC.prefix(inp.name)!r}), but no step writes or reads such a file: on Pegasus the "
                        "files must be known when the workflow is built")
            else:
                consumers.setdefault(inp.name, []).append((n.name, i))

    def read_of(c: str, i: int) -> str | None:
        if plain[c]:
            return PC.plain_read(spec, c, i)
        return {"rank": "own", "all": "all"}.get(read_mode(by_name[c].code, i))

    file_axis, mode, counted = PC.family_counts(spec, read_of)  # {rank} file -> number of copies
    problems += counted

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
    def reads(b: str) -> set[str]:
        got = {inp.name for i, inp in enumerate(by_name[b].inputs, 1) if (b, i) not in folders}
        return got | {f for (c, _), fs in folders.items() if c == b for f in fs}

    for e in spec.edges:
        a, b = e.from_node, e.to_node
        if {o.name for o in by_name[a].outputs} & reads(b):
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
            if (n.name, i) in folders:  # every matching file, every copy: one directory
                pats = []
                for g in folders[(n.name, i)]:
                    if g in file_axis and r > 1:
                        pats.append(_with_axis(g, all_axis(file_axis[g])))
                        alias.add(file_axis[g])
                    else:
                        pats.append(ir_file(g))
                ins[f"input{i}"] = {"folder": pats}
            elif f in file_axis and r > 1 and mode.get((n.name, i)) == "all":
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
            if f in file_axis and r <= 1 and not plain[n.name]:  # FaaSr-style: one output per copy
                for v in range(1, file_axis[f] + 1):
                    outs[f"output{i}_{v}"] = f.replace(RANK, str(v))
            else:  # plain and unranked: the family's axis is not the node's, so a list of every copy
                outs[f"output{i}"] = ir_file(f)
        if n.name in markers:
            outs[MARKER_KEY] = ir_file(markers[n.name])
        if plain[n.name]:
            node = {"id": ids[n.name], "code": f"functions/{n.name}.py"}
            params = {"rank": "{" + axis(r) + "}", "max_rank": r} if r > 1 else {}
        else:
            node = {"id": ids[n.name], "contract": "faasr", "code": f"functions/{n.name}.py",
                    "entrypoint": n.name}
            params = {"faasr": {"args": faasr_arguments(spec, n), "rank_axis": axis(r) if r > 1 else None,
                                "max_rank": r}}
        if r > 1:
            node["for_each"] = [axis(r)]
        node.update(inputs=ins, outputs=outs, params=params)
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
    else:  # end files: written by a step, read by none (folder inputs read what they hold)
        read = set(consumers) | {f for fs in folders.values() for f in fs}
        chosen = [(f, None) for f in sorted(producers) if f not in read]
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
    import yaml  # only here: spec.py builds the Gate 4 preview without PyYAML

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


# ── Gate 4: the Pegasus target, read-only ────────────────────────────────────

def run_ir_name(spec_name: str, run_id: str) -> str:
    """The IR (Pegasus workflow) name for a run: <workflow>-<MMDD-HHMM> of the run, the same
    timestamp rule as the FaaSr deploy name's default suffix."""
    m = RUN_TS_RX.match(run_id or "")
    return ir_name(spec_name) + (f"-{m[1]}{m[2]}-{m[3]}{m[4]}" if m else "")


def settings(env=None) -> dict:
    """CURATE_PEGASUS_* from the environment, else ~/.config/curate/.env (never modifies os.environ)."""
    env = dict(os.environ if env is None else env)
    envfile.load(env, names=envfile.PEGASUS_NAMES)
    return envfile.pegasus_settings(env)


IMAGE_SOURCE_SETTING = "CURATE_PEGASUS_IMAGE"
IMAGE_SOURCE_DEFAULT = "the built-in default"


def effective(s: dict, current: dict | None = None) -> dict:
    """The run's Pegasus settings: pinned by prepare; before that, the current settings with the
    run's own image (chosen at Gate 4 or "Start this run?") if it has one. Adds `image_source`."""
    rec = s.get("pegasus") or {}
    if rec.get("runtime"):  # pinned
        return {"host": rec.get("host"), "runtime": rec["runtime"], "image": rec["image"],
                "image_source": rec.get("image_source") or IMAGE_SOURCE_SETTING}
    st = dict(current or settings())
    if rec.get("image_choice"):
        st.update(image=rec["image_choice"], image_source=rec.get("image_source") or "this run only")
    else:
        explicit = settings_image_set()
        st["image_source"] = IMAGE_SOURCE_SETTING if explicit else IMAGE_SOURCE_DEFAULT
    return st


def settings_image_set(env=None) -> bool:
    env = dict(os.environ if env is None else env)
    envfile.load(env, names=envfile.PEGASUS_NAMES)
    return bool(str(env.get("CURATE_PEGASUS_IMAGE", "")).strip())


def target_label(st: dict) -> str:
    return st["host"] or "this machine"


DIFFERENT_IMAGE_OPTION = "Approve & run on Pegasus with a different image…"


def option_label(st: dict) -> str:
    """Gate 4's Pegasus option, naming where it would run and the image it would use."""
    return f"Approve & run on Pegasus ({target_label(st)} · {st['image']})"


def plan(s: dict, st: dict) -> dict:
    """What the Pegasus target would do for this run, without writing anything: the IR name,
    job count, inputs and deliverables; or the problems that make it impossible."""
    spec = WorkflowSpec.model_validate(s["spec"])
    name = run_ir_name(spec.name, s.get("run_id", ""))
    try:
        built = build_ir(spec, name=name, image=st["image"])
    except MappingError as e:
        return {"ok": False, "name": name, "problems": e.problems}
    sys.path.insert(0, str(IR_DIR))
    import expand
    exp, errors = expand.expand(built["ir"])
    if errors:
        return {"ok": False, "name": name, "problems": [f"IR expansion: {e}" for e in errors]}
    return {"ok": True, "name": name, "jobs": len(exp["nodes"]), "functions": len(built["functions"]),
            "inputs": built["inputs"], "deliverables": [o["deliver_as"] for o in built["ir"]["outputs"]],
            "deliverable_files": len(exp.get("outputs") or []), "warnings": built["warnings"]}


def summary_lines(s: dict, st: dict, p: dict | None = None) -> list[str]:
    """The short Pegasus summary: Gate 4's "Where it can run", and the image check before a run
    with a different image. No file lists (those are in plan_lines, for the full review)."""
    where = f"`{st['host']}` (over ssh)" if st["host"] else "this machine"
    p = p or plan(s, st)
    if not p["ok"]:
        return [f"**Pegasus** on {where}: **cannot run on Pegasus:**"] + [f"- {x}" for x in p["problems"]]
    src = st.get("image_source")
    n = p["deliverable_files"]
    out = [f"**Pegasus** on {where}: `{p['name']}`, {p['jobs']} jobs, image `{st['image']}`"
           + (f" ({src})" if src else "") + f", runtime {st['runtime']}, {n} deliverable{'s' if n != 1 else ''}",
           "- input files you must provide: " + (f"{len(p['inputs'])} (listed in the full review)"
                                                  if p["inputs"] else "none")]
    out += [f"- note: {w}" for w in p["warnings"]]
    return out


def plan_lines(s: dict, st: dict) -> list[str]:
    """The full "Pegasus run would write" lines, for the Gate 4 review file."""
    run_dir = s.get("run_dir") or "<run>"
    local = f"{run_dir}/pegasus"
    out = [f"If you choose **{option_label(st)}** (nothing is written now; the run asks before each step):", ""]
    if st["host"]:
        out.append(f"- Runs on: `{st['host']}` over ssh; `{local}/` is copied to "
                   f"`{st['host']}:~/curate-runs/{s.get('run_id')}/` and run there")
    else:
        out.append(f"- Runs on: this machine (Pegasus + HTCondor), in `{local}/`")
    p = plan(s, st)
    if not p["ok"]:
        out.append("- **Cannot run on Pegasus:**")
        out += [f"  - {x}" for x in p["problems"]]
        return out
    out += [f"- Workflow: `{p['name']}`, {p['jobs']} jobs, image `{st['image']}`, runtime {st['runtime']}",
            f"- Writes: `{local}/ir.yaml`, `{local}/functions/` ({p['functions']} files), `{local}/inputs/`",
            "- Input files you must provide: " + (", ".join(f"`{f}`" for f in p["inputs"]) or "none"),
            "- Delivered: " + (", ".join(f"`{d}`" for d in p["deliverables"]) or "nothing")]
    out += [f"- Note: {w}" for w in p["warnings"]]
    return out


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
