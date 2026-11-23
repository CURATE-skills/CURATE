# /// script
# requires-python = ">=3.11"
# dependencies = ["cwl-utils==0.47", "pyyaml==6.0.2"]
# ///
"""Expand a CURATE CWL workflow's scatter into concrete jobs, in the shape curate_ir/expand.py
produces for IR v0.2, so the existing backends take it unchanged.

  expand_cwl.py workflow.yml [--inputs inputs.yml]     # prints the job list

  from cwl.expand_cwl import expand_cwl
  exp = expand_cwl(wf)      # wf from cwl/load.py, already checked by cwl/validate.py

The result is an expanded IR dict: name, description, defaults {image, resources}, inputs
[{file, location}], outputs [{file, deliver_as}], _base, and nodes, each with id (`<step>` or
`<step>__<axis values>`), _origin, _assign {axis: value}, code, entrypoint, inputs / outputs
(a file name, a list of names, or {"folder": [names]} for a curate:folder input), params, and
image / resources / packages when the step sets them. Control edges are a node's `after` (the
ids of every copy it waits for); `run_if` and `secrets` are carried for the builders to judge.

File names are made here, deterministically: a workflow input file keeps the name of its
location (prefixed with the input id if two would clash); a step output is
`<step>__<output>[__<values>]`, with `__<axis value>` added per file of a curate:axis list
(the output before the values, so every output's files share their own prefix), then
`__<curate:name>` with its placeholders filled for that file, so the spec's file name and its
extension are kept (`plot__output1__corvallis_winter_comparison.png`).
"""
from __future__ import annotations

import argparse
import itertools
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cwl.load import NO_VALUE, Workflow  # noqa: E402

PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")


@dataclass(frozen=True)
class Leaf:
    """One concrete file and the axis values it belongs to."""
    name: str
    assign: tuple  # ((axis, value), ...)


def _safe(value) -> str:
    return re.sub(r"[^a-z0-9_]", "_", str(value).lower())


def _leaves(v) -> list:
    if isinstance(v, list):
        return [x for e in v for x in _leaves(e)]
    return [v]


def _names(v):
    """A source value (a Leaf or nested lists of them) as expand.py gives it: one name, or one flat
    list of names in order."""
    return v.name if isinstance(v, Leaf) else [x.name for x in _leaves(v)]


def _image(url):
    if not url:
        return None
    return url if "://" in url else "docker:///" + url


def _resources(r: dict) -> dict:
    out = {}
    if r.get("coresMin") is not None:
        out["cpus"] = r["coresMin"]
    if r.get("ramMin") is not None:
        out["memory_mb"] = r["ramMin"]
    return out


def _location(value: dict) -> str:
    return "file://" + value["path"] if value.get("path") else value.get("location", "")


def _order(wf: Workflow) -> list[str]:
    deps = {sid: set() for sid in wf.steps}
    for sid, s in wf.steps.items():
        for e in s.ins.values():
            deps[sid].update(src.split("/", 1)[0] for src in e.sources if "/" in src)
        deps[sid].update(s.after)
        if s.run_if:
            deps[sid].add(s.run_if["step"])
    order, left = [], list(wf.steps)  # keep declaration order where the data allows
    while left:
        nxt = next(s for s in left if deps[s] <= set(order))
        order.append(nxt)
        left.remove(nxt)
    return order


def expand_cwl(wf: Workflow) -> dict:
    values: dict = {}       # source id -> Leaf | nested list of Leaf | plain value
    axis_inputs = {n for n, i in wf.inputs.items() if i.type in ("int[]", "string[]")}
    inputs_list = []

    # workflow inputs: files get flat names from their locations
    file_inputs = [(n, i) for n, i in wf.inputs.items() if i.type.startswith("File")]
    basenames = [Path(_location(v)).name for _, i in file_inputs
                 for v in (i.value if isinstance(i.value, list) else [i.value])]
    for n, i in wf.inputs.items():
        if not i.type.startswith("File"):
            values[n] = i.value
            continue
        vals = i.value if isinstance(i.value, list) else [i.value]
        leaves = []
        for v in vals:
            base = Path(_location(v)).name
            name = base if basenames.count(base) == 1 else f"{n}__{base}"
            leaves.append(Leaf(name, ()))
            inputs_list.append({"file": name, "location": _location(v)})
        values[n] = leaves if isinstance(i.value, list) else leaves[0]

    copies_of: dict[str, list[str]] = {}
    nodes = []
    for sid in _order(wf):
        s = wf.steps[sid]
        fn = s.function or {}
        # the value each in entry receives (before scatter)
        given = {}
        for name, e in s.ins.items():
            if not e.sources:
                given[name] = e.default
            elif len(e.sources) == 1 and e.link_merge != "merge_flattened":
                given[name] = values[e.sources[0]]
            else:  # merge_flattened (a curate:folder input)
                merged = []
                for src in e.sources:
                    v = values[src]
                    merged += v if isinstance(v, list) else [v]
                given[name] = merged
        # copies: (suffix values, assign, {in entry: element})
        if not s.scatter:
            copies = [((), (), {})]
        else:
            lists = [given[name] for name in s.scatter]
            method = s.scatter_method or "dotproduct"
            if method == "dotproduct":
                index_sets = [tuple(i for _ in s.scatter) for i in range(len(lists[0]))]
            else:
                index_sets = list(itertools.product(*(range(len(x)) for x in lists)))
            copies = []
            for idx in index_sets:
                picked = {name: lists[k][idx[k]] for k, name in enumerate(s.scatter)}
                assign, suffix = [], []
                for k, name in enumerate(s.scatter):
                    src = s.ins[name].sources
                    if len(src) == 1 and src[0] in axis_inputs:
                        assign.append((src[0], str(picked[name])))
                        suffix.append(picked[name])
                if not suffix:  # scattered over files only: number the copies
                    suffix = [i + 1 for i in idx[:1]] if method == "dotproduct" else [i + 1 for i in idx]
                copies.append((tuple(suffix), tuple(assign), picked))
        ids = []
        per_copy_out = []
        for suffix, assign, picked in copies:
            nid = sid + "".join("__" + _safe(v) for v in suffix)
            ids.append(nid)
            vals = "".join("__" + _safe(v) for v in suffix)
            ins, params, outs, out_vals = {}, {}, {}, {}
            for name, port in s.op_inputs.items():
                v = picked[name] if name in picked else given.get(name, NO_VALUE)
                if port.type.startswith("File"):
                    if s.ins[name].folder:
                        ins[name] = {"folder": [x.name for x in _leaves(v)]}
                    else:
                        ins[name] = _names(v)
                else:
                    params[name] = v
            for name, port in s.op_outputs.items():
                if port.axis:
                    leaves = [Leaf(f"{sid}__{name}{vals}__{_safe(x)}"
                                   + _named(port.name, dict(assign + ((port.axis, str(x)),))),
                                   assign + ((port.axis, str(x)),))
                              for x in wf.inputs[port.axis].value]
                    out_vals[name] = leaves
                    outs[name] = [x.name for x in leaves]
                else:
                    leaf = Leaf(f"{sid}__{name}{vals}" + _named(port.name, dict(assign)), assign)
                    out_vals[name] = leaf
                    outs[name] = leaf.name
            per_copy_out.append(out_vals)
            node = {"id": nid, "_origin": sid, "_assign": dict(assign), "code": fn.get("code"),
                    "entrypoint": fn.get("entrypoint") or "main", "inputs": ins, "outputs": outs, "params": params}
            if s.image:
                node["image"] = _image(s.image)
            if s.resources:
                node["resources"] = _resources(s.resources)
            if s.packages:
                node["packages"] = list(s.packages)
            waits = list(s.after) + ([s.run_if["step"]] if s.run_if else [])
            if waits:
                node["after"] = [c for w in waits for c in copies_of[w]]
            if s.run_if:
                node["run_if"] = {"after": copies_of[s.run_if["step"]], "result": s.run_if["result"]}
            if s.secrets:
                node["secrets"] = list(s.secrets)
            nodes.append(node)
        copies_of[sid] = ids
        # each output's value as the next steps see it: nested by the scatter
        for name in s.op_outputs:
            per = [o[name] for o in per_copy_out]
            if not s.scatter:
                values[f"{sid}/{name}"] = per[0]
            elif (s.scatter_method or "dotproduct") in ("dotproduct", "flat_crossproduct") or len(s.scatter) == 1:
                values[f"{sid}/{name}"] = per
            else:  # nested_crossproduct: one level per scattered entry
                shape = [len(given[n]) for n in s.scatter]
                values[f"{sid}/{name}"] = _nest(per, shape)

    outputs = []
    for n, o in wf.outputs.items():
        v = values[o.sources[0]]
        for leaf in _leaves(v):
            assign = dict(leaf.assign)
            deliver = o.deliver_as or f"results/{n}"
            deliver = PLACEHOLDER.sub(lambda m: str(assign.get(m.group(1), m.group(0))), deliver)
            outputs.append({"file": leaf.name, "deliver_as": deliver})

    exp = {"name": wf.label or wf.path.stem, "description": wf.doc or "",
           "defaults": {}, "inputs": inputs_list, "outputs": outputs, "nodes": nodes, "_base": str(wf.base)}
    if wf.image:
        exp["defaults"]["image"] = _image(wf.image)
    if wf.resources:
        exp["defaults"]["resources"] = _resources(wf.resources)
    return exp


def _named(name, assign: dict) -> str:
    """`__<curate:name>` with the file's axis values filled in, or "" without a curate:name."""
    if not name:
        return ""
    return "__" + PLACEHOLDER.sub(lambda m: str(assign.get(m.group(1), m.group(0))), name)


def _nest(flat: list, shape: list[int]):
    if len(shape) == 1:
        return list(flat)
    step = len(flat) // shape[0]
    return [_nest(flat[i * step:(i + 1) * step], shape[1:]) for i in range(shape[0])]


def main(argv=None) -> int:
    from cwl.validate import validate
    ap = argparse.ArgumentParser(description="Expand a CURATE CWL workflow into jobs")
    ap.add_argument("workflow")
    ap.add_argument("--inputs")
    a = ap.parse_args(argv)
    wf, errors, _ = validate(a.workflow, a.inputs)
    for e in errors:
        print("ERROR:", e)
    if errors:
        return 1
    for n in expand_cwl(wf)["nodes"]:
        print(f"{n['id']:<28} in={n['inputs']}  out={n['outputs']}  params={n['params']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
