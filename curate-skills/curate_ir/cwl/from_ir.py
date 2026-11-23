# /// script
# requires-python = ">=3.11"
# dependencies = ["cwl-utils==0.47", "pyyaml==6.0.2"]
# ///
"""Convert an IR v0.2 workflow (docs/IR_SPEC.md) to a CURATE CWL workflow (docs/CWL_IR.md).

  from_ir.py ir.yaml --out DIR      # writes DIR/workflow.yml, DIR/inputs.yml, DIR/functions/

  from cwl.from_ir import convert, write
  conv = convert(ir)                # ir as loaded by curate_ir/validate.py load()
  write(conv, out_dir)

The mapping follows docs/CWL_IR.md, "Mapping from IR v0.2". Producers and consumers are matched
by file-name template (the literal parts and the axes in between, with equal axis values), so
pegasus_ir's alias axes (`f_{rank3_all}` read as `f_{rank3}`) resolve to the same output.
File names themselves are dropped; marker files (`curate_done*`) become curate:after.

Axis inputs reproduce IR v0.2's param types exactly: int[] for an axis used only through
whole-value renamed params with all-digit values, string[] for one injected under its own name,
and a refusal for an all-digit axis used both ways.

What plain CWL cannot express without expressions is refused, one message each (ConvertError):
a step over several axes that reads a per-copy file, pairing over two axes, a file read by a
literal member of a family, a param that only partly holds a placeholder, a param that is not
a scalar or a list of scalars, a list over two axes written by one step, and two consumers
needing different groupings of one producer.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cwl import NS  # noqa: E402
from expand import PLACEHOLDER  # noqa: E402
import validate as V0  # noqa: E402  (the IR v0.2 validator)

MARKER = "curate_done"


class ConvertError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def template(pattern: str) -> tuple[tuple, list]:
    parts = PLACEHOLDER.split(str(pattern))
    return tuple(parts[0::2]), parts[1::2]


def _dedup(xs):
    return list(dict.fromkeys(xs))


def _id(name: str, taken: set) -> str:
    base = re.sub(r"[^a-z0-9_]+", "_", str(name).lower()).strip("_") or "x"
    if base[0].isdigit():
        base = "f_" + base
    out, n = base, 2
    while out in taken:
        out, n = f"{base}_{n}", n + 1
    taken.add(out)
    return out


def _image(url: str) -> str:
    return url[len("docker:///"):] if url.startswith("docker:///") else url


def _resources(r: dict) -> dict:
    out = {}
    if r.get("cpus") is not None:
        out["coresMin"] = r["cpus"]
    if r.get("memory_mb") is not None:
        out["ramMin"] = r["memory_mb"]
    return out


def _value_type(v):
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list) and v:
        ts = {_value_type(x) for x in v}
        if len(ts) == 1 and None not in ts and not next(iter(ts)).endswith("[]"):
            return next(iter(ts)) + "[]"
    return None


class _Source:
    def __init__(self, ref, node, pattern, lits, phs, G, E):
        self.ref, self.node, self.pattern = ref, node, pattern
        self.lits, self.phs, self.G, self.E = lits, phs, G, E


def convert(ir: dict) -> dict:
    """{"workflow": CWL dict, "inputs": inputs-file dict, "functions": {code path: absolute file},
    "base": the IR's folder}. Raises ConvertError."""
    _, errors, _, _ = V0.prepare(ir)
    if errors:
        raise ConvertError([f"the IR is not valid: {e}" for e in errors])
    problems = []
    base = Path(ir["_base"])
    axes = {a: [str(v) for v in vals] for a, vals in (ir.get("axes") or {}).items()}
    nodes = ir.get("nodes") or []
    fe = {n["id"]: list(n.get("for_each") or []) for n in nodes}
    taken = {n["id"] for n in nodes}

    # every file family a source can hand on
    sources: list[_Source] = []
    markers: dict[tuple, str] = {}  # marker template -> producing node
    in_ids = {}
    for inp in ir.get("inputs") or []:
        lits, phs = template(inp["file"])
        E = _dedup(phs)
        if len(E) > 1:
            problems.append(f"workflow input {inp['file']}: a file family over several axes is not supported")
            continue
        iid = _id(inp["file"].replace("{", "").replace("}", ""), taken)
        in_ids[inp["file"]] = iid
        sources.append(_Source(iid, None, inp["file"], lits, phs, [], E))
    for n in nodes:
        for key, pat in (n.get("outputs") or {}).items():
            lits, phs = template(pat)
            if key.startswith(MARKER):
                markers[(lits, len(phs))] = n["id"]
                continue
            E = [a for a in _dedup(phs) if a not in fe[n["id"]]]
            if len(E) > 1:
                problems.append(f"{n['id']}: output {key} is a list over several axes ({', '.join(E)}); "
                                "curate:axis names one")
            sources.append(_Source(f"{n['id']}/{key}", n["id"], pat, lits, phs, fe[n["id"]], E))

    def find(pattern, where):
        """(source, {source axis: consumer axis}) for the family a pattern names."""
        lits, phs = template(pattern)
        for s in sources:
            if s.lits != lits or len(s.phs) != len(phs):
                continue
            m, ok = {}, True
            for sa, ca in zip(s.phs, phs):
                if m.setdefault(sa, ca) != ca or axes.get(sa) != axes.get(ca):
                    ok = False
            if ok and len(set(m.values())) == len(m):
                return s, m
        problems.append(f"{where}: {pattern} matches no file family a step writes or the workflow takes "
                        "(reading one member of a family by its literal name is not supported)")
        return None, None

    # scatter order: a consumer that keeps one axis of a multi-axis producer needs it outermost
    want: dict[str, dict[str, str]] = {}
    plan = {}  # (node, input key) -> (source, mapping)
    for n in nodes:
        F = fe[n["id"]]
        for key, v in (n.get("inputs") or {}).items():
            if key.startswith(MARKER) or isinstance(v, dict):
                continue
            s, m = find(v, f"{n['id']} input {key}")
            if s is None:
                continue
            plan[(n["id"], key)] = (s, m)
            paired = [a for a in s.G + s.E if m.get(a) in F]
            if not paired:
                continue
            if len(F) > 1:
                problems.append(f"{n['id']}: input {key} is one file per copy of a step over several axes "
                                f"({', '.join(F)}); plain CWL cannot pair it")
            elif len(paired) > 1:
                problems.append(f"{n['id']}: input {key} pairs over several axes; plain CWL cannot express it")
            elif paired[0] in s.E and s.G:
                problems.append(f"{n['id']}: input {key} pairs on the inner list axis {paired[0]} of {s.ref}")
            elif paired[0] in s.G and len(s.G) > 1:
                want.setdefault(s.node, {})[n["id"]] = paired[0]
    order = dict(fe)
    for producer, by in want.items():
        if len(set(by.values())) > 1:
            problems.append(f"{producer}: consumers need different groupings ("
                            + ", ".join(f"{c} by {a}" for c, a in sorted(by.items()))
                            + "); plain CWL cannot regroup without JavaScript")
        else:
            first = next(iter(by.values()))
            order[producer] = [first] + [a for a in fe[producer] if a != first]

    def dims(s: _Source) -> list[str]:
        return (order[s.node] if s.node else []) + s.E

    # The type of each axis input reproduces what IR v0.2 hands the function: a whole-value
    # renamed param ("rank": "{rank3}") gets an int when the values are all digits; a value
    # injected under the axis's own name is always a string ("2019").
    renamed_use, own_use = {}, {}
    for n in nodes:
        F = fe[n["id"]]
        whole = {m.group(1) for v in (n.get("params") or {}).values() if isinstance(v, str)
                 for m in [PLACEHOLDER.fullmatch(v)] if m and m.group(1) in F}
        for a in F:
            (renamed_use if a in whole else own_use).setdefault(a, []).append(n["id"])
    axis_type = {}
    for a, vals in axes.items():
        digits = all(v.isdigit() for v in vals)
        if digits and a in renamed_use and a in own_use:
            problems.append(f"axis {a} reaches {', '.join(renamed_use[a])} as an int (a renamed param) and "
                            f"{', '.join(own_use[a])} as a string (under its own name); one CWL input cannot be "
                            "both: rename it the same way in every step")
        axis_type[a] = "int" if digits and a not in own_use else "string"

    steps, used_axes, scatter_used, merge_used = {}, set(), False, False
    for n in nodes:
        nid, F = n["id"], fe[n["id"]]
        ins, op_in, op_out, outs, scatter, after = {}, {}, {}, [], [], []
        params = n.get("params") or {}
        rename = {}
        for k, v in params.items():
            m = PLACEHOLDER.fullmatch(v) if isinstance(v, str) else None
            if m and m.group(1) in F:
                rename[m.group(1)] = k
        for a in order[nid]:
            name = rename.get(a, a)
            if name in (n.get("inputs") or {}):
                problems.append(f"{nid}: param {name} has the name of an input")
            ins[name] = a
            op_in[name] = axis_type[a]
            scatter.append(name)
            used_axes.add(a)
        for k, v in params.items():
            if k in rename.values():
                continue
            if isinstance(v, str) and any(a in F for a in PLACEHOLDER.findall(v)):
                problems.append(f"{nid}: param {k}={v!r} only partly holds an axis value; plain CWL cannot "
                                "build it per copy")
                continue
            t = _value_type(v)
            if t is None:
                problems.append(f"{nid}: param {k} is {type(v).__name__}; the CWL profile takes scalars and "
                                "lists of scalars")
                continue
            ins[k] = {"default": v}
            op_in[k] = t
        paired_keys = []
        for key, v in (n.get("inputs") or {}).items():
            if key.startswith(MARKER):
                lits, phs = template(v)
                producer = markers.get((lits, len(phs)))
                if producer is None:
                    problems.append(f"{nid}: marker input {v} has no producer")
                elif producer not in after:
                    after.append(producer)
                continue
            if isinstance(v, dict):  # folder input
                pats = v.get("folder")
                pats = pats if isinstance(pats, list) else [pats]
                srcs = []
                for pat in pats:
                    s, m = find(pat, f"{nid} folder input {key}")
                    if s is None:
                        continue
                    if any(m.get(a) in F for a in dims(s)):
                        problems.append(f"{nid}: folder input {key} changes per copy; a folder holds the same "
                                        "files for every copy")
                    elif len(dims(s)) > 1:
                        problems.append(f"{nid}: folder input {key} gathers {s.ref} over several axes")
                    srcs.append((s.ref, len(dims(s))))
                if len(srcs) == 1 and srcs[0][1] == 1:
                    ins[key] = {"source": srcs[0][0], "curate:folder": True}
                elif srcs:
                    ins[key] = {"source": [r for r, _ in srcs], "linkMerge": "merge_flattened", "curate:folder": True}
                    merge_used = True
                op_in[key] = "File[]"
                continue
            if (nid, key) not in plan:
                continue
            s, m = plan[(nid, key)]
            d = dims(s)
            paired = [a for a in d if m.get(a) in F]
            gathered_pattern = [m_inv for m_inv in _dedup(template(v)[1]) if m_inv not in F]
            inv = {c: a for a, c in m.items()}
            if [inv[c] for c in gathered_pattern] != [a for a in d if a not in paired]:
                problems.append(f"{nid}: input {key} gathers {s.ref} in another order than it is written "
                                f"({', '.join(gathered_pattern)} vs {', '.join(a for a in d if a not in paired)})")
            ins[key] = s.ref
            depth = len(d) - (1 if paired else 0)
            op_in[key] = "File" + "[]" * depth
            if paired:
                paired_keys.append(key)
        if paired_keys:
            scatter += paired_keys
        for key, pat in (n.get("outputs") or {}).items():
            if key.startswith(MARKER):
                continue
            E = [a for a in _dedup(template(pat)[1]) if a not in F]
            if E:
                op_out[key] = {"type": "File[]", "curate:axis": E[0]}
                used_axes.add(E[0])
            else:
                op_out[key] = "File"
            outs.append(key)
        hints = {"curate:function": {"code": n["code"], "entrypoint": n.get("entrypoint", "main")}}
        if n.get("image"):
            hints["DockerRequirement"] = {"dockerPull": _image(n["image"])}
        if n.get("resources"):
            hints["ResourceRequirement"] = _resources(n["resources"])
        if n.get("packages"):
            hints["SoftwareRequirement"] = {"packages": [{"package": p} for p in n["packages"]]}
        if after:
            hints["curate:after"] = {"steps": after}
        step = {}
        if scatter:
            scatter_used = True
            step["scatter"] = scatter if len(scatter) > 1 else scatter[0]
            if len(scatter) > 1:
                step["scatterMethod"] = "dotproduct" if paired_keys else "nested_crossproduct"
        step.update({"in": ins, "out": outs,
                     "run": {"class": "Operation", "inputs": op_in, "outputs": op_out}, "hints": hints})
        steps[nid] = step

    # workflow inputs: axes, then files
    wf_inputs, values = {}, {}
    for a in [a for a in axes if a in used_axes]:
        ints = axis_type[a] == "int"
        vals = [int(v) for v in axes[a]] if ints else list(axes[a])
        wf_inputs[a] = {"type": "int[]" if ints else "string[]", "default": vals}
        values[a] = vals
    for inp in ir.get("inputs") or []:
        iid = in_ids.get(inp["file"])
        if iid is None:
            continue
        _, phs = template(inp["file"])
        loc = inp["location"]
        if phs:
            a = phs[0]
            val = [{"class": "File", "location": loc.replace("{%s}" % a, v)} for v in axes[a]]
            wf_inputs[iid] = {"type": "File[]", "default": val}
        else:
            val = {"class": "File", "location": loc}
            wf_inputs[iid] = {"type": "File", "default": val}
        values[iid] = val

    wf_outputs = {}
    for o in ir.get("outputs") or []:
        s, m = find(o["file"], f"workflow output {o['file']}")
        if s is None:
            continue
        if s.node is None:
            problems.append(f"workflow output {o['file']} is a workflow input, not a step's output")
            continue
        d = dims(s)
        deliver = o.get("deliver_as") or o["file"]
        for a, c in m.items():
            deliver = deliver.replace("{%s}" % c, "{%s}" % a)
        wf_outputs[_id(o["file"].replace("{", "").replace("}", ""), taken)] = {
            "type": "File" + "[]" * len(d), "outputSource": s.ref, "curate:deliver_as": deliver}

    if problems:
        raise ConvertError(_dedup(problems))

    requirements = {}
    if scatter_used:
        requirements["ScatterFeatureRequirement"] = {}
    if merge_used:
        requirements["MultipleInputFeatureRequirement"] = {}
    defaults = ir.get("defaults") or {}
    hints = {}
    if defaults.get("image"):
        hints["DockerRequirement"] = {"dockerPull": _image(defaults["image"])}
    if defaults.get("resources"):
        hints["ResourceRequirement"] = _resources(defaults["resources"])
    doc = {"cwlVersion": "v1.2", "class": "Workflow", "$namespaces": {"curate": NS},
           "label": ir.get("name")}
    if ir.get("description"):
        doc["doc"] = ir["description"]
    if requirements:
        doc["requirements"] = requirements
    if hints:
        doc["hints"] = hints
    doc.update(inputs=wf_inputs, outputs=wf_outputs, steps=steps)
    functions = {n["code"]: str(base / n["code"]) for n in nodes}
    return {"workflow": doc, "inputs": values, "functions": functions, "base": str(base)}


def _relocate(value, out: Path):
    """file:// locations under `out` become relative to it."""
    if isinstance(value, list):
        return [_relocate(v, out) for v in value]
    if isinstance(value, dict) and value.get("class") == "File":
        loc = value.get("location", "")
        if loc.startswith("file://"):
            p = Path(loc[len("file://"):])
            try:
                return {**value, "location": str(p.relative_to(out))}
            except ValueError:
                pass
        return value
    return value


def write(conv: dict, out) -> Path:
    """A NEW folder: workflow.yml, inputs.yml and the function files at their code paths."""
    import yaml

    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError(f"{out} already exists; nothing was written")
    out.mkdir(parents=True)
    for rel, src in conv["functions"].items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, out / rel)
    doc = conv["workflow"]
    for i in doc["inputs"].values():
        if "default" in i:
            i["default"] = _relocate(i["default"], out)
    values = {k: _relocate(v, out) for k, v in conv["inputs"].items()}
    dump = lambda x: yaml.safe_dump(x, sort_keys=False, default_flow_style=False, allow_unicode=True)  # noqa: E731
    (out / "workflow.yml").write_text(dump(doc))
    (out / "inputs.yml").write_text(dump(values))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="IR v0.2 -> CURATE CWL workflow")
    ap.add_argument("ir")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    try:
        conv = convert(V0.load(a.ir))
    except ConvertError as e:
        for p in e.problems:
            print("ERROR:", p)
        return 1
    out = write(conv, a.out)
    from cwl.validate import validate
    _, errors, warnings = validate(out / "workflow.yml", out / "inputs.yml")
    for w in warnings:
        print("warn :", w)
    for e in errors:
        print("ERROR:", e)
    print(f"wrote {out}; {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
