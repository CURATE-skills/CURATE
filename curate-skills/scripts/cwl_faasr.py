# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "cwl-utils==0.47", "pyyaml==6.0.2"]
# ///
"""The FaaSr builder: a CURATE CWL workflow (docs/CWL_IR.md) -> a FaaSr bundle.

  cwl_faasr.py build workflow.yml [--inputs inputs.yml] --out DIR --name NAME --folder F
               [--compute-target T] [--data-store S]

The bundle has the same files as emit.write_bundle's: workflow.json, functions/<step>.py (the
generated wrapper, faasr_wrapper.render_step), functions/<step>_main.py (the function,
byte-identical), functions/<step>.deps.txt, external_inputs.json and README.md, plus
outputs.json: every S3 object the run writes ({action, file}) and the deliverables
({file, deliver_as}), which `monitor.py download-outputs` reads instead of working names out.

It reads `scatter` directly: one ranked action per step (`InvokeNext: step(N)`), the rank
filling `{rank}` in that step's file names and indexing its per-rank params. File names are
generated: `<step>__<output>[__{rank}]__<curate:name>` for step outputs (the spec's file name, its
axis placeholders turned into `{rank}`, so the extension a function may rely on is kept), the
location's name for a workflow input file, `<input>__{rank}<ext>` for a workflow input list; so a folder input's files are
exactly those under its own prefix (`<step>__<output>__`), and any other name starting with that
prefix is refused. curate:after becomes InvokeNext; curate:run_if a conditional InvokeNext
({"True": [...]}); curate:secrets the workflow's Secrets (names only).

Refused, one message each: a step scattered over more than one axis, a ranked step writing a
list, a gather over two axes, a folder from several sources, more than one first step, code that
is not a plain function.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "curate_ir"))
import emit  # noqa: E402
import faasr_wrapper as W  # noqa: E402
import plain_contract as PC  # noqa: E402

RANK = "{rank}"


class BuildError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _location(v: dict) -> str:
    return "file://" + v["path"] if v.get("path") else v.get("location", "")


def _ext(locations) -> str:
    exts = {Path(x).suffix for x in locations}
    return exts.pop() if len(exts) == 1 else ""


def build(wf, name: str, folder: str, compute_target: str = "My_GitHub_Account",
          data_store: str = "S3", level: str = "structural") -> dict:
    """{"workflow", "files": {path in the bundle: text}, "external_inputs", "outputs", "validation"}.
    `wf` is a cwl.load.Workflow that cwl/validate.py accepted. Raises BuildError."""
    from cwl.expand_cwl import _order

    problems = []
    steps = wf.steps
    order = _order(wf)
    axis_inputs = {n for n, i in wf.inputs.items() if i.type in ("int[]", "string[]")}

    # names (patterns with {rank} when there is one file per rank) and how many copies they have
    names: dict[str, str] = {}
    count: dict[str, int] = {}
    file_inputs = [(n, i) for n, i in wf.inputs.items() if i.type.startswith("File")]
    basenames = [Path(_location(i.value)).name for _, i in file_inputs if i.type == "File"]
    external = []
    for n, i in file_inputs:
        if i.type == "File":
            base = Path(_location(i.value)).name
            names[n] = base if basenames.count(base) == 1 else f"{n}__{base}"
            external.append({"input": n, "file": names[n], "location": _location(i.value)})
        elif i.type == "File[]":
            locs = [_location(v) for v in i.value]
            names[n] = f"{n}__{RANK}{_ext(locs)}"
            count[n] = len(locs)
            external += [{"input": n, "file": names[n].replace(RANK, str(k)), "location": loc}
                         for k, loc in enumerate(locs, 1)]
        else:
            problems.append(f"workflow input {n} is {i.type}; FaaSr takes a file or a list of files")

    def length(src):
        if "/" not in src:
            v = wf.inputs[src].value
            return len(v) if isinstance(v, list) else None
        return count.get(src)

    copies = {}
    for sid in order:
        s = steps[sid]
        where = f"step {sid}"
        if s.scatter:
            axes = {s.ins[e].sources[0] for e in s.scatter if s.ins[e].sources and s.ins[e].sources[0] in axis_inputs}
            if (s.scatter_method or "dotproduct") != "dotproduct" and len(s.scatter) > 1 or len(axes) > 1:
                problems.append(f"{where} scatters over several axes ({', '.join(s.scatter)}); FaaSr runs one "
                                "ranked action per step, over one axis")
            copies[sid] = length(s.ins[s.scatter[0]].sources[0]) or 1
        else:
            copies[sid] = 1
        own_axes = {x for e in s.scatter for x in (s.ins[e].sources if e in s.ins else []) if x in axis_inputs}
        for out, port in s.op_outputs.items():
            src = f"{sid}/{out}"
            if port.axis and copies[sid] > 1:
                problems.append(f"{where} is ranked and writes a list ({out}); FaaSr names one file per rank")
            # the spec's file name, its axis placeholders as {rank} (FaaSr's wrapper fills {rank})
            tail = ("__" + re.sub(r"\{([A-Za-z0-9_]+)\}",
                                  lambda m: RANK if m.group(1) in own_axes | {port.axis} else m.group(0), port.name)
                    if port.name else "")
            if port.axis:
                names[src], count[src] = f"{sid}__{out}__{RANK}{tail}", len(wf.inputs[port.axis].value)
            elif copies[sid] > 1:
                names[src], count[src] = f"{sid}__{out}__{RANK}{tail}", copies[sid]
            else:
                names[src] = f"{sid}__{out}{tail}"

    def concrete(src):
        p = names[src]
        return [p.replace(RANK, str(k)) for k in range(1, count[src] + 1)] if RANK in p else [p]

    # each step's Arguments, wrapper tables and successors
    action_name = {sid: emit._to_action_name(sid) for sid in steps}
    action_list, repos, files, pypi = {}, {}, {}, {}
    all_names = {src: concrete(src) for src in names}
    secrets = sorted({x for s in steps.values() for x in s.secrets})
    for sid in order:
        s = steps[sid]
        where = f"step {sid}"
        ranked = copies[sid] > 1
        args = {"folder": folder}
        ins, outs, params = [], [], []
        for slot, port in s.op_inputs.items():
            e = s.ins[slot]
            if not port.type.startswith("File"):
                if slot in s.scatter:
                    vals = wf.inputs[e.sources[0]].value
                    params.append((slot, None, list(vals)) if ranked else (slot, vals[0], None))
                elif e.sources:
                    params.append((slot, wf.inputs[e.sources[0]].value, None))
                else:
                    params.append((slot, e.default, None))
                continue
            if port.type.count("[]") > 1:
                problems.append(f"{where}: input {slot} gathers over several axes ({port.type}); FaaSr has one rank")
                continue
            if e.folder:
                if len(e.sources) != 1:
                    problems.append(f"{where}: folder input {slot} gathers several sources; on FaaSr a folder input "
                                    "lists one prefix")
                    continue
                src = e.sources[0]
                args[slot] = names[src]
                prefix = names[src].split(RANK, 1)[0]
                mine = set(all_names[src])
                others = sorted(f for k, fs in all_names.items() if k != src for f in fs
                                if f.startswith(prefix) and f not in mine)
                if others:
                    problems.append(f"{where}: folder input {slot} lists every object starting with {prefix!r}, "
                                    f"which would also hold {', '.join(others)}")
                ins.append((slot, "folder", None))
                continue
            src = e.sources[0]
            args[slot] = names[src]
            if slot in s.scatter:
                ins.append((slot, "one", None))  # this rank's copy: {rank} filled by the wrapper
            elif port.type == "File[]":
                ins.append((slot, "list", count.get(src, 1)))
            else:
                ins.append((slot, "one", None))
        for slot, port in s.op_outputs.items():
            src = f"{sid}/{slot}"
            args[slot] = names[src]
            outs.append((slot, "list", count[src]) if port.axis else (slot, "one", None))

        fn = s.function or {}
        code_path = wf.base / str(fn.get("code") or "")
        code = code_path.read_text() if code_path.is_file() else ""
        if not PC.is_plain(code):
            problems.append(f"{where} is not a plain main(inputs, outputs, params) function")
        else:
            problems += W.code_problems(sid, code, set(steps))
        files[f"functions/{W.main_file(sid)}"] = code
        files[f"functions/{sid}.py"] = W.render_step(sid, code, ranked, ins, outs, params)
        files[f"functions/{sid}.deps.txt"] = "".join(f"{d}\n" for d in s.packages)
        if s.packages:
            pypi[sid] = list(s.packages)
        repos[sid] = emit.function_repo_paths(sid)
        action_list[action_name[sid]] = emit.faasr_action(compute_target, sid, args, [])

    # InvokeNext: data, curate:after and curate:run_if (conditional)
    def target(t):
        return f"{action_name[t]}({copies[t]})" if copies[t] > 1 else action_name[t]

    feeds = {sid: set() for sid in steps}
    for t, s in steps.items():
        cond = s.run_if["step"] if s.run_if else None
        for e in s.ins.values():
            for src in e.sources:
                if "/" in src and src.split("/", 1)[0] != cond:
                    feeds[src.split("/", 1)[0]].add(t)
        for a in s.after:
            if a != cond:
                feeds[a].add(t)
    for sid in order:
        nxt = [target(t) for t in order if t in feeds[sid]]
        branches = {}
        for t in order:
            ri = steps[t].run_if
            if ri and ri["step"] == sid:
                branches.setdefault("True" if ri["result"] else "False", []).append(target(t))
        if branches:
            nxt.append(branches)
        action_list[action_name[sid]]["InvokeNext"] = nxt
    first = [sid for sid in order if not any(sid in feeds[x] for x in steps)
             and not any((steps[sid].run_if or {}).get("step") == x for x in steps)]
    if len(first) != 1:
        problems.append(f"FaaSr starts one action; the workflow has {len(first)} first steps ({', '.join(first)})")

    if problems:
        raise BuildError(list(dict.fromkeys(problems)))

    doc = emit.faasr_document(name, compute_target, data_store, action_list, repos,
                              action_name[first[0]], pypi, secrets)
    written = [{"action": action_name[sid], "file": f} for sid in order for out in steps[sid].op_outputs
               for f in all_names[f"{sid}/{out}"]]
    deliverables = []
    for oid, o in wf.outputs.items():
        src = o.sources[0]
        sid, out = src.split("/", 1)
        port = steps[sid].op_outputs[out]
        axis = port.axis or next((x for e in steps[sid].scatter for x in steps[sid].ins[e].sources
                                  if x in axis_inputs), None)
        for k, f in enumerate(all_names[src]):
            deliver = o.deliver_as or f"results/{oid}"
            if axis and "{" in deliver:
                deliver = deliver.replace("{%s}" % axis, str(wf.inputs[axis].value[k]))
            deliverables.append({"output": oid, "file": f, "deliver_as": deliver})
    ext = [{**e, "s3_path": f"{folder}/{e['file']}"} for e in external]
    return {"workflow": doc, "files": files, "external_inputs": ext,
            "outputs": {"files": written, "deliverables": deliverables},
            "validation": emit.validate(doc, level), "secret_names": secrets, "names": names}


def write(built: dict, out) -> Path:
    """A NEW bundle folder; nothing is written if a secret value would land in it."""
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} already exists; nothing was written")
    wf = built["workflow"]
    texts = {"workflow.json": json.dumps(wf, indent=2) + "\n",
             "external_inputs.json": json.dumps(built["external_inputs"], indent=2) + "\n",
             "outputs.json": json.dumps(built["outputs"], indent=2) + "\n",
             "README.md": _readme(built), **built["files"]}
    leaked = sorted({n for t in texts.values() for n in emit.find_leaked_secrets(t, built["secret_names"])})
    if leaked:
        raise BuildError([f"secret values found in bundle content ({', '.join(leaked)}); nothing written"])
    for rel, text in texts.items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text)
    return out


def _readme(built: dict) -> str:
    wf = built["workflow"]
    lines = [f"# {wf['WorkflowName']}", "", "Built by curate-skills from workflow.yml (cwl_faasr.py). Not deployed.",
             "", "## Validation", "", f"- level: `{built['validation']['level']}`",
             f"- ok: `{built['validation']['ok']}`", "", "## Actions secrets the repo needs (names only)", ""]
    lines += [f"- `{s}`" for s in built["secret_names"]] or ["- none"]
    lines += ["", "## Input files you must provide in S3 (none = nothing to do)", ""]
    lines += [f"- `{e['s3_path']}` (tested with {e['location']})" for e in built["external_inputs"]] or ["- none"]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    from cwl.validate import validate
    ap = argparse.ArgumentParser(description="CURATE CWL workflow -> FaaSr bundle")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("workflow")
    b.add_argument("--inputs")
    b.add_argument("--out", required=True)
    b.add_argument("--name", required=True)
    b.add_argument("--folder", required=True)
    b.add_argument("--compute-target", default="My_GitHub_Account")
    b.add_argument("--data-store", default="S3")
    a = ap.parse_args(argv)
    wf, errors, _ = validate(a.workflow, a.inputs)
    if errors:
        print(json.dumps({"ok": False, "problems": errors}, indent=2))
        return 1
    try:
        built = build(wf, a.name, a.folder, a.compute_target, a.data_store)
        out = write(built, a.out)
    except BuildError as e:
        print(json.dumps({"ok": False, "problems": e.problems}, indent=2))
        return 1
    except FileExistsError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps({"ok": built["validation"]["ok"], "bundle_dir": str(out),
                      "external_inputs": built["external_inputs"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
