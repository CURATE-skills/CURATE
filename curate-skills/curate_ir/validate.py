#!/usr/bin/env python3
"""Load and check a CURATE IR file.

  python3 validate.py path/to/ir.yaml        # exit 0 = valid; warnings printed

Errors make the IR unusable. Warnings are portability risks (the paper's
"Portability" / "Handoff" failure categories): the IR may run here but not
unchanged on another site.

Accepts IR v0.1 and v0.2. v0.2 fan-out (axes / for_each / gather) is expanded
first (expand.py); all checks then run on the expanded nodes.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from expand import expand  # noqa: E402

try:
    import yaml
except ImportError:  # JSON still works
    yaml = None

NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
NODE_ID = re.compile(r"^[a-z0-9_]+$")
ABS_PATH = re.compile(r"(^|[\s'\"=])(/(home|Users|tmp|root|mnt|data|scratch)/[^\s'\"]*)")
# How a node's code is called: plain main(inputs, outputs, params) (runner/curate_run.py), or
# FaaSr-style code with the FaaSr API (runner/curate_faasr_run.py; see IR_SPEC "contract").
CONTRACTS = ("plain", "faasr")
INSTALL = re.compile(r"\b(pip|conda|apt-get|apt|dnf|yum)\s+install\b|subprocess.*\bpip\b")


def load(path):
    path = Path(path)
    text = path.read_text()
    if path.suffix in (".yaml", ".yml"):
        if yaml is None:
            raise SystemExit("PyYAML needed for YAML IR files")
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    base = path.resolve().parent
    data["_base"] = str(base)
    # A location without a scheme is a file next to the IR: make it file://<absolute path>,
    # so examples and emitted run folders work wherever they are checked out or copied.
    for inp in data.get("inputs") or []:
        loc = inp.get("location") if isinstance(inp, dict) else None
        if isinstance(loc, str) and loc and "://" not in loc and not loc.startswith("/"):
            inp["location"] = "file://" + str(base / loc)
    return data


def files_of(value):
    """The file names of an expanded input or output value: a name, a list of names (a gather,
    or an output written once per axis value), or a folder input {"folder": [names]}."""
    if isinstance(value, dict):
        return list(value.get("folder") or [])
    return value if isinstance(value, list) else [value]


def prepare(ir):
    """Expand fan-out and run all checks. Returns (expanded_ir, errors, warnings, deps)."""
    version = ir.get("ir_version")
    if version not in ("0.1", "0.2"):
        return ir, [f"ir_version must be '0.1' or '0.2', got {version!r}"], [], {}
    if version == "0.1" and ("axes" in ir or any("for_each" in n for n in ir.get("nodes", []))):
        return ir, ["axes / for_each need ir_version '0.2'"], [], {}
    exp, exp_errors = expand(ir)
    errors, warnings, deps = check(exp)
    return exp, exp_errors + errors, warnings, deps


def check(ir):
    """Checks on an expanded IR (plain nodes; input values may be lists)."""
    errors, warnings = [], []
    if not NAME.match(str(ir.get("name", ""))):
        errors.append("name must match [a-z0-9-]+")
    base = Path(ir["_base"])
    defaults = ir.get("defaults", {})

    external = {}
    for inp in ir.get("inputs", []):
        f, loc = inp.get("file"), inp.get("location", "")
        if not f or not loc:
            errors.append(f"input needs file and location: {inp}")
            continue
        external[f] = loc
        if loc.startswith("file://"):
            warnings.append(f"input {f} is a submit-host file; fine for Pegasus (it stages it), "
                            f"but FaaSr/cloud-only backends need a URL or s3:// location")

    producers, consumers, ids, seen_origin = {}, {}, set(), set()
    list_outputs = []  # (node id, output name, [files]) for outputs written once per axis value
    faasr_nodes = []  # contract faasr: one warning for all of them, after the loop
    for n in ir.get("nodes", []):
        nid = n.get("id", "")
        origin = n.get("_origin", nid)
        if not NODE_ID.match(origin):
            errors.append(f"bad node id {origin!r}")
        if nid in ids:
            errors.append(f"duplicate node id {nid}")
        ids.add(nid)
        contract = n.get("contract", "plain")
        if contract not in CONTRACTS and origin not in seen_origin:
            errors.append(f"{origin}: contract must be one of {', '.join(CONTRACTS)}, got {contract!r}")
        first = origin not in seen_origin
        seen_origin.add(origin)
        code = base / n.get("code", "")
        if not first:
            pass
        elif not n.get("code") or not code.is_file():
            errors.append(f"{nid}: code file not found: {n.get('code')}")
        else:
            src = code.read_text()
            if f"def {n.get('entrypoint', 'main')}(" not in src:
                errors.append(f"{nid}: entrypoint {n.get('entrypoint', 'main')}() not in {n['code']}")
            if contract == "faasr":
                faasr_nodes.append(origin)
            elif "faasr_" in src:
                errors.append(f"{nid}: code calls FaaSr API; IR functions must use plain file paths "
                              "(or mark the node contract: faasr)")
            if INSTALL.search(src):
                warnings.append(f"{nid}: code installs packages at run time; put them in the image")
            for m in ABS_PATH.finditer(src):
                warnings.append(f"{nid}: hard-coded absolute path in code: {m.group(2)}")
        if first and not (n.get("image") or defaults.get("image")):
            errors.append(f"{nid}: no image and no defaults.image")
        if first:
            for m in ABS_PATH.finditer(json.dumps(n.get("params", {}))):
                warnings.append(f"{origin}: absolute path in params: {m.group(2)}")
        for k, v in n.get("outputs", {}).items():
            if isinstance(v, list):
                list_outputs.append((nid, k, v))
        for f in (f for v in n.get("outputs", {}).values() for f in files_of(v)):
            if f in producers:
                errors.append(f"file {f} produced by both {producers[f]} and {nid}")
            if f in external:
                errors.append(f"file {f} is both a workflow input and produced by {nid}")
            producers[f] = nid
        for k, v in n.get("inputs", {}).items():
            if isinstance(v, dict) and not files_of(v):
                errors.append(f"{nid}: folder input {k} holds no files")
            for f in files_of(v):
                consumers.setdefault(f, []).append(nid)

    if faasr_nodes:
        warnings.append(f"contract faasr ({', '.join(faasr_nodes)}): FaaSr-style code run through "
                        "runner/curate_faasr_run.py; portable only to backends that ship that runner")
    for f, users in consumers.items():
        if f not in producers and f not in external:
            errors.append(f"file {f} (used by {', '.join(users)}) has no producer and is not an input")
    for o in ir.get("outputs", []):
        if o.get("file") not in producers:
            errors.append(f"declared output {o.get('file')} is not produced by any node")
    if not ir.get("outputs"):
        warnings.append("no declared outputs; nothing will be delivered for grading")
    # A list output (an axis the node does not fan out over) that nothing reads or delivers is
    # most likely a misspelled file name: say so instead of silently writing unused copies.
    delivered = {o.get("file") for o in ir.get("outputs", [])}
    for nid, k, files in list_outputs:
        if not any(f in consumers or f in delivered for f in files):
            warnings.append(f"{nid}: output {k} writes {len(files)} files ({', '.join(files[:3])}"
                            f"{', …' if len(files) > 3 else ''}) that no node reads and no output delivers; "
                            "is the file name misspelled?")

    # cycle check (Kahn)
    deps = {n["id"]: {producers[f] for v in n.get("inputs", {}).values()
                      for f in files_of(v) if f in producers}
            for n in ir.get("nodes", []) if "id" in n}
    order, ready = [], [k for k, v in deps.items() if not v]
    remaining = {k: set(v) for k, v in deps.items()}
    while ready:
        k = ready.pop()
        order.append(k)
        for j, v in remaining.items():
            if k in v:
                v.discard(k)
                if not v and j not in order and j not in ready:
                    ready.append(j)
    if len(order) != len(deps):
        errors.append("cycle in dataflow: " + ", ".join(sorted(set(deps) - set(order))))
    return errors, warnings, deps


def main():
    ir = load(sys.argv[1])
    _, errors, warnings, deps = prepare(ir)
    for w in warnings:
        print("warn :", w)
    for e in errors:
        print("ERROR:", e)
    print(f"{len(deps)} nodes, {sum(len(v) for v in deps.values())} edges; "
          f"{len(errors)} error(s), {len(warnings)} warning(s)")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
