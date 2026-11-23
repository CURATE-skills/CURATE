# /// script
# requires-python = ">=3.11"
# dependencies = ["cwl-utils==0.47", "pyyaml==6.0.2"]
# ///
"""Check a CURATE workflow (CWL v1.2 restricted to the profile in docs/CWL_IR.md).

  validate.py workflow.yml [--inputs inputs.yml]     # exit 0 = valid; warnings printed

Two passes:
1. The raw document: refused CWL constructs (`when`, expressions, CommandLineTool, sub-workflows,
   valueFrom, secondaryFiles, linkMerge, loops, requirements outside the profile), each with its
   own one-line message, before cwl-utils sees it.
2. The loaded workflow (cwl/load.py): every step's curate:function and its file; the
   extensions' shapes; wiring and types from source to sink (with scatter and merge_flattened);
   fan-out sizes from workflow inputs only; dotproduct pairs of the same axis (which is how the
   partial-gather order and curate:axis lists are checked); deliver_as placeholders; after/run_if
   targets and cycles.

Errors make the workflow unusable. Warnings are portability risks, as in curate_ir/validate.py.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cwl import EXTENSIONS, NS  # noqa: E402
from cwl.load import NO_VALUE, LoadError, Workflow, load, read_yaml  # noqa: E402

STEP_ID = re.compile(r"^[a-z0-9_]+$")
AXIS_VALUE = re.compile(r"^[A-Za-z0-9._-]+$")
SECRET = re.compile(r"^[A-Z][A-Z0-9_]*$")
PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")
ABS_PATH = re.compile(r"(^|[\s'\"=])(/(home|Users|tmp|root|mnt|data|scratch)/[^\s'\"]*)")
INSTALL = re.compile(r"\b(pip|conda|apt-get|apt|dnf|yum)\s+install\b|subprocess.*\bpip\b")

BASE_TYPES = {"File", "Directory", "string", "int", "float", "boolean"}
SCATTER_METHODS = {"dotproduct", "nested_crossproduct", "flat_crossproduct"}
WORKFLOW_REQUIREMENTS = {"ScatterFeatureRequirement", "MultipleInputFeatureRequirement"}
STEP_REQUIREMENTS = {"DockerRequirement", "SoftwareRequirement", "ResourceRequirement"}
REFUSED_REQUIREMENTS = {
    "InlineJavascriptRequirement": "InlineJavascriptRequirement is refused: CURATE workflows have no "
                                   "expressions",
    "StepInputExpressionRequirement": "StepInputExpressionRequirement is refused: valueFrom is not allowed",
    "SubworkflowFeatureRequirement": "SubworkflowFeatureRequirement is refused: steps are inline "
                                     "Operations, not sub-workflows",
    "Loop": "loops are refused (cwltool:Loop)",
}
REFUSED_KEYS = {
    "when": "`when` is refused; use curate:run_if for a step that runs only on its predecessor's result",
    "pickValue": "`pickValue` is refused",
    "valueFrom": "`valueFrom` is refused: step inputs come from sources or defaults",
    "secondaryFiles": "`secondaryFiles` is refused",
    "loop": "loops are refused",
}
RUN_CLASSES = {
    "CommandLineTool": "is a CommandLineTool; steps are inline Operations (the function is named by "
                       "curate:function)",
    "ExpressionTool": "is an ExpressionTool; expressions are refused",
    "Workflow": "is a sub-workflow; sub-workflows are refused",
}


# ── pass 1: the raw document ─────────────────────────────────────────────────

def _entries(x):
    """(id, value) pairs of a CWL map-or-list field."""
    if isinstance(x, dict):
        return list(x.items())
    if isinstance(x, list):
        return [(e.get("id") if isinstance(e, dict) else e, e) for e in x]
    return []


def _req_classes(x):
    if isinstance(x, dict):
        return list(x)
    if isinstance(x, list):
        return [e.get("class", "") for e in x if isinstance(e, dict)]
    return []


def _short(cls: str) -> str:
    cls = str(cls)
    if cls.startswith(NS):
        return "curate:" + cls[len(NS):]
    return cls.rsplit("#", 1)[-1].split(":", 1)[-1] if not cls.startswith("curate:") else cls


def raw_errors(doc) -> list[str]:
    """Profile refusals that are visible in the raw YAML."""
    if not isinstance(doc, dict):
        return ["the file is not a CWL document (a YAML mapping)"]
    errors = []
    if doc.get("cwlVersion") != "v1.2":
        errors.append(f"cwlVersion must be v1.2, got {doc.get('cwlVersion')!r}")
    if doc.get("class") != "Workflow":
        return errors + [f"the top-level class must be Workflow, got {doc.get('class')!r}"]

    def walk(x, where):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in REFUSED_KEYS:
                    errors.append(f"{where}: {REFUSED_KEYS[k]}")
                walk(v, f"{where}.{k}" if where else str(k))
        elif isinstance(x, list):
            for i, v in enumerate(x):
                walk(v, f"{where}[{i}]")
        elif isinstance(x, str) and ("$(" in x or "${" in x):
            errors.append(f"{where}: expressions ($(...) / ${{...}}) are refused")
    walk({k: v for k, v in doc.items() if k != "$namespaces"}, "")

    def reqs(obj, where, allowed):
        for key in ("requirements", "hints"):
            for cls in _req_classes(obj.get(key)):
                short = _short(cls)
                if short in REFUSED_REQUIREMENTS:
                    errors.append(f"{where}: {REFUSED_REQUIREMENTS[short]}")
                elif short.startswith("curate:"):
                    if where == "workflow" or "run" in where:
                        errors.append(f"{where}: {short} belongs in a step's hints")
                    elif key != "hints":
                        errors.append(f"{where}: {short} goes under hints, not requirements")
                elif short not in allowed:
                    errors.append(f"{where}: {short} is not in the CURATE profile")
    reqs(doc, "workflow", WORKFLOW_REQUIREMENTS | STEP_REQUIREMENTS)

    names = [n for n, _ in _entries(doc.get("inputs"))] + [n for n, _ in _entries(doc.get("outputs"))]
    for sid, step in _entries(doc.get("steps")):
        names.append(sid)
        where = f"step {sid}"
        if not isinstance(step, dict):
            errors.append(f"{where}: not a mapping")
            continue
        if not STEP_ID.match(str(sid)) or "__" in str(sid):
            errors.append(f"{where}: step ids must match [a-z0-9_]+ without '__' (they become job and action "
                          "names, and '__' separates a copy's values)")
        run = step.get("run")
        if isinstance(run, str):
            errors.append(f"{where}: run: {run} refers to another file; steps are inline Operations "
                          "(sub-workflows and external tools are refused)")
        elif not isinstance(run, dict):
            errors.append(f"{where}: needs an inline run: {{class: Operation, ...}}")
        elif run.get("class") != "Operation":
            msg = RUN_CLASSES.get(run.get("class"), f"has class {run.get('class')!r}; steps are inline Operations")
            errors.append(f"{where}: run {msg}")
        else:
            reqs(run, f"{where} run", STEP_REQUIREMENTS)
        reqs(step, where, STEP_REQUIREMENTS)
        for name, e in _entries(step.get("in")):
            if not isinstance(e, dict):
                continue
            folder = e.get("curate:folder") is True or e.get(NS + "folder") is True
            lm = e.get("linkMerge")
            if lm is not None and lm != "merge_nested" and not (lm == "merge_flattened" and folder):
                errors.append(f"{where} input {name}: linkMerge {lm} is refused"
                              + ("; merge_flattened is allowed only on a curate:folder input"
                                 if lm == "merge_flattened" else ""))
            src = e.get("source")
            if isinstance(src, list) and len(src) > 1 and not folder:
                errors.append(f"{where} input {name}: several sources are allowed only on a curate:folder "
                              "input (with linkMerge: merge_flattened)")
            if isinstance(src, list) and len(src) > 1 and folder and lm != "merge_flattened":
                errors.append(f"{where} input {name}: a folder from several sources needs "
                              "linkMerge: merge_flattened")
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        errors.append(f"ids used twice among workflow inputs, outputs and steps: {', '.join(map(str, dup))} "
                      "(CWL gives them one namespace)")
    return errors


# ── pass 2: the loaded workflow ──────────────────────────────────────────────

@dataclass(frozen=True)
class Dim:
    label: str | None   # the axis input it comes from; None for a plain File[] input
    length: int | None


@dataclass
class Shape:
    type: str
    dims: tuple  # one Dim per outer array level that has a known axis or length


def _depth(t: str) -> int:
    return (len(t) - len(t.rstrip("[]"))) // 2


def _base(t: str) -> str:
    return t[:-2 * _depth(t)] if _depth(t) else t


def _strip(t: str) -> str:
    return t[:-2] if t.endswith("[]") else t


def _value_ok(t: str, v) -> bool:
    if _depth(t):
        return isinstance(v, list) and all(_value_ok(_strip(t), x) for x in v)
    return {"File": lambda x: isinstance(x, dict) and x.get("class") == "File",
            "Directory": lambda x: isinstance(x, dict) and x.get("class") == "Directory",
            "string": lambda x: isinstance(x, str),
            "int": lambda x: isinstance(x, int) and not isinstance(x, bool),
            "float": lambda x: isinstance(x, (int, float)) and not isinstance(x, bool),
            "boolean": lambda x: isinstance(x, bool)}.get(t, lambda x: False)(v)


def is_axis(wf: Workflow, name: str) -> bool:
    i = wf.inputs.get(name)
    return bool(i) and _depth(i.type) == 1 and _base(i.type) in ("string", "int")


def check(wf: Workflow) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    steps = wf.steps

    # types allowed in the profile
    def type_ok(t: str, where: str, directory: bool):
        if "|" in t or "?" in t or "null" in t:
            errors.append(f"{where}: type {t}: optional and union types are not in the CURATE profile")
        elif _base(t) not in BASE_TYPES:
            errors.append(f"{where}: type {t} is not in the CURATE profile")
        elif _base(t) == "Directory" and not directory:
            errors.append(f"{where}: Directory is allowed only as a workflow input")

    for n, i in wf.inputs.items():
        type_ok(i.type, f"workflow input {n}", True)
        if i.value is NO_VALUE:
            errors.append(f"workflow input {n} has no value: give it a default (the tested value) or a "
                          "value in the inputs file")
        elif not _value_ok(i.type, i.value):
            errors.append(f"workflow input {n}: its value does not match type {i.type}")
    for n in wf.unknown_inputs:
        errors.append(f"inputs file: {n} is not an input of the workflow")
    for n, o in wf.outputs.items():
        type_ok(o.type, f"workflow output {n}", False)

    # axes: string[] / int[] inputs that a step scatters over or a curate:axis names
    used_axes = set()
    for s in steps.values():
        for name in s.scatter:
            used_axes.update(src for src in (s.ins.get(name).sources if name in s.ins else [])
                             if is_axis(wf, src))
        used_axes.update(p.axis for p in s.op_outputs.values() if p.axis)
    for a in sorted(used_axes):
        if not is_axis(wf, a):
            continue
        vals = wf.inputs[a].value
        if vals is NO_VALUE or not isinstance(vals, list):
            continue
        if not vals:
            errors.append(f"axis {a} has no values")
        bad = [v for v in vals if not AXIS_VALUE.match(str(v))]
        if bad:
            errors.append(f"axis {a}: values not file-name safe (letters, digits, '.', '_', '-'): {bad}")
        if len({str(v) for v in vals}) != len(vals):
            errors.append(f"axis {a}: duplicate values")

    def axis_len(a):
        v = wf.inputs[a].value if a in wf.inputs else NO_VALUE
        return len(v) if isinstance(v, list) else None

    def standard_hint(h, where):
        given = {k for k, v in h.fields.items() if v is not None}
        if h.cls == "DockerRequirement" and given - {"dockerPull"}:
            errors.append(f"{where}: DockerRequirement takes only dockerPull")
        elif h.cls == "ResourceRequirement" and given - {"coresMin", "ramMin"}:
            errors.append(f"{where}: ResourceRequirement takes only coresMin and ramMin")
        elif h.cls == "SoftwareRequirement" and not all(isinstance(p, dict) and p.get("package")
                                                        for p in h.fields.get("packages") or []):
            errors.append(f"{where}: SoftwareRequirement packages need a package name each")

    for h in wf.hints:
        standard_hint(h, "workflow")

    # per-step checks that need no shapes
    image = wf.image
    for sid, s in steps.items():
        where = f"step {sid}"
        for h in s.hints:
            if h.cls.startswith("curate:"):
                name = h.cls.split(":", 1)[1]
                if name not in EXTENSIONS or EXTENSIONS[name] != "step hints":
                    errors.append(f"{where}: {h.cls} is not a step extension")
                elif h.where.startswith("run"):
                    errors.append(f"{where}: {h.cls} belongs in the step's hints, not the Operation's")
            else:
                standard_hint(h, where)
        fn = s.function
        if not fn:
            errors.append(f"{where}: needs hints: curate:function: {{code, entrypoint}}")
        else:
            code, entry = fn.get("code"), fn.get("entrypoint") or "main"
            f = wf.base / str(code or "")
            if not code or not f.is_file():
                errors.append(f"{where}: curate:function code file not found: {code}")
            else:
                src = f.read_text()
                if not re.search(rf"^def {re.escape(entry)}\(", src, re.M):
                    errors.append(f"{where}: entrypoint {entry}() not in {code}")
                if "faasr_" in src:
                    errors.append(f"{where}: {code} calls the FaaSr API; functions are plain and use only the "
                                  "local paths they are given")
                if INSTALL.search(src):
                    warnings.append(f"{where}: {code} installs packages at run time; put them in the image")
                for m in ABS_PATH.finditer(src):
                    warnings.append(f"{where}: hard-coded absolute path in {code}: {m.group(2)}")
        if not (s.image or image):
            errors.append(f"{where}: no image: add a DockerRequirement (dockerPull) to the workflow's or "
                          "the step's hints")
        after = s.hint("curate:after")
        if after is not None and not (isinstance(after.get("steps"), list)
                                      and all(isinstance(x, str) for x in after["steps"])):
            errors.append(f"{where}: curate:after needs {{steps: [<step>, ...]}}")
        for t in s.after:
            if t == sid or t not in steps:
                errors.append(f"{where}: curate:after names {t!r}, which is not another step")
        ri = s.run_if
        if ri is not None:
            if set(ri) != {"step", "result"} or not isinstance(ri.get("result"), bool):
                errors.append(f"{where}: curate:run_if needs {{step: <step>, result: true|false}}")
            elif ri["step"] == sid or ri["step"] not in steps:
                errors.append(f"{where}: curate:run_if names {ri['step']!r}, which is not another step")
        sec = s.hint("curate:secrets")
        if sec is not None:
            names = sec.get("names")
            if not isinstance(names, list) or not names:
                errors.append(f"{where}: curate:secrets needs {{names: [NAME, ...]}}")
            else:
                bad = [x for x in names if not (isinstance(x, str) and SECRET.match(x))]
                if bad:
                    errors.append(f"{where}: curate:secrets holds names only (UPPER_SNAKE), not {bad}")
        # wiring inside the step
        for name in s.ins:
            if name not in s.op_inputs:
                errors.append(f"{where}: in entry {name} is not an input of its Operation")
        for name, p in s.op_inputs.items():
            type_ok(p.type, f"{where} input {name}", True)
            e = s.ins.get(name)
            if e is None or (not e.sources and e.default is NO_VALUE):
                errors.append(f"{where}: Operation input {name} gets no source and no default")
            elif not e.sources and not _value_ok(p.type, e.default):
                errors.append(f"{where}: the default of {name} does not match type {p.type}")
            if e is not None and e.folder:
                if p.type != "File[]":
                    errors.append(f"{where}: curate:folder input {name} must be of type File[], not {p.type}")
                if name in s.scatter:
                    errors.append(f"{where}: curate:folder input {name} cannot be scattered over")
        for name, p in s.op_outputs.items():
            type_ok(p.type, f"{where} output {name}", False)
            if "__" in name:
                errors.append(f"{where}: output id {name} may not contain '__' (generated file names use it to "
                              "keep each output's files under their own prefix)")
            if _depth(p.type) and not p.axis:
                errors.append(f"{where}: output {name} is {p.type}: name its axis with curate:axis: <axis input> "
                              "(one file per value)")
            elif p.axis and not _depth(p.type):
                errors.append(f"{where}: output {name}: curate:axis is only for File[] outputs")
            elif p.axis and not is_axis(wf, p.axis):
                errors.append(f"{where}: output {name}: curate:axis {p.axis} is not an axis (a string[] or int[] "
                              "workflow input)")
            elif _depth(p.type) > 1:
                errors.append(f"{where}: output {name}: a step writes at most one list (File[]) per output")
            if p.name is not None:
                own = {src for e in s.scatter for src in (s.ins[e].sources if e in s.ins else []) if is_axis(wf, src)}
                own |= {p.axis} if p.axis else set()
                if not isinstance(p.name, str) or not p.name or "/" in p.name or p.name in (".", ".."):
                    errors.append(f"{where}: output {name}: curate:name must be a plain file name")
                else:
                    extra = [a for a in PLACEHOLDER.findall(p.name) if a not in own]
                    if extra:
                        errors.append(f"{where}: output {name}: curate:name {p.name!r} names "
                                      + ", ".join("{%s}" % a for a in extra) + ", which is not one of the output's axes")
            if name not in s.outs:
                errors.append(f"{where}: Operation output {name} is missing from out")
        for name in s.outs:
            if name not in s.op_outputs:
                errors.append(f"{where}: out {name} is not an output of its Operation")
        for name in s.scatter:
            if name not in s.ins:
                errors.append(f"{where}: scatter names {name}, which is not an in entry")
        if len(s.scatter) > 1 and s.scatter_method not in SCATTER_METHODS:
            errors.append(f"{where}: scattering over several inputs needs scatterMethod (dotproduct, "
                          "nested_crossproduct or flat_crossproduct)")

    # dependencies and cycles (data, after, run_if)
    def source_step(src):
        return src.split("/", 1)[0] if "/" in src else None

    deps = {sid: set() for sid in steps}
    for sid, s in steps.items():
        for e in s.ins.values():
            for src in e.sources:
                p = source_step(src)
                if p is None:
                    if src not in wf.inputs:
                        errors.append(f"step {sid}: source {src} is not a workflow input")
                elif p not in steps or src.split("/", 1)[1] not in steps[p].outs:
                    errors.append(f"step {sid}: source {src} is not an output of a step")
                else:
                    deps[sid].add(p)
        deps[sid].update(t for t in s.after if t in steps and t != sid)
        if s.run_if and s.run_if.get("step") in steps and s.run_if.get("step") != sid:
            deps[sid].add(s.run_if["step"])
    order, ready, remaining = [], sorted(k for k, v in deps.items() if not v), {k: set(v) for k, v in deps.items()}
    while ready:
        k = ready.pop(0)
        order.append(k)
        for j, v in remaining.items():
            if k in v:
                v.discard(k)
                if not v and j not in order and j not in ready:
                    ready.append(j)
    if len(order) != len(steps):
        errors.append("cycle among steps (data, curate:after or curate:run_if): "
                      + ", ".join(sorted(set(steps) - set(order))))
    if errors:
        return errors, warnings

    # shapes: what each source carries, in topological order
    shapes: dict[str, Shape] = {}
    for n, i in wf.inputs.items():
        dims, v = [], i.value
        for level in range(_depth(i.type)):  # lengths from the value, outer level first
            dims.append(Dim(n if level == 0 and is_axis(wf, n) else None, len(v) if isinstance(v, list) else None))
            v = v[0] if isinstance(v, list) and v else None
        shapes[n] = Shape(i.type, tuple(dims))
    wanted: dict[str, dict[str, str]] = {}  # source -> {consumer: the axis it pairs on the outer dim}
    misgrouped = []  # (consumer, source, axis it wants outermost, axis that is outermost)

    for sid in order:
        s, where = steps[sid], f"step {sid}"
        in_shape: dict[str, Shape] = {}
        for name, e in s.ins.items():
            if not e.sources:
                continue
            got = [shapes[src] for src in e.sources]
            if len(got) == 1 and e.link_merge != "merge_flattened":
                in_shape[name] = got[0]
                continue
            # merge_flattened (a curate:folder input): every source is T or T[]; the result is T[]
            bases = {_strip(g.type) if _depth(g.type) else g.type for g in got}
            if len(bases) != 1:
                errors.append(f"{where} input {name}: its sources carry different types ({', '.join(g.type for g in got)})")
                continue
            in_shape[name] = Shape(bases.pop() + "[]", (Dim(None, None),))
        # scatter
        copies: tuple = ()
        if s.scatter:
            outer = []
            for name in s.scatter:
                sh = in_shape.get(name)
                if sh is None:
                    errors.append(f"{where}: scatters over {name}, which has no source")
                    continue
                if not _depth(sh.type):
                    errors.append(f"{where}: scatters over {name}, which is a single {sh.type}, not a list")
                    continue
                d = sh.dims[0] if sh.dims else Dim(None, None)
                if d.length is None:
                    errors.append(f"{where}: the fan-out size over {name} is unknown; fan-out sizes come only from "
                                  "workflow inputs (an axis or a File[] with a value)")
                outer.append((name, d, sh))
                in_shape[name] = Shape(_strip(sh.type), sh.dims[1:])
            if len(outer) != len(s.scatter):
                continue
            method = s.scatter_method or "dotproduct"
            if method == "dotproduct":
                lengths = {d.length for _, d, _ in outer}
                labels = {d.label for _, d, _ in outer if d.label}
                if len(lengths) > 1:
                    errors.append(f"{where}: dotproduct pairs lists of different lengths: "
                                  + ", ".join(f"{n} ({d.length})" for n, d, _ in outer))
                if len(labels) > 1:
                    regroup = _regroup(s, outer)
                    if regroup:
                        misgrouped.append((sid, *regroup))
                    else:
                        errors.append(f"{where}: dotproduct pairs different axes ("
                                      + ", ".join(f"{n}: {d.label}" for n, d, _ in outer if d.label)
                                      + "); a list written with curate:axis must be paired with that same axis")
                label = next(iter(labels)) if len(labels) == 1 else None
                copies = (Dim(label, next(iter(lengths)) if len(lengths) == 1 else None),)
                if label:
                    for name, d, _ in outer:
                        for src in s.ins[name].sources:
                            wanted.setdefault(src, {})[sid] = label
            elif method == "nested_crossproduct":
                copies = tuple(d for _, d, _ in outer)
            else:
                n = math.prod(d.length for _, d, _ in outer) if all(d.length for _, d, _ in outer) else None
                copies = (Dim(None, n),)
        # types at the sinks
        for name, sh in in_shape.items():
            want = s.op_inputs.get(name)
            if want and sh.type != want.type:
                errors.append(f"{where} input {name}: receives {sh.type} but its Operation input is {want.type}"
                              + (" (it is scattered over, so the source must be a list of these)"
                                 if name in s.scatter else ""))
        for name, p in s.op_outputs.items():
            inner = (Dim(p.axis, axis_len(p.axis)),) if p.axis else ()
            shapes[f"{sid}/{name}"] = Shape(p.type + "[]" * len(copies), copies + inner)

    # a partial gather in the wrong order, or two consumers needing different groupings
    for sid, src, want, outer_axis in misgrouped:
        others = sorted(c for c, a in wanted.get(src, {}).items() if a == outer_axis)
        if others:
            errors.append(f"{src}: consumers need different groupings ({', '.join(others)} by {outer_axis}, "
                          f"{sid} by {want}); plain CWL cannot regroup without JavaScript")
        else:
            errors.append(f"step {sid}: partial gather of {src}: its producer's scatter must list {want} first "
                          f"(outermost) so {sid} gets one group per {want}; now {outer_axis} is outermost")

    # workflow outputs
    read = {src for s in steps.values() for e in s.ins.values() for src in e.sources}
    for n, o in wf.outputs.items():
        if len(o.sources) != 1:
            errors.append(f"workflow output {n}: needs exactly one outputSource")
            continue
        sh = shapes.get(o.sources[0])
        if sh is None:
            errors.append(f"workflow output {n}: outputSource {o.sources[0]} is not a step output")
            continue
        read.add(o.sources[0])
        if sh.type != o.type:
            errors.append(f"workflow output {n}: its source carries {sh.type} but the output is {o.type}")
        da = o.deliver_as
        if da is None:
            if _depth(o.type):
                errors.append(f"workflow output {n} is a list: give curate:deliver_as with its axes as "
                              "placeholders, e.g. results/part_{axis}.csv")
            continue
        if not isinstance(da, str) or da.startswith("/") or ".." in Path(da).parts:
            errors.append(f"workflow output {n}: curate:deliver_as must be a relative path under deliver/")
            continue
        ph = PLACEHOLDER.findall(da)
        labels = [d.label for d in sh.dims]
        if None in labels:
            errors.append(f"workflow output {n}: its files have no axis to name them by, so curate:deliver_as "
                          "cannot place them")
        elif sorted(ph) != sorted(labels):
            errors.append(f"workflow output {n}: curate:deliver_as {da!r} must hold exactly its axes "
                          + (", ".join("{%s}" % a for a in labels) if labels else "(none)"))
    if not wf.outputs:
        warnings.append("no workflow outputs; nothing will be delivered")
    for sid, s in steps.items():
        for name, p in s.op_outputs.items():
            if p.axis and f"{sid}/{name}" not in read:
                warnings.append(f"step {sid}: output {name} writes one file per {p.axis} that no step reads and "
                                "no output delivers")
    return errors, warnings


def _regroup(s, outer):
    """(source, wanted axis, outermost axis) when a dotproduct pairs an axis with a nested list whose
    outer dimension is another axis but which holds the wanted one further in (a partial gather
    whose producer lists the axes in the other order); else None."""
    labels = {n: d.label for n, d, _ in outer if d.label}
    for n, d, sh in outer:
        others = {lab for m, lab in labels.items() if m != n}
        deeper = [x.label for x in sh.dims[1:]]
        hit = next((a for a in sorted(others) if a in deeper), None)
        if hit and d.label:
            return s.ins[n].sources[0], hit, d.label
    return None


def validate(path, inputs=None):
    """(workflow or None, errors, warnings)."""
    try:
        raw = read_yaml(Path(path))
    except LoadError as e:
        return None, [str(e)], []
    errors = raw_errors(raw)
    if errors:
        return None, errors, []
    try:
        wf = load(path, inputs)
    except LoadError as e:
        return None, [str(e)], []
    errors, warnings = check(wf)
    return wf, errors, warnings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Check a CURATE CWL workflow (docs/CWL_IR.md)")
    ap.add_argument("workflow")
    ap.add_argument("--inputs", help="a CWL inputs file (run values)")
    a = ap.parse_args(argv)
    wf, errors, warnings = validate(a.workflow, a.inputs)
    for w in warnings:
        print("warn :", w)
    for e in errors:
        print("ERROR:", e)
    n = len(wf.steps) if wf else 0
    print(f"{n} steps; {len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
