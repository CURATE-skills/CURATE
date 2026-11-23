# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "rank-bm25==0.2.2"]
# ///
"""Migrate a CURATE catalog's FaaSr-style functions to the plain contract.

  migrate_catalog.py [--catalog-dir D] [--apply] [--manual-fixes DIR] [--no-verify] [--timeout S]

Dry-run by default: prints a JSON report and writes nothing. With --apply it first copies every
entry file it will change to <catalog>/.backup-<UTC timestamp>/ (same relative path), then
writes each one atomically. Both function entries (functions/<uuid>.json) and every step of
workflow entries (workflows/<uuid>.json) are migrated.

Each code block ends up in one of:
  already_plain  nothing to do
  converted      rewritten by the rules below AND output-checked
  fixed          replaced by a hand conversion from --manual-fixes AND output-checked
  manual         left unchanged, with the reason (a pattern the rules don't cover, a failed or
                 impossible output check, or a folder-input conflict in its workflow)

The rewrite is conservative and edits the source in place (comments and layout are kept):
  def <name>(folder, input1.., output1..)  ->  def main(inputs, outputs, params), with
                                              inputN = inputs["inputN"] / outputN = outputs[...]
  faasr_get_file(local_file=L, remote_file=R, remote_folder=…)  ->  _curate_copy(R, L)
  faasr_put_file(local_file=L, remote_file=R, remote_folder=…)  ->  _curate_copy(L, R)
  faasr_log(x)       ->  print(x, flush=True)
  faasr_rank()       ->  {"rank": params["rank"], "max_rank": params["max_rank"]}
  faasr_secret(X)    ->  os.environ[X]
  slot.replace("{rank}", X) / slot.format(rank=X)  ->  slot (one file: the path is already this
                     rank's) or slot[X - 1] (a list: fan-out / fan-in by index)
  {folder} in an f-string  ->  "."
Anything else (faasr_get_folder_list, other uses of `folder`, positional faasr_* arguments, …)
is manual. `_curate_copy` copies unless source and destination are the same path.

Output check (unless --no-verify): for every step of a workflow entry, in dependency order and
every rank, the OLD code runs against a local stand-in for FaaSr's S3 and the NEW code runs as
main(...) on the same input files (the old upstream outputs); the output files must be equal
(PNG: equal pixels). `random` (and numpy, if present) is seeded the same for both. A function
entry is checked through a workflow step with the same code, or on its own if it has no inputs;
otherwise it is manual ("no test inputs"). Each run uses `uv` with the step's dependencies.

Folder inputs: after migration, every workflow entry is checked with
plain_contract.folder_conflicts; a conflicting entry is not written, and the report proposes
renames.

--manual-fixes DIR: one JSON file per hand conversion,
  {"name": step, "old_sha256": sha of the old code, "code": new plain code,
   "inputs": {"input1": {"kind": "folder"}}}   # optional IOSpec changes
applied to every block with that name and exactly that old code.

Prints JSON {"ok": ...}; exit 0 ok, 1 something needs attention (manual or conflicts), 2 bad input.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import plain_contract as PC  # noqa: E402
from catalog import resolve_catalog_dir  # noqa: E402
from curate_models import clean_dependencies  # noqa: E402

PYTHON = "3.13"
COPY_HELPER = '''def _curate_copy(src, dst):
    """Copy a file unless src and dst are the same path (added by migrate_catalog.py)."""
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)


'''


class Manual(Exception):
    """The code uses a pattern the rewrite rules don't cover."""


def sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


# ── the rewrite ──────────────────────────────────────────────────────────────

class _Source:
    """Character offsets for AST positions (whose columns are UTF-8 byte offsets)."""

    def __init__(self, code: str):
        self.code = code
        self.lines = code.splitlines(keepends=True)
        self.starts, n = [], 0
        for ln in self.lines:
            self.starts.append(n)
            n += len(ln)

    def off(self, lineno: int, col: int) -> int:
        line = self.lines[lineno - 1]
        return self.starts[lineno - 1] + len(line.encode()[:col].decode(errors="replace"))

    def span(self, node) -> tuple[int, int]:
        return self.off(node.lineno, node.col_offset), self.off(node.end_lineno, node.end_col_offset)

    def text(self, node) -> str:
        a, b = self.span(node)
        return self.code[a:b]


def _kw(call: ast.Call) -> dict:
    return {k.arg: k.value for k in call.keywords}


def convert(code: str, name: str, kinds: dict[str, str]) -> str:
    """The plain version of FaaSr-style `code` whose entry point is `name`. `kinds` maps each slot
    (input1, output1, …) to "one" | "list" | "folder" (plain_contract.io_layout). Raises Manual."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise Manual(f"not valid Python: {e}") from None
    src = _Source(code)
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    if not fns:
        raise Manual(f"no top-level `def {name}(...)`")
    fn = fns[0]
    args = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    if not args or args[0] != "folder" or fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs:
        raise Manual(f"`def {name}` does not take (folder, input…, output…)")
    slots = args[1:]
    if any(not re.fullmatch(r"(input|output)\d+", a) for a in slots):
        raise Manual(f"`def {name}` has arguments other than folder, inputN, outputN: {slots}")
    if any(k == "folder" for k in (kinds.get(s) for s in slots)):
        raise Manual("a folder input: read through faasr_get_folder_list, which needs a hand conversion")

    edits: list[tuple[int, int, str]] = []
    handled: set[int] = set()  # id() of nodes already rewritten (their insides are not visited)

    def edit(node, text):
        a, b = src.span(node)
        edits.append((a, b, text))
        handled.add(id(node))

    def inside_handled(node, parents) -> bool:
        return any(id(p) in handled for p in parents)

    uses = {"copy": False, "os": False}

    def visit(node, parents):
        if isinstance(node, ast.Call) and not inside_handled(node, parents):
            f = node.func
            if isinstance(f, ast.Name) and f.id in ("faasr_get_file", "faasr_put_file"):
                kw = _kw(node)
                if node.args or not {"local_file", "remote_file"} <= set(kw) or \
                        set(kw) - {"local_file", "remote_file", "remote_folder", "server_name"}:
                    raise Manual(f"{f.id} called with arguments other than local_file=, remote_file=, "
                                 "remote_folder=")
                for v in (kw["local_file"], kw["remote_file"]):
                    for x in ast.walk(v):
                        if (isinstance(x, ast.Name) and (x.id.startswith("faasr_") or x.id == "folder")) or \
                                (isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name)
                                 and x.value.id in slots and x.attr in ("replace", "format")):
                            raise Manual(f"{f.id}: a file argument that itself needs rewriting "
                                         f"(line {node.lineno})")
                loc, rem = src.text(kw["local_file"]), src.text(kw["remote_file"])
                pair = (rem, loc) if f.id == "faasr_get_file" else (loc, rem)
                edit(node, f"_curate_copy({pair[0]}, {pair[1]})")
                uses["copy"] = True
                return
            if isinstance(f, ast.Name) and f.id == "faasr_log":
                kw = _kw(node)
                arg = node.args[0] if len(node.args) == 1 and not kw else kw.get("log_message")
                if arg is None or len(node.args) + len(kw) != 1:
                    raise Manual("faasr_log called with an unexpected signature")
                text = _rewrite_inner(arg)
                edit(node, f"print({text}, flush=True)")
                return
            if isinstance(f, ast.Name) and f.id == "faasr_rank":
                if node.args or node.keywords:
                    raise Manual("faasr_rank called with arguments")
                edit(node, '{"rank": params["rank"], "max_rank": params["max_rank"]}')
                return
            if isinstance(f, ast.Name) and f.id == "faasr_secret":
                if len(node.args) != 1 or node.keywords:
                    raise Manual("faasr_secret called with an unexpected signature")
                edit(node, f"os.environ[{src.text(node.args[0])}]")
                uses["os"] = True
                return
            if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in slots \
                    and f.attr in ("replace", "format"):
                slot = f.value.id
                idx = None
                if f.attr == "replace" and len(node.args) == 2 and not node.keywords and \
                        isinstance(node.args[0], ast.Constant) and node.args[0].value == PC.RANK:
                    idx = node.args[1]
                elif f.attr == "format" and not node.args and [k.arg for k in node.keywords] == ["rank"]:
                    idx = node.keywords[0].value
                if idx is not None:
                    if kinds.get(slot, "one") == "one":
                        edit(node, slot)  # already this rank's path
                    else:
                        if isinstance(idx, ast.Call) and isinstance(idx.func, ast.Name) and \
                                idx.func.id == "str" and len(idx.args) == 1 and not idx.keywords:
                            i = src.text(idx.args[0])
                        else:
                            i = f"int({src.text(idx)})"
                        edit(node, f"{slot}[{i} - 1]")
                    return
        if isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name) and \
                node.value.id == "folder" and not inside_handled(node, parents):
            if node.conversion != -1 or node.format_spec is not None:
                raise Manual("`folder` formatted with a conversion or format spec")
            edit(node, ".")
            return
        if isinstance(node, ast.Name) and node.id.startswith("faasr_") and not inside_handled(node, parents):
            raise Manual(f"uses {node.id}, which has no plain equivalent here")
        visit_children(node, parents)

    def visit_children(node, parents):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.keyword) and child.arg == "remote_folder":
                continue
            visit(child, parents + [node])

    def _rewrite_inner(arg) -> str:
        """The source of a log argument with {folder} replaced (f-strings only)."""
        a0, _ = src.span(arg)
        inner: list[tuple[int, int, str]] = []
        for v in ast.walk(arg):
            if isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name) and v.value.id == "folder":
                if v.conversion != -1 or v.format_spec is not None:
                    raise Manual("`folder` formatted with a conversion or format spec")
                a, b = src.span(v)
                inner.append((a - a0, b - a0, "."))
            elif isinstance(v, ast.Name) and (v.id.startswith("faasr_") or
                                              (v.id == "folder" and not _in_fstring(arg, v))):
                raise Manual(f"log message uses {v.id}")
        text = src.text(arg)
        for a, b, t in sorted(inner, reverse=True):
            text = text[:a] + t + text[b:]
        return text

    def _in_fstring(root, name_node) -> bool:
        return any(isinstance(v, ast.FormattedValue) and v.value is name_node for v in ast.walk(root))

    for stmt in fn.body:
        visit(stmt, [fn])
    for stmt in tree.body:  # module-level code outside the entry point
        if stmt is not fn:
            for n in ast.walk(stmt):
                if isinstance(n, ast.Name) and n.id.startswith("faasr_"):
                    raise Manual(f"{n.id} used outside `def {name}`")

    # leftover `folder` (anything not log text or remote_folder=) cannot be converted by rule
    dropped = {id(k.value) for c in ast.walk(fn) if isinstance(c, ast.Call)
               and isinstance(c.func, ast.Name) and c.func.id in ("faasr_get_file", "faasr_put_file")
               for k in c.keywords if k.arg == "remote_folder"}
    spans = [(a, b) for a, b, _ in edits]
    for n in ast.walk(fn):
        if isinstance(n, ast.Name) and n.id == "folder" and isinstance(n.ctx, ast.Load) and id(n) not in dropped:
            a, b = src.span(n)
            if not any(x <= a and b <= y for x, y in spans):
                raise Manual(f"uses `folder` (line {n.lineno}) outside log text")

    # the header: def <name>(...) [-> T]:  ->  def main(inputs, outputs, params):
    start = src.off(fn.lineno, fn.col_offset)
    m = re.compile(r"def\s+" + re.escape(name) + r"\s*\(").match(code, start)
    if not m:
        raise Manual("cannot find the def header")
    depth, i = 1, m.end()
    while depth and i < len(code):
        depth += {"(": 1, ")": -1}.get(code[i], 0)
        i += 1
    tail = re.compile(r"\s*(?:->[^:]*)?:").match(code, i)
    if not tail:
        raise Manual("cannot find the end of the def header")
    edits.append((start, tail.end(), "def main(inputs, outputs, params):"))

    # bindings: after the docstring, at the body's indentation
    first = fn.body[0]
    has_doc = isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
    anchor = fn.body[1] if has_doc and len(fn.body) > 1 else (None if has_doc else first)
    indent = " " * first.col_offset
    binds = "".join(f'{indent}{s} = {"inputs" if s.startswith("input") else "outputs"}["{s}"]\n' for s in slots)
    if anchor is not None:
        at = src.starts[anchor.lineno - 1]
    else:  # docstring only
        at = src.starts[first.end_lineno - 1] + len(src.lines[first.end_lineno - 1])
    edits.append((at, at, binds))

    # helper and imports
    if uses["copy"]:
        def_line = src.starts[(fn.decorator_list[0].lineno if fn.decorator_list else fn.lineno) - 1]
        edits.append((def_line, def_line, COPY_HELPER))
        uses["os"] = True
    imported = {a.asname or a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names}
    need = [m for m, used in (("os", uses["os"]), ("shutil", uses["copy"])) if used and m not in imported]
    plain_imports = [n for n in tree.body if isinstance(n, ast.Import)]
    any_import = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    for mod in need:  # alphabetically among the plain `import x` lines, else before the first statement
        after = next((n for n in plain_imports if n.names[0].name > mod), None)
        if after is not None:
            at = src.starts[after.lineno - 1]
        elif plain_imports:
            last = plain_imports[-1]
            at = src.starts[last.end_lineno - 1] + len(src.lines[last.end_lineno - 1])
        elif any_import:
            at = src.starts[any_import[0].lineno - 1]
        else:
            doc = tree.body[0] if isinstance(tree.body[0], ast.Expr) and \
                isinstance(getattr(tree.body[0], "value", None), ast.Constant) else None
            target = next((n for n in tree.body if n is not doc), None)
            at = src.starts[target.lineno - 1] if target is not None else len(code)
            edits.append((at, at, f"import {mod}\n\n"))
            continue
        edits.append((at, at, f"import {mod}\n"))

    edits.sort(key=lambda e: (e[0], e[1]))
    for (a1, b1, _), (a2, b2, _) in zip(edits, edits[1:]):
        if a2 < b1:
            raise Manual("overlapping rewrites (nested faasr_* calls)")
    out = code
    for a, b, t in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        out = out[:a] + t + out[b:]
    problems = PC.plain_problems(out)
    if problems:
        raise Manual("the rewrite is not plain: " + "; ".join(problems))
    return out


# ── the output check ─────────────────────────────────────────────────────────

HARNESS = r'''
import json, os, random, shutil, sys
job = json.load(open(sys.argv[1]))
random.seed(job["seed"])
try:
    import numpy
    numpy.random.seed(job["seed"])
except Exception:
    pass
g = {"__name__": "curate_fn", "__file__": job["code_file"]}
if job["mode"] == "old":
    s3 = job["s3"]

    def faasr_get_file(local_file, remote_file, server_name="", local_folder=".", remote_folder="."):
        src = os.path.join(s3, remote_folder, remote_file)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(local_folder, local_file))

    def faasr_put_file(local_file, remote_file, server_name="", local_folder=".", remote_folder="."):
        dst = os.path.join(s3, remote_folder, remote_file)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(os.path.join(local_folder, local_file), dst)

    def faasr_get_folder_list(server_name="", prefix=""):
        keys = []
        for root, _, files in os.walk(s3):
            for f in files:
                k = os.path.relpath(os.path.join(root, f), s3)
                if k.startswith(prefix):
                    keys.append(k)
        return sorted(keys)

    def faasr_rank():
        return {"rank": job["rank"], "max_rank": job["max_rank"]}

    def faasr_log(m):
        print(m, file=sys.stderr, flush=True)

    def faasr_secret(n):
        return os.environ[n]

    for f in (faasr_get_file, faasr_put_file, faasr_get_folder_list, faasr_rank, faasr_log, faasr_secret):
        g[f.__name__] = f
    exec(compile(open(job["code_file"]).read(), job["code_file"], "exec"), g)
    res = g[job["name"]](**job["args"])
else:
    exec(compile(open(job["code_file"]).read(), job["code_file"], "exec"), g)
    res = g["main"](job["inputs"], job["outputs"], job["params"])
simple = res is None or isinstance(res, (bool, int, float, str))
print("CURATE_RETURN " + json.dumps(res if simple else repr(res)))
'''

PNG_COMPARE = r'''
import sys
import numpy as np
from PIL import Image
a, b = (np.asarray(Image.open(p).convert("RGBA")) for p in sys.argv[1:3])
print("same" if a.shape == b.shape and (a == b).all() else "different")
'''


def _uv(deps: list[str]) -> list[str]:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is not on PATH")
    cmd = [uv, "run", "--no-project", "--quiet", "--python", PYTHON]
    for d in deps:
        cmd += ["--with", d]
    return cmd + ["python"]


def _run(job: dict, deps: list[str], work: Path, timeout: int) -> tuple[bool, str, object]:
    work.mkdir(parents=True, exist_ok=True)
    jf = work / f"job-{job['mode']}-{job.get('rank') or job['params'].get('rank', 1)}.json"
    jf.write_text(json.dumps(job))
    hf = work.parent / "harness.py"
    hf.write_text(HARNESS)
    env = {k: v for k, v in os.environ.items()}
    env.setdefault("MPLBACKEND", "Agg")
    try:
        p = subprocess.run(_uv(deps) + [str(hf), str(jf)], cwd=work, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s", None
    m = re.findall(r"^CURATE_RETURN (.*)$", p.stdout, re.M)
    return p.returncode == 0, (p.stdout + p.stderr)[-3000:], json.loads(m[-1]) if m else None


def _same(a: Path, b: Path, timeout: int) -> bool:
    if a.read_bytes() == b.read_bytes():
        return True
    if a.suffix.lower() == ".png":
        try:
            p = subprocess.run(_uv(["numpy", "pillow"]) + ["-c", PNG_COMPARE, str(a), str(b)],
                               capture_output=True, text=True, timeout=timeout)
            return p.stdout.strip() == "same"
        except (subprocess.TimeoutExpired, RuntimeError):
            return False
    return False


def verify_workflow(spec: dict, new_code: dict[str, str], tmp: Path, timeout: int) -> dict[str, dict]:
    """{step: {"ok", "detail"}} for every step in new_code (old vs new on the same inputs).
    Steps not in new_code run old only, to produce inputs for the later ones."""
    sp = PC.as_spec(spec)
    layout, _ = PC.io_layout(sp)
    folder = spec.get("folder") or "workflow_data"
    s3 = tmp / "s3"
    (s3 / folder).mkdir(parents=True, exist_ok=True)
    order = _topo(spec)
    results: dict[str, dict] = {}
    failed: set[str] = set()
    parents = {}
    for e in spec.get("edges") or []:
        parents.setdefault(e["to_node"], set()).add(e["from_node"])
    for n in order:
        node = next(x for x in spec["nodes"] if x["name"] == n)
        lay = layout[n]
        ranks = range(1, lay["rank"] + 1)
        deps = clean_dependencies(node.get("dependencies"))
        if parents.get(n, set()) & failed:
            failed.add(n)
            if n in new_code:
                results[n] = {"ok": False, "detail": "an upstream step failed its old run, so there are no test inputs"}
            continue
        work = tmp / "work" / n
        old_file = work / "old.py"
        work.mkdir(parents=True, exist_ok=True)
        old_file.write_text(node.get("code") or "")
        args = {"folder": folder, **{f"input{i}": x["name"] for i, x in enumerate(node.get("inputs") or [], 1)},
                **{f"output{i}": x["name"] for i, x in enumerate(node.get("outputs") or [], 1)}}
        ok_old, out_old = True, ""
        for r in ranks:
            ok, out, _ = _run({"mode": "old", "code_file": str(old_file), "name": n, "args": args, "s3": str(s3),
                               "rank": r, "max_rank": lay["rank"], "seed": 0}, deps, work / f"old_r{r}", timeout)
            if not ok:
                ok_old, out_old = False, out
                break
        if not ok_old:
            failed.add(n)
            if n in new_code:
                results[n] = {"ok": False, "detail": "the OLD code failed, so there is nothing to compare: "
                                                    + out_old[-800:]}
            continue
        if n not in new_code:
            continue
        new_file = work / "new.py"
        new_file.write_text(new_code[n])
        problems = []
        for r in ranks:
            rr = r if lay["rank"] > 1 else None
            stage, outd = work / f"new_in_r{r}", work / "new_out"
            stage.mkdir(parents=True, exist_ok=True)
            outd.mkdir(exist_ok=True)
            inputs = {}
            for s in lay["inputs"]:
                if s["kind"] == "folder":
                    d = stage / f"dir_{s['slot']}"
                    d.mkdir(exist_ok=True)
                    for f in sorted((s3 / folder).iterdir()):
                        if f.is_file() and f.name.startswith(s["prefix"]):
                            shutil.copyfile(f, d / f.name)
                    inputs[s["slot"]] = str(d)
                    continue
                names = [PC.resolve(s["file"], i) for i in range(1, (s["copies"] or 0) + 1)] \
                    if s["kind"] == "list" else [PC.resolve(s["file"], rr)]
                paths = []
                for name in names:
                    src = s3 / folder / name
                    if src.exists():
                        shutil.copyfile(src, stage / name)
                    else:
                        (stage / name).write_text("")
                    paths.append(str(stage / name))
                inputs[s["slot"]] = paths if s["kind"] == "list" else paths[0]
            outputs = {s["slot"]: ([str(outd / PC.resolve(s["file"], i)) for i in range(1, (s["copies"] or 0) + 1)]
                                   if s["kind"] == "list" else str(outd / PC.resolve(s["file"], rr)))
                       for s in lay["outputs"]}
            params = {"rank": r, "max_rank": lay["rank"]} if lay["rank"] > 1 else {}
            ok, out, _ = _run({"mode": "new", "code_file": str(new_file), "inputs": inputs, "outputs": outputs,
                               "params": params, "seed": 0}, deps, work / f"new_r{r}", timeout)
            if not ok:
                problems.append(f"rank {r}: the NEW code failed: {out[-800:]}")
                break
            for path in (p for v in outputs.values() for p in (v if isinstance(v, list) else [v])):
                new_p, old_p = Path(path), s3 / folder / Path(path).name
                if not new_p.exists():
                    problems.append(f"rank {r}: the new code did not write {new_p.name}")
                elif not old_p.exists():
                    problems.append(f"rank {r}: the old code did not write {new_p.name}")
                elif not _same(old_p, new_p, timeout):
                    problems.append(f"rank {r}: {new_p.name} differs between the old and new code")
        results[n] = {"ok": not problems,
                      "detail": "; ".join(problems) or f"outputs equal on {len(ranks)} rank(s)"}
    return results


def _topo(spec: dict) -> list[str]:
    names = [n["name"] for n in spec["nodes"]]
    deps = {n: set() for n in names}
    for e in spec.get("edges") or []:
        if e["to_node"] in deps:
            deps[e["to_node"]].add(e["from_node"])
    order = []
    while len(order) < len(names):
        ready = [n for n in names if n not in order and deps[n] <= set(order)]
        if not ready:
            raise ValueError("the workflow's edges have a cycle")
        order += ready
    return order


# ── the catalog ──────────────────────────────────────────────────────────────

def load_fixes(folder: Path | None) -> list[dict]:
    if not folder:
        return []
    out = []
    for f in sorted(Path(folder).glob("*.json")):
        d = json.loads(f.read_text())
        if not {"name", "old_sha256", "code"} <= set(d):
            raise ValueError(f"{f}: a manual fix needs name, old_sha256 and code")
        if not PC.is_plain(d["code"]):
            raise ValueError(f"{f}: the fixed code is not plain: {'; '.join(PC.plain_problems(d['code']))}")
        out.append({**d, "file": str(f)})
    return out


def _kinds(spec: dict, name: str) -> dict[str, str]:
    layout, _ = PC.io_layout(PC.as_spec(spec))
    lay = layout[name]
    return {s["slot"]: s["kind"] for s in lay["inputs"] + lay["outputs"]}


def _apply_io(node: dict, changes: dict) -> None:
    for slot, ch in (changes or {}).items():
        kind, i = ("inputs", int(slot[5:])) if slot.startswith("input") else ("outputs", int(slot[6:]))
        node[kind][i - 1].update(ch)


def _diff(old: str, new: str, label: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        f"{label} (old)", f"{label} (new)"))


def propose_renames(spec: dict, problems_of) -> dict[str, str]:
    """For each file a folder input would wrongly hold, a name that no longer starts with its prefix."""
    names = {f["name"] for n in spec["nodes"] for f in (n.get("inputs") or []) + (n.get("outputs") or [])}
    out = {}
    for n in spec["nodes"]:
        for i in n.get("inputs") or []:
            if i.get("kind") != "folder":
                continue
            pre = PC.prefix(i["name"])
            for f in sorted(x for x in names if x != i["name"] and x.startswith(pre) and pre):
                base, ext = os.path.splitext(f)
                rest = base[len(pre):].strip("_-") or "all"
                new = f"{rest}_{pre.strip('_-')}{ext}"
                while new in names or new in out.values():
                    new = "x" + new
                out[f] = new
    return out


def migrate(root: Path, apply: bool = False, fixes_dir: Path | None = None, verify: bool = True,
            timeout: int = 300) -> dict:
    if not root.is_dir():
        return {"ok": False, "catalog_dir": str(root), "error": f"{root} does not exist; nothing to migrate"}
    fixes = load_fixes(fixes_dir)
    files = sorted(root.glob("functions/*.json")) + sorted(root.glob("workflows/*.json"))
    entries = []
    for f in files:
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError) as e:
            entries.append({"file": str(f.relative_to(root)), "error": f"unreadable: {e}"})
            continue
        if "workflow_spec" in data:
            spec = data["workflow_spec"]
            nodes, kind = spec.get("nodes") or [], "workflow"
        else:
            node = data.get("function_spec") or {}
            spec, nodes, kind = {"name": node.get("name", "?"), "nodes": [node], "edges": []}, [node], "function"
        entries.append({"path": f, "file": str(f.relative_to(root)), "kind": kind, "data": data, "spec": spec,
                        "nodes": nodes, "blocks": []})

    # 1. rewrite (or take a hand fix for) every block
    for e in (x for x in entries if "data" in x):
        for node in e["nodes"]:
            code = node.get("code") or ""
            b = {"step": node["name"], "old_sha256": sha256(code)}
            fix = next((x for x in fixes if x["name"] == node["name"] and x["old_sha256"] == b["old_sha256"]), None)
            if not code:
                b.update(status="manual", reason="no code")
            elif PC.is_plain(code):
                b.update(status="already_plain")
            elif fix:
                b.update(status="fixed", new=fix["code"], io=fix.get("inputs") or {}, fix=fix["file"])
            else:
                try:
                    b.update(status="converted", new=convert(code, node["name"], _kinds(e["spec"], node["name"])))
                except Manual as m:
                    b.update(status="manual", reason=str(m))
            if b.get("new") is not None:
                b["diff"] = _diff(code, b["new"], f"{e['file']}:{node['name']}")
            e["blocks"].append(b)

    # 2. the output check, per workflow entry (then reused for function entries with the same code)
    checked: dict[tuple[str, str], dict] = {}  # (step, old sha) -> result
    if verify:
        for e in (x for x in entries if x.get("kind") == "workflow"):
            todo = {b["step"]: b for b in e["blocks"] if b["status"] in ("converted", "fixed")}
            if not todo:
                continue
            spec = json.loads(json.dumps(e["spec"]))
            for n in spec["nodes"]:  # the new IOSpec (e.g. a folder input) as the new code expects
                if n["name"] in todo:
                    _apply_io(n, todo[n["name"]].get("io"))
            with tempfile.TemporaryDirectory(prefix="curate-migrate-") as tmp:
                # spec: the old code, with the new slot kinds; new_code: what replaces it
                res = verify_workflow(spec, {k: b["new"] for k, b in todo.items()}, Path(tmp), timeout)
            for step, r in res.items():
                todo[step]["verified"] = r
                checked[(step, todo[step]["old_sha256"])] = r
        for e in (x for x in entries if x.get("kind") == "function"):
            for b in e["blocks"]:
                if b["status"] not in ("converted", "fixed"):
                    continue
                hit = checked.get((b["step"], b["old_sha256"]))
                if hit is not None:
                    b["verified"] = {**hit, "detail": hit["detail"] + " (checked in a workflow entry)"}
                elif not (e["nodes"][0].get("inputs")):
                    spec = json.loads(json.dumps(e["spec"]))
                    _apply_io(spec["nodes"][0], b.get("io"))
                    with tempfile.TemporaryDirectory(prefix="curate-migrate-") as tmp:
                        b["verified"] = verify_workflow(spec, {b["step"]: b["new"]}, Path(tmp), timeout)[b["step"]]
                else:
                    b["verified"] = {"ok": False, "detail": "no test inputs: no workflow entry runs this code"}
    for e in (x for x in entries if "data" in x):
        for b in e["blocks"]:
            if b["status"] in ("converted", "fixed") and verify and not (b.get("verified") or {}).get("ok"):
                b["proposed"] = b["status"]
                b.update(status="manual", reason="output check failed: " + (b.get("verified") or {}).get(
                    "detail", "not checked"))

    # 3. folder inputs: a migrated workflow must not let another file land in a folder input
    conflicts = []
    for e in (x for x in entries if x.get("kind") == "workflow"):
        spec = json.loads(json.dumps(e["spec"]))
        by = {b["step"]: b for b in e["blocks"]}
        for n in spec["nodes"]:
            b = by[n["name"]]
            if b["status"] in ("converted", "fixed"):
                _apply_io(n, b.get("io"))
                n["code"] = b["new"]
        plain = {n["name"] for n in spec["nodes"] if PC.is_plain(n.get("code"))}
        problems = PC.folder_conflicts(PC.as_spec(spec), only=plain)
        if problems:
            e["conflict"] = True
            conflicts.append({"file": e["file"], "workflow": spec.get("name"), "problems": problems,
                              "proposed_renames": propose_renames(spec, problems)})

    # 4. write (apply only)
    backup, written = None, []
    if apply:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for e in (x for x in entries if "data" in x and not x.get("conflict")):
            changed = [b for b in e["blocks"] if b["status"] in ("converted", "fixed")]
            if not changed:
                continue
            by = {b["step"]: b for b in changed}
            for n in e["nodes"]:
                if n["name"] in by:
                    n["code"] = by[n["name"]]["new"]
                    _apply_io(n, by[n["name"]].get("io"))
            backup = backup or root / f".backup-{stamp}"
            dst = backup / e["file"]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(e["path"], dst)
            tmpf = e["path"].with_suffix(".json.tmp")
            tmpf.write_text(json.dumps(e["data"], indent=2))
            tmpf.replace(e["path"])
            written.append(e["file"])

    report_entries = []
    for e in entries:
        if "data" not in e:
            report_entries.append(e)
            continue
        report_entries.append({"file": e["file"], "kind": e["kind"], "name": e["spec"].get("name"),
                               **({"not_written": "folder-input conflict"} if e.get("conflict") else {}),
                               "blocks": [{k: v for k, v in b.items() if k not in ("new", "io")} for b in e["blocks"]]})
    counts: dict[str, int] = {}
    for e in entries:
        for b in e.get("blocks") or []:
            counts[b["status"]] = counts.get(b["status"], 0) + 1
    needs = bool(counts.get("manual") or conflicts or any("error" in e for e in entries))
    return {"ok": not needs, "catalog_dir": str(root), "mode": "apply" if apply else "dry-run",
            "verified": verify, "summary": counts, "folder_conflicts": conflicts, "entries": report_entries,
            **({"backup_dir": str(backup), "written": written} if apply else {})}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="migrate_catalog.py", description=__doc__.split("\n")[0])
    p.add_argument("--catalog-dir", help="default: as catalog.py (CURATE_CATALOG_DIR, then ~/.curate/catalog)")
    p.add_argument("--apply", action="store_true", help="write (after a backup); default is a dry run")
    p.add_argument("--manual-fixes", help="a folder of hand conversions (JSON, see above)")
    p.add_argument("--no-verify", action="store_true", help="skip the output check (dry run only)")
    p.add_argument("--timeout", type=int, default=300, help="seconds per run of one step")
    a = p.parse_args(argv)
    if a.apply and a.no_verify:
        print(json.dumps({"ok": False, "error": "--apply needs the output check; drop --no-verify"}))
        return 2
    root, _ = resolve_catalog_dir(a.catalog_dir)
    try:
        res = migrate(Path(root), a.apply, Path(a.manual_fixes) if a.manual_fixes else None,
                      not a.no_verify, a.timeout)
    except (ValueError, OSError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps(res, indent=2))
    if "error" in res:
        return 2
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
