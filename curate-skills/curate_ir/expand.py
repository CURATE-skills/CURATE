#!/usr/bin/env python3
"""Expand IR v0.2 fan-out (axes / for_each / gather) into plain nodes.

  python3 expand.py ir.yaml          # print the expanded node list

v0.2 adds, on top of v0.1:

  axes:                       # named lists of values
    winter: ["2019", "2020"]
  nodes:
    - id: detect_ice
      for_each: [winter]      # one parallel copy per value (several axes: every combination)
      inputs:  {daily: daily.csv}
      outputs: {ice: "ice_{winter}.json"}     # {winter} filled in per copy
    - id: assemble            # no for_each over winter ...
      inputs:  {ice: "ice_{winter}.json"}     # ... so this becomes a LIST of all copies' files

Rules
- Placeholders {axis} may appear in node input/output file names, in string
  params, and in workflow inputs/outputs (file, location, deliver_as).
- A fan-out copy gets each of its axis values in params (unless params sets it).
- An output of a fan-out node must contain every axis it fans out over
  (otherwise copies would write the same file).
- An input placeholder whose axis is NOT in the node's for_each is a gather:
  the function receives a list of paths, in axis-value order.
- Axis values must be file-name safe: letters, digits, '.', '_', '-'.

Expansion produces v0.1-shaped nodes (input values may be lists), so the
validator's checks and every backend work on the expanded form.
"""
import copy
import itertools
import re
import sys

PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
AXIS_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
AXIS_VALUE = re.compile(r"^[A-Za-z0-9._-]+$")


def _fill(text, assign):
    return PLACEHOLDER.sub(lambda m: assign.get(m.group(1), m.group(0)), text)


def _fill_obj(obj, assign):
    if isinstance(obj, str):
        return _fill(obj, assign)
    if isinstance(obj, list):
        return [_fill_obj(x, assign) for x in obj]
    if isinstance(obj, dict):
        return {k: _fill_obj(v, assign) for k, v in obj.items()}
    return obj


def _combos(names, axes):
    """All assignments for the given axis names, in declaration order."""
    names = list(names)
    for values in itertools.product(*(axes[n] for n in names)):
        yield dict(zip(names, values))


def _safe(value):
    return re.sub(r"[^a-z0-9_]", "_", value.lower())


def expand(ir):
    """Return (expanded_ir, errors). The input dict is not modified."""
    errors = []
    raw_axes = ir.get("axes", {}) or {}
    axes = {}
    for name, values in raw_axes.items():
        if not AXIS_NAME.match(str(name)):
            errors.append(f"axis name {name!r} must match [a-z][a-z0-9_]*")
            continue
        if not isinstance(values, list) or not values:
            errors.append(f"axis {name} must be a non-empty list")
            continue
        vals = [str(v) for v in values]
        bad = [v for v in vals if not AXIS_VALUE.match(v)]
        if bad:
            errors.append(f"axis {name}: values not file-name safe: {bad}")
        if len(set(vals)) != len(vals):
            errors.append(f"axis {name}: duplicate values")
        axes[name] = vals

    def unknown(text, where):
        for a in PLACEHOLDER.findall(text):
            if a not in axes:
                errors.append(f"{where}: placeholder {{{a}}} is not a declared axis")

    out = copy.deepcopy({k: v for k, v in ir.items() if k not in ("axes", "nodes", "inputs", "outputs")})

    # workflow inputs / outputs: expand over the axes they mention
    for key in ("inputs", "outputs"):
        items = []
        for item in ir.get(key, []) or []:
            text = " ".join(str(v) for v in item.values())
            unknown(text, f"workflow {key} {item.get('file')}")
            used = [a for a in dict.fromkeys(PLACEHOLDER.findall(text)) if a in axes]
            for assign in _combos(used, axes):
                items.append(_fill_obj(item, assign))
        out[key] = items

    nodes = []
    for n in ir.get("nodes", []) or []:
        nid = n.get("id", "")
        fe = n.get("for_each", []) or []
        if isinstance(fe, str):
            fe = [fe]
        bad_fe = [a for a in fe if a not in axes]
        if bad_fe:
            errors.append(f"{nid}: for_each uses undeclared axes {bad_fe}")
            continue
        for f in (n.get("inputs") or {}).values():
            unknown(str(f), f"{nid} input")
        for name, f in (n.get("outputs") or {}).items():
            unknown(str(f), f"{nid} output")
            used = set(PLACEHOLDER.findall(str(f)))
            missing = [a for a in fe if a not in used]
            if missing:
                errors.append(f"{nid}: output {name}={f} must contain "
                              + ", ".join("{%s}" % a for a in missing)
                              + " (otherwise its copies write the same file)")
            extra = [a for a in used if a in axes and a not in fe]
            if extra:
                errors.append(f"{nid}: output {name}={f} uses {extra} but the node does not fan out over it")

        for assign in _combos(fe, axes):
            m = copy.deepcopy({k: v for k, v in n.items() if k != "for_each"})
            m["_origin"] = nid
            m["_assign"] = dict(assign)
            if fe:
                m["id"] = nid + "__" + "__".join(_safe(assign[a]) for a in fe)
            m["outputs"] = {k: _fill(str(v), assign) for k, v in (n.get("outputs") or {}).items()}
            inputs = {}
            for k, v in (n.get("inputs") or {}).items():
                v = _fill(str(v), assign)
                gather = [a for a in dict.fromkeys(PLACEHOLDER.findall(v)) if a in axes]
                if gather:
                    inputs[k] = [_fill(v, g) for g in _combos(gather, axes)]
                else:
                    inputs[k] = v
            m["inputs"] = inputs
            params = _fill_obj(copy.deepcopy(n.get("params") or {}), assign)
            for a, val in assign.items():
                params.setdefault(a, val)
            m["params"] = params
            nodes.append(m)
    out["nodes"] = nodes
    return out, errors


def main():
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    from validate import load
    ir = load(sys.argv[1])
    exp, errors = expand(ir)
    for e in errors:
        print("ERROR:", e)
    for n in exp["nodes"]:
        ins = {k: v for k, v in n["inputs"].items()}
        print(f"{n['id']:<28} in={ins}  out={n['outputs']}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
