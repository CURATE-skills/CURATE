"""The plain function contract that every CURATE target runs unchanged.

A node's function is one file with

    def main(inputs, outputs, params) -> bool | None

inputs / outputs map slot names (input1.., output1.., in the spec's order) to LOCAL paths, with
{rank} already filled in. The function never sees a folder, a bucket or a {rank} placeholder:

  "one"     a path (str): an unranked step's file, a file every rank shares, or a ranked
            step's own copy of a {rank} file
  "list"    a list of paths in rank order 1..N: every copy of a {rank} file family, for an
            unranked step reading it (fan-in) or writing it (fan-out, write every path), and for
            a ranked step whose input says read: "all"
  "folder"  (input kind "folder") one local directory holding every workflow file whose name
            starts with the input's prefix (the text before its first {rank}, else the whole
            name); the function lists it with os.listdir

Every declared output path must exist when main returns. params is {"rank": r, "max_rank": N}
for a ranked step and {} otherwise. Secrets: os.environ["NAME"] (names declared in the spec).
Logs: stdout/stderr. Return None, or True/False for a step with conditional edges.

Each target maps its own world onto this call: the FaaSr wrapper (downloads, main, uploads),
the IR runner (curate_ir/runner/curate_run.py) on the files Pegasus stages, and the stub test.
This module holds what they share. Standard library only.
"""
from __future__ import annotations

import ast
import re
from types import SimpleNamespace

RANK = "{rank}"
ARGS = ["inputs", "outputs", "params"]
FAASR_MODULES = {"faasr", "FaaSr_py", "faasr_stubs"}
# Environment variables a function may read that are not credentials.
NOT_SECRETS = {"HOME", "PATH", "TMPDIR", "TMP", "TEMP", "USER", "LANG", "LC_ALL", "PWD", "SHELL", "TZ",
               "MPLBACKEND", "PYTHONPATH", "PYTHONHASHSEED", "OMP_NUM_THREADS"}
_SECRET_RX = [re.compile(r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]*)['\"]\s*\]"),
              re.compile(r"os\.environ\.get\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]"),
              re.compile(r"os\.getenv\(\s*['\"]([A-Z][A-Z0-9_]*)['\"]")]


# ── is this code plain? ──────────────────────────────────────────────────────

def plain_problems(code: str | None) -> list[str]:
    """Why `code` does not follow the plain contract; empty if it does."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError as e:
        return [f"not valid Python: {e}"]
    problems = []
    mains = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    if not mains:
        problems.append("no top-level `def main(inputs, outputs, params)`")
    else:
        a = mains[0].args
        got = [x.arg for x in a.posonlyargs + a.args]
        if got != ARGS or a.vararg or a.kwarg or a.kwonlyargs:
            problems.append(f"main takes ({', '.join(got)}); the contract is main(inputs, outputs, params)")
    api = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id.startswith("faasr_"):
            api.add(n.id)
        elif isinstance(n, ast.Attribute) and n.attr.startswith("faasr_"):
            api.add(n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("faasr_"):
            api.add(n.name)
    if api:
        problems.append(f"uses the FaaSr API ({', '.join(sorted(api))}); a plain function reads and writes "
                        "the local paths in inputs/outputs")
    imports = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imports |= {a.name.split(".")[0] for a in n.names if a.name.split(".")[0] in FAASR_MODULES}
        elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in FAASR_MODULES:
            imports.add(n.module.split(".")[0])
    if imports:
        problems.append(f"imports {', '.join(sorted(imports))}; a plain function imports no workflow system")
    return problems


def is_plain(code: str | None) -> bool:
    return not plain_problems(code)


def secret_names(code: str | None) -> list[str]:
    """UPPER_SNAKE environment variables the code reads (os.environ[...] / .get / os.getenv),
    without the usual non-secret ones (HOME, PATH, MPLBACKEND, ...)."""
    found = []
    for rx in _SECRET_RX:
        for name in rx.findall(code or ""):
            if name not in NOT_SECRETS and name not in found:
                found.append(name)
    return found


# ── how many copies each {rank} file has ─────────────────────────────────────

def prefix(name: str) -> str:
    """A folder input's prefix: the text before its first {rank}, or the whole name."""
    return name.split(RANK, 1)[0]


def node_rank(n) -> int:
    return max(1, int(n.rank or 1))


def plain_read(spec, node_name: str, i: int) -> str:
    """How a ranked plain step reads its input<i>: "all" if the spec says so, else "own"."""
    n = next(x for x in spec.nodes if x.name == node_name)
    return "all" if n.inputs[i - 1].read == "all" else "own"


def family_counts(spec) -> tuple[dict[str, int], dict[tuple[str, int], str], list[str]]:
    """({rank} file -> number of copies, {(ranked reader, input index): "own"|"all"}, problems).

    The count of a {rank} file is its ranked writer's rank, else the rank of the ranked steps
    that read it one copy each (plain_read). Folder inputs do not count: they take whatever
    copies exist."""
    def read_of(c: str, i: int) -> str:
        return plain_read(spec, c, i)
    rank = {n.name: node_rank(n) for n in spec.nodes}
    producers: dict[str, str] = {}
    consumers: dict[str, list[tuple[str, int]]] = {}
    for n in spec.nodes:
        for o in n.outputs:
            producers.setdefault(o.name, n.name)
        for i, inp in enumerate(n.inputs, 1):
            if inp.kind != "folder":
                consumers.setdefault(inp.name, []).append((n.name, i))

    problems: list[str] = []
    mode: dict[tuple[str, int], str | None] = {}
    counts: dict[str, int] = {}
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
                mode[(c, i)] = read_of(c, i)
        per = sorted({rank[c] for c, i in readers if rank[c] > 1 and mode[(c, i)] == "own"})
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
        counts[f] = count
        source = f"ranked step {p} (×{count})" if p and rank[p] > 1 else (p or "the workflow input")
        for c, i in readers:
            if rank[c] <= 1:
                continue
            if mode[(c, i)] == "own" and rank[c] != count:
                problems.append(
                    f"N→M ranked handoff: {source} provides {count} copies of {f}, but ranked step {c} "
                    f"runs {rank[c]} ranks and reads one copy per rank. Pegasus cannot pair them; put an "
                    f"unranked step between them that reads all copies and writes {rank[c]} (like a shuffle).")
    return counts, mode, problems


def folder_conflicts(spec, only=None) -> list[str]:
    """A folder input must only ever hold its own files (the family its name gives), because its
    function lists the folder without filtering by name. So any OTHER workflow file — a step's
    output, or a file another step reads — whose name starts with the folder's prefix is refused:
    on FaaSr it would land in the same S3 prefix and be downloaded into the folder. `only`: the
    steps to check (the plain ones)."""
    files: dict[str, str] = {}
    for n in spec.nodes:
        for o in n.outputs:
            files.setdefault(o.name, f"written by {n.name}")
        for i in n.inputs:
            if i.kind != "folder":
                files.setdefault(i.name, f"read by {n.name}")
    problems = []
    for n in spec.nodes:
        if only is not None and n.name not in only:
            continue
        for idx, i in enumerate(n.inputs, 1):
            if i.kind != "folder":
                continue
            pre = prefix(i.name)
            if not pre:
                problems.append(f"step {n.name}'s folder input input{idx} ({i.name}) has no name before "
                                f"{RANK}, so it would hold every file; give the files a common prefix")
                continue
            for f in sorted(f for f in files if f != i.name and f.startswith(pre)):
                problems.append(f"step {n.name}'s folder input input{idx} ({i.name}) holds every file starting "
                                f"with {pre!r}, so {f} ({files[f]}) would land in it too; rename {f} so it "
                                f"does not start with {pre!r}")
    return problems


def folder_matches(spec, node_name: str, name: str) -> list[str]:
    """The workflow files (spec names, {rank} kept) a folder input of `node_name` holds: every
    file some step writes or reads whose name starts with prefix(name), except the step's own
    outputs and other folder inputs."""
    pre = prefix(name)
    own = {o.name for n in spec.nodes if n.name == node_name for o in n.outputs}
    names = {o.name for n in spec.nodes for o in n.outputs}
    names |= {i.name for n in spec.nodes for i in n.inputs if i.kind != "folder"}
    return sorted(f for f in names if f.startswith(pre) and f not in own)


# ── what each slot of each step receives ─────────────────────────────────────

def io_layout(spec) -> tuple[dict[str, dict], list[str]]:
    """({step: {"rank": N, "inputs": [slot], "outputs": [slot]}}, problems) for a spec whose
    steps follow the plain contract. A slot is {"slot": "input1", "file": <spec name>, "kind":
    "one"|"list"|"folder", "copies": N (list), "prefix" and "matches" (folder)}."""
    counts, _, problems = family_counts(spec)
    layout = {}
    for n in spec.nodes:
        r = node_rank(n)
        ins, outs = [], []
        for i, inp in enumerate(n.inputs, 1):
            slot = {"slot": f"input{i}", "file": inp.name}
            if inp.kind == "folder":
                slot.update(kind="folder", prefix=prefix(inp.name),
                            matches=folder_matches(spec, n.name, inp.name))
            elif RANK in inp.name and (r == 1 or inp.read == "all"):
                slot.update(kind="list", copies=counts.get(inp.name))
            else:
                slot["kind"] = "one"
            ins.append(slot)
        for i, out in enumerate(n.outputs, 1):
            slot = {"slot": f"output{i}", "file": out.name}
            if RANK in out.name and r == 1:
                slot.update(kind="list", copies=counts.get(out.name))
            else:
                slot["kind"] = "one"
            outs.append(slot)
        layout[n.name] = {"rank": r, "inputs": ins, "outputs": outs}
    return layout, problems


def as_spec(data):
    """A spec as this module reads it, from a WorkflowSpec or from its JSON dict (state.json's
    "spec"), for callers without pydantic (stubtest.py). Only the fields used here."""
    if not isinstance(data, dict):
        return data

    def io(x):
        x = x if isinstance(x, dict) else {"name": str(x)}
        return SimpleNamespace(name=x["name"], kind=x.get("kind") or "file", read=x.get("read"))

    nodes = [SimpleNamespace(name=n["name"], rank=n.get("rank") or 1, code=n.get("code"),
                             inputs=[io(i) for i in n.get("inputs") or []],
                             outputs=[io(o) for o in n.get("outputs") or []])
             for n in data.get("nodes") or []]
    return SimpleNamespace(nodes=nodes, edges=data.get("edges") or [])


def producers(spec) -> dict[str, str]:
    """{file name as the spec writes it: the step that writes it}."""
    out = {}
    for n in spec.nodes:
        for o in n.outputs:
            out.setdefault(o.name, n.name)
    return out


def resolve(name: str, rank: int | None) -> str:
    """A file name with {rank} filled in for one rank."""
    return name.replace(RANK, str(rank)) if rank is not None else name
