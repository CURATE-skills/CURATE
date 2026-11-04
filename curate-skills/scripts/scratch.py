# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "rank-bm25==0.2.2"]
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Partly derived from CURATE-prototype src/faasr_agents/faasr/context_dir.py and
# agents/fga.py (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140): test-data
# seeding, the CONTEXT.md / spec.md text and the deps/secrets harvest are adapted
# copies. The lock, snapshot and verify code is new. See scripts/VENDORED.md.
"""Scratch directory, MGA lock, write verification and harvest for /curate.

Layout built under <run>/:
  CONTEXT.md                 shared workflow context for every MGA subagent
  stubs/faasr_stubs.py       local FaaSr API stubs
  context/                   copies of user reference files
  functions/<node>/          one folder per node — the only place its MGA may write
      spec.md  <node>.py  <node>.deps.txt  original_<node>.py
      test_data/  test_output/  parity_report.txt  stubtest.log

  scratch.py build         --run R [--resume]
  scratch.py lock          --run R --node N [--force]
  scratch.py unlock        (--run R | --project-dir D)
  scratch.py snapshot      --run R --node N
  scratch.py verify-writes --run R --node N
  scratch.py harvest       --run R --node N
  scratch.py record        --run R --node N --status S [--attempts K] [--note T]

Prints JSON {"ok": ...}; exit 0 ok, 1 check failed, 2 bad input.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import itertools
import json
import os
import re
import shutil
import sys
from pathlib import Path

from curate_models import FunctionSpec, WorkflowSpec, clean_dependencies
from runstate import node_basis, run_lock
from spec import InputError, load_state, save_state, spec_of, dump, topo_order

SCRIPTS = Path(__file__).resolve().parent
STUBTEST = SCRIPTS / "stubtest.py"
LARGE_CSV_BYTES = 512 * 1024
MGA_STATUSES = ("pending", "passed", "failed", "external_only", "reused", "reused_failed")
DONE_STATUSES = {"passed", "external_only", "reused", "reused_failed"}
SKIP_DIRS = {".git", ".curate", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
             ".mypy_cache", ".tox"}
PROJECT_FILE_CAP = 50000


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def node_dir(run: Path, name: str) -> Path:
    return run / "functions" / name


def needs_generation(n: FunctionSpec) -> bool:
    return not (n.source in ("catalog", "cached") and n.code)


def stubtest_cmd(run: Path, name: str, plugin: str = "<plugin>") -> str:
    """The stub-test command for a function. spec.md stores it with a literal `<plugin>` (no
    absolute, versioned plugin path in the run); the spawn prompt fills in the plugin folder.
    Its form, `uv run --quiet <plugin>/scripts/...`, is the one the README's allow rule and the
    skills' allowed-tools match."""
    return f'uv run --quiet {plugin}/scripts/stubtest.py run --run "{run}" --node {name}'


# ── test data seeding (context_dir.py _seed_real_input / _write_test_data) ──

def _seed_real_input(dst: Path, src: str) -> bool:
    try:
        if dst.suffix.lower() == ".csv" and os.path.getsize(src) > LARGE_CSV_BYTES:
            with open(src, "r", errors="replace") as f:
                dst.write_text("".join(itertools.islice(f, 501)))  # header + 500 rows
        else:
            shutil.copy2(src, dst)
        return True
    except OSError:
        return False


def seed_test_data(test_data: Path, node: FunctionSpec, context_files: list[dict]) -> list[str]:
    """Copy user files whose names match this node's declared inputs. Never fabricate."""
    seeded = []
    for inp in node.inputs:
        if "{rank}" in inp.name:
            pat = re.compile("^" + re.escape(inp.name).replace(r"\{rank\}", r"\d+") + "$")
            matches = [c for c in context_files if pat.match(c["name"])]
        else:
            matches = [c for c in context_files if c["name"] == inp.name]
        for c in matches:
            dst = test_data / c["name"]
            if not dst.exists() and _seed_real_input(dst, c["path"]):
                seeded.append(c["name"])
    return seeded


# ── CONTEXT.md / spec.md (adapted from context_dir.py 251-569) ───────────────

def signature_and_test_call(spec: FunctionSpec) -> tuple[str, str]:
    sig = (["folder: str"] + [f"input{i + 1}: str" for i in range(len(spec.inputs))]
           + [f"output{i + 1}: str" for i in range(len(spec.outputs))])
    args = (["'test_folder'"] + [f"'{i.name}'" for i in spec.inputs]
            + [f"'{o.name}'" for o in spec.outputs])
    return f"def {spec.name}({', '.join(sig)}) -> None", f"{spec.name}({', '.join(args)})"


def shared_context_md(run: Path, wf: WorkflowSpec, state: dict) -> str:
    request = (state.get("user_request") or "").strip()
    request_block = ""
    if request:
        request_block = (
            f"\n## Original User Request (verbatim)\n{request}\n\n"
            "Follow any explicit implementation details above that apply to the step you are "
            "implementing exactly as written — never substitute your own defaults for something "
            "the user specified. Details about other steps belong to those steps.\n")
    ctx_block = ""
    if state.get("context_files"):
        listing = "\n".join(f"  - {run}/context/{c['name']}"
                            + (f" — {c['description']}" if c.get("description") else "")
                            for c in state["context_files"])
        ctx_block = (
            "\n## [User Provided Context] — Reference Files\n"
            "The user attached these files as domain reference material (papers, input/output "
            "examples, sample datasets). READ the relevant ones to match real data formats, "
            "parameter values and domain details — but they are REFERENCES, not code to import or "
            f"execute, and not a license to fabricate data:\n{listing}\n"
            "Files whose names match a function's declared inputs have already been placed in that "
            "function's test_data/ as the REAL local test inputs. Stub runs MUST use them and pass.\n")
    mode = state.get("stubtest_mode", "strict")
    mode_note = ("Every rank 1..N is run, your <node>.deps.txt is installed into an isolated env, "
                 "test_output/ is cleared first, and every declared output must be uploaded."
                 if mode == "strict" else
                 "Only rank 1 is run and dependencies are NOT installed (parity with the original "
                 "CURATE); a ModuleNotFoundError for a package listed in <node>.deps.txt is tolerated.")
    return f"""\
# Workflow: {wf.name}
{wf.description}

Run directory: `{run}`

## All Workflow Functions (topological order)
{', '.join(topo_order(wf))}

Each function is implemented by its own curate-mga subagent, in the order above. A function's
full specification is in `{run}/functions/<function>/spec.md`.
{request_block}
## Where you may write
Only inside YOUR function's folder, `{run}/functions/<your function>/`. Everything else —
other functions' folders, this file, state.json, the catalog — is read-only. Writes elsewhere
are blocked and fail the attempt.

## Data Integrity (non-negotiable)
This is a REAL scientific workflow. Use the real data source / API named in each spec.
NEVER fabricate, mock, randomize, hardcode, or fall back to synthetic data when a
source is unavailable — if it cannot be reached, faasr_log the error and raise.

## Neighbor Code (`{run}/functions/*/`)
Every function that already exists (catalog/cached reuse, seeds, user-provided originals
`original_<name>.py`, and functions implemented earlier in this run) lives in its own folder.
MATCH filenames against it: a function's input filenames MUST equal the exact remote_file each
producer passes to faasr_put_file, and its output filenames MUST equal what each consumer
passes to faasr_get_file. The Inputs/Outputs in each spec are starting names — reconcile to the
real code when they differ. CATALOG functions are FIXED (their filenames cannot change), so
conform to a catalog neighbor's names exactly.

## FaaSr API
See the faasr-reference skill. The final file must contain NO faasr import of any kind and
must call faasr_get_file, faasr_put_file, faasr_log, faasr_secret, faasr_rank and
faasr_get_folder_list as bare names.

## Testing with Stubs
Run your function through the local stubs with the exact command in your spec.md
("Local Stub Test"). {mode_note}
The stub faasr_get_file reads your test_output/, then your test_data/, then the test_output/
of every upstream function — so you are tested on the REAL outputs upstream functions produced.

YOU ARE NOT DONE UNTIL THIS RUNS CLEANLY on representative inputs:
- A PURE-LOCAL function (parse / validate / transform / simulate on the given inputs / plot) has
  NO external dependency, so the stub run MUST complete without error. A parse/validation/logic
  error is a REAL DEFECT — fix the code and re-run. Do NOT rationalize a failure as "expected
  placeholder data."
- If a matching input is present (your test_data/ or an upstream test_output/), you MUST run
  against it and get a clean pass — never swap in dummy data.
- If an input file is ABSENT everywhere, construct a representative input (matching the documented
  schema / a context reference file) in YOUR test_data/, then verify a clean run. Fabricating data
  INSIDE the function is still forbidden (see Data Integrity).
- The ONLY acceptable local failure is a genuine EXTERNAL fetch (API / URL / database) that needs
  network or credentials unavailable here — and only that. Everything else must pass.
{ctx_block}"""


def node_spec_md(run: Path, spec: FunctionSpec, wf: WorkflowSpec, feedback: str,
                 seeded: list[str]) -> str:
    signature, test_call = signature_and_test_call(spec)
    me = node_dir(run, spec.name)
    inputs_lines = "\n".join(
        f'  input{i + 1} = "{inp.name}"  # type: {inp.type or "any"}'
        + (f" — {inp.description}" if inp.description else "")
        for i, inp in enumerate(spec.inputs)) or "  (none)"
    outputs_lines = "\n".join(
        f'  output{i + 1} = "{out.name}"  # type: {out.type or "any"}'
        + (f" — {out.description}" if out.description else "")
        for i, out in enumerate(spec.outputs)) or "  (none)"
    incoming = "\n".join(f"  '{d.file}' ({d.format or 'any'}) ← from {d.from_node} "
                         f"(code: {node_dir(run, d.from_node)}/{d.from_node}.py)"
                         for d in wf.data_flow if d.to_node == spec.name) \
        or "  (none — this is a source node)"
    outgoing = "\n".join(f"  '{d.file}' ({d.format or 'any'}) → to {d.to_node}"
                         for d in wf.data_flow if d.from_node == spec.name) \
        or "  (none — this is a sink node, write final outputs)"
    deps = ", ".join(spec.dependencies) if spec.dependencies else "none"
    secrets_block = "\n".join(f'  - {s}   →   read with faasr_secret("{s}")'
                              for s in spec.secrets) or "  (none)"

    rank_by_name = {n.name: n.rank or 1 for n in wf.nodes}
    rank_block = ""
    if spec.rank > 1:
        rank_block += (
            f"\n## Parallel Execution (RANK = {spec.rank})\n"
            f"This function runs as {spec.rank} parallel instances. Call `r = faasr_rank()` "
            f"(returns {{'rank': i, 'max_rank': N}}) and use r['rank'] (1..{spec.rank}) to process "
            "ONLY this instance's shard. The input/output names above contain a {rank} placeholder "
            "— substitute r['rank'] before calling faasr_get_file / faasr_put_file "
            "(e.g. input1.format(rank=r['rank'])).\n")
    for s in (e.to_node for e in wf.edges if e.from_node == spec.name):
        n = rank_by_name.get(s, 1)
        if n <= 1:
            continue
        handed = [d.file for d in wf.data_flow if d.from_node == spec.name and d.to_node == s]
        if handed and not any("{rank}" in f for f in handed):
            # A single shared file (no {rank}): every instance reads the same object.
            rank_block += (
                f"\n## Ranked successor `{s}` (×{n}) reads one shared file\n"
                f"`{s}` runs as {n} parallel instances, and ALL of them read the same "
                f"{', '.join(repr(f) for f in handed)}. Write it exactly once — do not shard it. "
                "THIS function is NOT ranked: do NOT call faasr_rank().\n")
            continue
        rank_block += (
            f"\n## Fan-out to ranked successor `{s}` (×{n})\n"
            f"`{s}` runs as {n} parallel instances, so you MUST write exactly {n} output shards — "
            "one per instance. THIS function is NOT ranked: do NOT call faasr_rank() to get this "
            "count (it reports YOUR own rank, which is 1 here). "
            f"Hardcode the count as {n} and substitute {{rank}} with 1..{n}, e.g.\n"
            f"    for i in range(1, {n + 1}):\n"
            f"        faasr_put_file(local_file=..., remote_folder=folder, "
            f"remote_file=output1.replace(\"{{rank}}\", str(i)))\n")
    for p in (e.from_node for e in wf.edges if e.to_node == spec.name):
        n = rank_by_name.get(p, 1)
        if n > 1:
            rank_block += (
                f"\n## Fan-in from ranked predecessor `{p}` (×{n})\n"
                f"You run ONCE after all {n} instances of `{p}` finish. Read ALL of its per-rank "
                "outputs: discover them with faasr_get_folder_list(prefix=...) and faasr_get_file each "
                "one (do not assume a fixed count or use boto3). faasr_get_folder_list returns FULL "
                "object keys including the folder prefix; pass only the basename to faasr_get_file: "
                "remote_file=name.rsplit(\"/\", 1)[-1].\n")

    feedback_block = (f"\n## Feedback from Code Review\n{feedback}\n"
                      "\nAddress all points above in your implementation.\n") if feedback else ""
    user = spec.source == "user_provided"
    prior_block = ""
    if spec.code and not user and spec.source != "new":
        prior_block = (f"\n## Previous Implementation\nSee `{me}/{spec.name}.py` (seeded from "
                       f"{spec.origin_kind or 'a previous round'}"
                       f"{' `' + spec.origin_name + '`' if spec.origin_name else ''}) — make the "
                       "smallest change that addresses the requirements while preserving everything else.\n")
    run_original = (f'uv run --no-project --python 3.13 --with-requirements {spec.name}.deps.txt '
                    f'python original_{spec.name}.py')
    parity_block = ""
    if user and spec.user_model_mode == "verbatim":
        parity_block = (
            "\n## [User Provided Model] — Wrap Verbatim (REQUIRED)\n"
            f"`{me}/original_{spec.name}.py` is a user-provided Python **script or function**. The "
            "user's model code itself must NOT change — your job is only to wrap it for FaaSr.\n"
            "1. Read it and work out WHAT it computes — its real inputs (files it reads, CLI/argparse "
            "args, hardcoded paths, stdin) and outputs (files written, stdout, return value).\n"
            f"2. Copy the model's code into `{spec.name}.py` UNCHANGED — no renaming, no "
            "algorithm/constant/output-format changes. (If it is a plain script, you may enclose its "
            "top-level logic in a function so it is callable, but every statement's logic must be "
            f"preserved.) Then add the FaaSr entry function `{spec.name}` with the signature above: "
            "download inputs via faasr_get_file, convert formats as needed, CALL the model, and upload "
            "its results via faasr_put_file, matching the declared I/O filenames.\n"
            "3. The entry function must produce its results ONLY by calling the preserved model "
            "function(s) — never by reimplementing or duplicating any model logic in the wrapper body.\n"
            f"4. List the original's third-party imports in `{spec.name}.deps.txt` so it is runnable "
            f"(run the original with `{run_original}` from your folder).\n"
            "5. VERIFY, exercising the WRAPPED entry function (not the model directly):\n"
            "   (a) PARITY — run the original on representative synthetic inputs to capture baseline "
            "outputs; run the entry function on the equivalent inputs via the stub test; compare.\n"
            "   (b) INVOCATION — prove the entry function actually executes the model: temporarily "
            "instrument the model function (e.g. a call counter or sentinel), assert it fired when the "
            "entry function ran, then REMOVE the instrumentation so the shipped code is untouched.\n"
            f"6. Record both checks in `{me}/parity_report.txt`. If the outputs cannot be made to "
            "match without changing the model, faasr_log the discrepancy and FAIL — never fabricate "
            "data and never modify the model to force a pass.\n")
    elif user:
        parity_block = (
            "\n## [User Provided Model] — Refactor & Verify Parity (REQUIRED)\n"
            f"`{me}/original_{spec.name}.py` is a user-provided Python **script or function** (it may "
            "be a plain script: top-level statements, argparse, hardcoded paths, an "
            "`if __name__ == \"__main__\"` block).\n"
            "1. Read it and work out WHAT it computes — its real inputs and outputs.\n"
            f"2. Refactor that logic into the FaaSr function `{spec.name}.py` with the signature above, "
            "reading inputs via faasr_get_file and writing outputs via faasr_put_file, matching the "
            "declared I/O filenames. Apply EXACTLY the changes the workflow requirements and feedback "
            "call for — everything else must preserve the original computation.\n"
            f"3. List the original's third-party imports in `{spec.name}.deps.txt` "
            f"(run the original with `{run_original}`).\n"
            "4. VERIFY PARITY: construct representative synthetic inputs; RUN the original to capture "
            "its baseline outputs; run your FaaSr translation on the equivalent inputs via the stub "
            "test; compare the outputs.\n"
            f"5. Iterate until they match apart from the required changes, then write `{me}/"
            "parity_report.txt` noting what you compared, that they agree, and which deviations are "
            "intentional. If they cannot be made to match, faasr_log the discrepancy and FAIL — never "
            "fabricate data to force a pass.\n")
    seeded_note = ""
    if seeded:
        seeded_note = (f"\n## Real Test Inputs (user-provided)\nThese user files match this function's "
                       f"declared inputs and are in `{me}/test_data/`: {', '.join(seeded)}. Your stub run "
                       "MUST use them and pass — do not test against placeholder data.\n")
    return f"""\
# Function: `{spec.name}`
{spec.description}

Your folder (the only place you may write): `{me}`

## Function Signature
```python
{signature}
```

## Inputs  (S3 filenames — pass as `remote_file` to faasr_get_file)
{inputs_lines}

## Outputs  (S3 filenames — pass as `remote_file` to faasr_put_file)
{outputs_lines}
{rank_block}
## PyPI Dependencies (third-party packages only — NOT faasr, NOT stdlib)
{deps}
Write the packages your code imports, one per line, to `{me}/{spec.name}.deps.txt`
(empty file if none; prefer headless variants such as opencv-python-headless).

## Required Secrets (credentials / API tokens)
Read each one with faasr_secret("NAME") — never hardcode, and raise if missing.
If you discover this function needs a credential not listed here, call
faasr_secret("NEW_NAME") with an UPPER_SNAKE name — it is detected automatically.
{secrets_block}

## Data Flow  (MATCH these filenames to the real code of neighbors)
### Incoming files (produced by upstream functions):
{incoming}

### Outgoing files (consumed by downstream functions):
{outgoing}

## Local Stub Test
Test call: `{test_call}`
Run it with exactly this, where `<plugin>` is the plugin folder given in your spawn prompt
(your spawn prompt repeats the command with it filled in):
```bash
{stubtest_cmd(run, spec.name)}
```
{seeded_note}{parity_block}{feedback_block}{prior_block}"""


# ── build ────────────────────────────────────────────────────────────────────

def cmd_build(args) -> dict:
    run = Path(args.run).resolve()
    s = load_state(run)
    wf = spec_of(s.get("spec"))
    if wf is None:
        raise InputError("no resolved spec yet")
    (run / "stubs").mkdir(exist_ok=True)
    shutil.copy2(SCRIPTS / "faasr_stubs.py", run / "stubs" / "faasr_stubs.py")
    ctx = run / "context"
    ctx.mkdir(exist_ok=True)
    warnings = []
    for c in s.get("context_files") or []:
        try:
            shutil.copy2(c["path"], ctx / c["name"])
        except OSError as e:
            warnings.append(f"skipped reference file {c['name']!r}: {e}")

    # The spec is the source of truth: rebuild every node folder from it. On
    # --resume, folders of nodes that already finished are kept as they are.
    fns = run / "functions"
    old_mga = s.get("mga") or {}
    keep = {k for k, v in old_mga.items() if v.get("status") in DONE_STATUSES} if args.resume else set()
    if keep:
        for d in fns.iterdir() if fns.is_dir() else []:
            if d.name not in keep:
                shutil.rmtree(d, ignore_errors=True)
    else:
        shutil.rmtree(fns, ignore_errors=True)
    fns.mkdir(exist_ok=True)
    feedback = (s.get("code_feedback") or "").strip()
    mga: dict = {}
    nodes_out = []
    for name in topo_order(wf):
        n = next(x for x in wf.nodes if x.name == name)
        d = node_dir(run, name)
        if name in keep:
            mga[name] = old_mga[name]
            nodes_out.append({"name": name, "generate": False, "source": n.source, "seeded": [],
                              "kept": old_mga[name]["status"]})
            continue
        (d / "test_data").mkdir(parents=True)
        (d / "test_output").mkdir()
        # Code goes into the folder only when the function is meant to start from it:
        # reused (catalog/cached), rewritten (adapt), or a user model's original. A function
        # marked new starts empty, even if old code is around (e.g. after a rewind).
        if n.code and n.source != "new":
            target = d / (f"original_{name}.py" if n.source == "user_provided" else f"{name}.py")
            target.write_text(n.code)
        if not needs_generation(n):
            (d / f"{name}.deps.txt").write_text("".join(f"{x}\n" for x in clean_dependencies(n.dependencies)))
        seeded = seed_test_data(d / "test_data", n, s.get("context_files") or [])
        generate = needs_generation(n)
        if generate:
            (d / "spec.md").write_text(node_spec_md(run, n, wf, feedback, seeded))
        mga[name] = {"status": "pending" if generate else "reused", "attempts": 0,
                     "last_output_tail": "", "violations": []}
        nodes_out.append({"name": name, "generate": generate, "source": n.source, "seeded": seeded})
    (run / "CONTEXT.md").write_text(shared_context_md(run, wf, s))
    s["mga"] = mga
    s["stage"] = "generate"
    save_state(run, s)
    return {"ok": True, "order": [x["name"] for x in nodes_out], "nodes": nodes_out,
            "warnings": warnings}


# ── lock ─────────────────────────────────────────────────────────────────────

def lock_path(project: Path) -> Path:
    return project / ".curate" / "mga.lock.json"


def cmd_lock(args) -> dict:
    run = Path(args.run).resolve()
    s = load_state(run)
    if not any(n["name"] == args.node for n in (s.get("spec") or {}).get("nodes", [])):
        raise InputError(f"node {args.node!r} is not in the spec")
    lp = lock_path(Path(s["project_dir"]))
    if lp.exists() and not args.force:
        held = json.loads(lp.read_text())
        return {"ok": False, "error": "an MGA lock is already held", "lock": held}
    allowed = node_dir(run, args.node)
    allowed.mkdir(parents=True, exist_ok=True)
    lock = {"run_id": s["run_id"], "run_dir": str(run), "node": args.node,
            "allowed_dir": str(allowed.resolve()), "ts": _now()}
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(json.dumps(lock, indent=2) + "\n")
    return {"ok": True, "lock": lock}


def cmd_unlock(args) -> dict:
    if args.run:
        project = Path(load_state(Path(args.run))["project_dir"])
    elif args.project_dir:
        project = Path(args.project_dir).resolve()
    else:
        raise InputError("pass --run or --project-dir")
    lp = lock_path(project)
    existed = lp.exists()
    if existed and args.run:
        # Only this run's lock: another run's subagent may still be working.
        try:
            held = json.loads(lp.read_text()).get("run_id")
        except (OSError, ValueError):
            held = None
        mine = load_state(Path(args.run))["run_id"]
        if held and held != mine:
            return {"ok": True, "removed": False, "held_by": held,
                    "note": f"the MGA lock belongs to run {held}; left in place"}
    lp.unlink(missing_ok=True)
    return {"ok": True, "removed": existed}


# ── snapshot / verify-writes ─────────────────────────────────────────────────

def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk(root: Path, skip: set[Path], skip_names: set[str] = frozenset()):
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in skip_names and (dp / d) not in skip]
        for f in filenames:
            yield dp / f


def manifest(run: Path, node: str, state: dict) -> dict:
    own = node_dir(run, node).resolve()
    snaps = (run / "snapshots").resolve()
    out = {"run": {}, "catalog": {}, "project": {}, "truncated": False}
    for p in _walk(run.resolve(), {own, snaps}, {"__pycache__"}):
        out["run"][str(p)] = _sha(p)
    cat = Path(state["catalog_dir"])
    if cat.is_dir():
        for p in _walk(cat.resolve(), set()):
            out["catalog"][str(p)] = _sha(p)
    project = Path(state["project_dir"]).resolve()
    for i, p in enumerate(_walk(project, set(), SKIP_DIRS)):
        if i >= PROJECT_FILE_CAP:
            out["truncated"] = True
            break
        try:
            st = p.stat()
            out["project"][str(p)] = [st.st_size, st.st_mtime_ns]
        except OSError:
            pass
    return out


def cmd_snapshot(args) -> dict:
    run = Path(args.run).resolve()
    s = load_state(run)
    m = manifest(run, args.node, s)
    (run / "snapshots").mkdir(exist_ok=True)
    (run / "snapshots" / f"{args.node}.json").write_text(json.dumps(m))
    return {"ok": True, "files": {k: len(m[k]) for k in ("run", "catalog", "project")},
            "project_truncated": m["truncated"]}


def cmd_verify_writes(args) -> dict:
    run = Path(args.run).resolve()
    s = load_state(run)
    snap = run / "snapshots" / f"{args.node}.json"
    if not snap.exists():
        raise InputError(f"no snapshot for {args.node}; run snapshot first")
    before = json.loads(snap.read_text())
    after = manifest(run, args.node, s)
    violations = []
    for area in ("run", "catalog", "project"):
        b, a = before[area], after[area]
        for path in sorted(set(a) - set(b)):
            violations.append({"area": area, "change": "added", "path": path})
        for path in sorted(set(b) - set(a)):
            violations.append({"area": area, "change": "removed", "path": path})
        for path in sorted(set(a) & set(b)):
            if a[path] != b[path]:
                violations.append({"area": area, "change": "modified", "path": path})
    s.setdefault("mga", {}).setdefault(args.node, {})["violations"] = violations
    save_state(run, s)
    return {"ok": not violations, "violations": violations}


# ── harvest / record (fga.py 927-981) ────────────────────────────────────────

def extract_secrets(code: str) -> list[str]:
    found: list[str] = []
    for raw in re.findall(r"faasr_secret\(\s*['\"]([^'\"]+)['\"]", code):
        if raw not in found:
            found.append(raw)
    return found


def read_reported_deps(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = [ln.strip() for ln in path.read_text().splitlines()]
    return clean_dependencies([ln for ln in lines if ln and not ln.startswith("#")])


def cmd_harvest(args) -> dict:
    run = Path(args.run).resolve()
    s = load_state(run)
    wf = spec_of(s.get("spec"))
    if wf is None:
        raise InputError("no spec yet")
    d = node_dir(run, args.node)
    impl = d / f"{args.node}.py"
    if not impl.exists():
        return {"ok": False, "error": f"{impl} was not produced"}
    if args.node not in {n.name for n in wf.nodes}:
        raise InputError(f"node {args.node!r} is not in the spec")
    code = impl.read_text()
    harvested: dict = {}
    nodes = []
    for n in wf.nodes:
        if n.name == args.node:
            secrets = list(n.secrets) + [x for x in extract_secrets(code) if x not in n.secrets]
            deps_file = d / f"{n.name}.deps.txt"
            # Dependencies = what the agent reported (it knows import → package names).
            n = n.model_copy(update={"code": code, "secrets": secrets,
                                     "dependencies": read_reported_deps(deps_file)})
            harvested = {"lines": len(code.splitlines()), "secrets": secrets,
                         "dependencies": n.dependencies, "deps_file": deps_file.exists()}
        nodes.append(n)
    s["spec"] = dump(wf.model_copy(update={"nodes": nodes}))
    s["validation"] = None  # the code changed, so any earlier bundle no longer matches
    save_state(run, s)
    return {"ok": True, "node": args.node, **harvested}


def cmd_record(args) -> dict:
    run = Path(args.run).resolve()
    s = load_state(run)
    if args.status not in MGA_STATUSES:
        raise InputError(f"--status must be one of {MGA_STATUSES}")
    entry = s.setdefault("mga", {}).setdefault(args.node, {"attempts": 0, "violations": []})
    entry["status"] = args.status
    if args.attempts is not None:
        entry["attempts"] = args.attempts
    log = node_dir(run, args.node) / "stubtest.log"
    entry["last_output_tail"] = log.read_text()[-6000:] if log.exists() else ""
    if args.note:
        entry["note"] = args.note
    node = next((n for n in (s.get("spec") or {}).get("nodes", []) if n["name"] == args.node), None)
    if node is not None:
        entry["basis"] = node_basis(node)  # lets emit detect a spec change after generation
    save_state(run, s)
    return {"ok": True, "node": args.node, "status": args.status, "attempts": entry.get("attempts", 0)}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="scratch.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("build")
    sp.add_argument("--run", required=True)
    sp.add_argument("--resume", action="store_true", help="keep folders of finished nodes")
    for name in ("lock", "snapshot", "verify-writes", "harvest", "record"):
        sp = sub.add_parser(name)
        sp.add_argument("--run", required=True)
        sp.add_argument("--node", required=True)
        if name == "lock":
            sp.add_argument("--force", action="store_true")
        if name == "record":
            sp.add_argument("--status", required=True)
            sp.add_argument("--attempts", type=int)
            sp.add_argument("--note")
    sp = sub.add_parser("unlock")
    sp.add_argument("--run")
    sp.add_argument("--project-dir")
    args = p.parse_args(argv)
    commands = {"build": cmd_build, "lock": cmd_lock, "unlock": cmd_unlock,
                "snapshot": cmd_snapshot, "verify-writes": cmd_verify_writes,
                "harvest": cmd_harvest, "record": cmd_record}
    try:
        with run_lock(getattr(args, "run", None) or "/nonexistent"):
            result = commands[args.cmd](args)
    except (InputError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
