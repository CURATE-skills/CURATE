# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "rank-bm25==0.2.2", "boto3==1.37.11", "cwl-utils==0.47", "pyyaml==6.0.2", "pegasus-wms.api==5.1.3"]
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Partly derived from CURATE-prototype src/faasr_agents/faasr/context_dir.py and
# agents/fga.py (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140): test-data
# seeding, the CONTEXT.md / spec.md text and the deps/secrets harvest are adapted
# copies. The lock, snapshot and verify code is new. See scripts/VENDORED.md.
"""Scratch directory, MGA lock, write verification and harvest for /curate.

Layout built under <run>/:
  CONTEXT.md                 shared workflow context for every MGA subagent
  context/                   copies of user reference files
  functions/<node>/          one folder per node — the only place its MGA may write
      spec.md  <node>.py  <node>.deps.txt  original_<node>.py
      test_data/  test_output/  stub_in/  parity_report.txt  stubtest.log

Every generated function follows the plain contract (plain_contract.py):
def main(inputs, outputs, params), on local paths.

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

import plain_contract as PC
from curate_models import FunctionSpec, WorkflowSpec, clean_dependencies
import runstate
from runstate import node_basis
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

def conditional_edges(spec: FunctionSpec, wf: WorkflowSpec) -> dict[str, list[str]]:
    """{"True": [successors], "False": [...]} for this step's conditional out-edges."""
    out: dict[str, list[str]] = {}
    for e in wf.edges:
        if e.from_node == spec.name and e.condition is not None:
            out.setdefault(e.condition, []).append(e.to_node)
    return out


def signature_and_test_call(spec: FunctionSpec, wf: WorkflowSpec) -> tuple[str, str]:
    """The plain signature, and the call the stub test makes for rank 1 (stubtest.plain_call:
    inputs are copied to stub_in/r1/, outputs go to test_output/)."""
    ret = "bool" if conditional_edges(spec, wf) else "None"
    layout, _ = PC.io_layout(wf)
    lay = layout[spec.name]
    r = 1 if lay["rank"] > 1 else None
    ins, outs = {}, {}
    for s in lay["inputs"]:
        if s["kind"] == "list":
            ins[s["slot"]] = [f"stub_in/r1/{PC.resolve(s['file'], i)}" for i in range(1, (s["copies"] or 0) + 1)]
        elif s["kind"] == "folder":
            ins[s["slot"]] = f"stub_in/r1/dir_{s['slot']}"
        else:
            ins[s["slot"]] = f"stub_in/r1/{PC.resolve(s['file'], r)}"
    for s in lay["outputs"]:
        if s["kind"] == "list":
            outs[s["slot"]] = [f"test_output/{PC.resolve(s['file'], i)}" for i in range(1, (s["copies"] or 0) + 1)]
        else:
            outs[s["slot"]] = f"test_output/{PC.resolve(s['file'], r)}"
    params = {"rank": 1, "max_rank": lay["rank"]} if r else {}
    return (f"def main(inputs: dict, outputs: dict, params: dict) -> {ret}",
            f"main({json.dumps(ins)}, {json.dumps(outs)}, {json.dumps(params)})")


def slot_lines(spec: FunctionSpec, wf: WorkflowSpec) -> tuple[str, str]:
    """spec.md's Inputs / Outputs: what each slot holds, from plain_contract.io_layout."""
    layout, _ = PC.io_layout(wf)
    lay = layout[spec.name]
    ranked = lay["rank"] > 1
    readers: dict[str, list[str]] = {}
    for n in wf.nodes:
        for i in n.inputs:
            readers.setdefault(i.name, []).append(n.name)

    def tail(io) -> str:
        return f"  ({io.type or 'any'})" + (f" — {io.description}" if io.description else "")

    ins = []
    for s, io in zip(lay["inputs"], spec.inputs):
        if s["kind"] == "folder":
            what = (f'a FOLDER: a directory holding every file whose name starts with "{s["prefix"]}"; '
                    "list it with os.listdir and read every file in it (never assume how many there are; "
                    "don't filter by name, it holds only these files). If it is EMPTY, raise: no files means "
                    "the steps before this one wrote nothing")
        elif s["kind"] == "list":
            what = f"a LIST of {s['copies']} paths, every copy in rank order 1..{s['copies']}"
        elif ranked and PC.RANK in s["file"]:
            what = f"a path: THIS rank's copy ({PC.resolve(s['file'], 2)} for rank 2)"
        else:
            what = "a path" + (" (the same file for every rank)" if ranked else "")
        ins.append(f'  inputs["{s["slot"]}"]   {s["file"]}{tail(io)}\n      → {what}')
    outs = []
    for s, io in zip(lay["outputs"], spec.outputs):
        if s["kind"] == "list":
            who = ", ".join(r for r in readers.get(s["file"], []) if r != spec.name) or "the next step"
            what = (f"a LIST of {s['copies']} paths in rank order: write EVERY one (path i is read by "
                    f"rank i of {who})")
        elif ranked and PC.RANK in s["file"]:
            what = "a path: write THIS rank's copy"
        else:
            what = "a path: write it"
        outs.append(f'  outputs["{s["slot"]}"]  {s["file"]}{tail(io)}\n      → {what}')
    return "\n".join(ins) or "  (none)", "\n".join(outs) or "  (none)"


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
                 "test_output/ is cleared first, and every declared output must be written."
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
source is unavailable — if it cannot be reached, print the error and raise.

## Function contract
Every function is ONE file defining `def main(inputs, outputs, params)` that works only on the
local paths it is given (see the function-reference skill). `inputs` / `outputs` map the slots
`input1`…, `output1`… (your spec.md lists what each one holds) to local paths; `params` is
{{"rank": r, "max_rank": N}} for a ranked function and {{}} otherwise. The SAME file runs
unchanged on every target: FaaSr, Pegasus, and the local stub test each hand it paths. So it
never names a workflow system: no faasr_* calls or imports, no boto3/S3, no folder or bucket.

## Neighbor Code (`{run}/functions/*/`)
Every function that already exists (catalog/cached reuse, seeds, user-provided originals
`original_<name>.py`, and functions implemented earlier in this run) lives in its own folder.
Steps are linked by the FILE NAMES in the spec, which the runtime turns into the paths each
function receives; your code never writes a file name. Read neighbors to match DATA FORMATS
(columns, JSON keys, units) exactly.

## Testing with Stubs
Run your function through the local stub test with the exact command in your spec.md
("Local Stub Test"). {mode_note}
It calls `main` with copies of the real files: each input is looked up in your test_output/,
then your test_data/, then the test_output/ of every upstream function (nearest first) — so
you are tested on the REAL outputs upstream functions produced. In strict mode a file an
upstream function should have written but didn't is an error; an external input found nowhere
is replaced by an EMPTY file, with a warning that is shown to the user at Gate 3.

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
    signature, test_call = signature_and_test_call(spec, wf)
    me = node_dir(run, spec.name)
    inputs_lines, outputs_lines = slot_lines(spec, wf)
    cond = conditional_edges(spec, wf)
    return_line = ("Return `True` or `False`: "
                   + "; ".join(f"`{v}` runs {', '.join(ts)}" for v, ts in sorted(cond.items()))
                   + ". Every declared output must still be written." if cond else
                   "Return `None` (nothing). Every declared output must exist when `main` returns.")
    incoming = "\n".join(f"  '{d.file}' ({d.format or 'any'}) ← from {d.from_node} "
                         f"(code: {node_dir(run, d.from_node)}/{d.from_node}.py)"
                         for d in wf.data_flow if d.to_node == spec.name) \
        or "  (none — this is a source node)"
    outgoing = "\n".join(f"  '{d.file}' ({d.format or 'any'}) → to {d.to_node}"
                         for d in wf.data_flow if d.from_node == spec.name) \
        or "  (none — this is a sink node, write final outputs)"
    deps = ", ".join(spec.dependencies) if spec.dependencies else "none"
    secrets_block = "\n".join(f'  - {s}   →   read with os.environ["{s}"]'
                              for s in spec.secrets) or "  (none)"

    layout, _ = PC.io_layout(wf)
    lay = layout[spec.name]
    rank_by_name = {n.name: n.rank or 1 for n in wf.nodes}
    rank_block = ""
    if spec.rank > 1:
        rank_block += (
            f"\n## Parallel Execution (RANK = {spec.rank})\n"
            f"This function runs as {spec.rank} parallel instances. `params[\"rank\"]` is THIS "
            f"instance's rank (1..{spec.rank}) and `params[\"max_rank\"]` is {spec.rank}; use them to "
            "process ONLY this instance's shard (e.g. pick the rank-th category). The paths you are "
            "given are already this rank's copies: never substitute {rank} into anything yourself.\n")
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
                f"{', '.join(repr(f) for f in handed)}. Write it exactly once — do not shard it.\n")
            continue
        lists = [o["slot"] for o in lay["outputs"] if o["kind"] == "list"]
        if lists and spec.rank <= 1:
            rank_block += (
                f"\n## Fan-out to ranked successor `{s}` (×{n})\n"
                f"`{s}` runs as {n} parallel instances, so "
                + " and ".join(f'`outputs["{x}"]`' for x in lists)
                + f" is a LIST of {n} paths: write one shard to EACH path, in order (path i is read by "
                f"rank i). Take the count from the list, `len(outputs[...])`; don't hard-code it.\n")
    for p in (e.from_node for e in wf.edges if e.to_node == spec.name):
        n = rank_by_name.get(p, 1)
        if n > 1 and spec.rank <= 1:
            rank_block += (
                f"\n## Fan-in from ranked predecessor `{p}` (×{n})\n"
                f"You run ONCE after all {n} instances of `{p}` finish. Read ALL of their outputs "
                "through the input that holds them (a LIST of paths, or a FOLDER you list with "
                "os.listdir, as Inputs above says). Never assume a fixed count. If that input holds NO "
                "files, raise an error naming it: never return a zero or empty result for \"no inputs\" "
                "(\"whatever files exist\" means some copies may be missing, never that none is fine).\n")

    feedback_block = (f"\n## Feedback from Code Review\n{feedback}\n"
                      "\nAddress all points above in your implementation.\n") if feedback else ""
    user = spec.source == "user_provided"
    prior_block = ""
    if spec.code and not user and spec.source != "new":
        prior_block = (f"\n## Previous Implementation\nSee `{me}/{spec.name}.py` (seeded from "
                       f"{spec.origin_kind or 'a previous round'}"
                       f"{' `' + spec.origin_name + '`' if spec.origin_name else ''}) — make the "
                       "smallest change that addresses the requirements while preserving everything else.\n"
                       + ("" if PC.is_plain(spec.code) else
                          "That code is in the OLDER FaaSr style (faasr_* calls, a `def "
                          f"{spec.name}(folder, …)` entry point). Convert it to `main(inputs, outputs, params)` "
                          "on the paths above, keeping its computation exactly; drop every faasr_* call.\n"))
    run_original = (f'uv run --no-project --python 3.13 --with-requirements {spec.name}.deps.txt '
                    f'python original_{spec.name}.py')
    parity_block = ""
    if user and spec.user_model_mode == "verbatim":
        parity_block = (
            "\n## [User Provided Model] — Wrap Verbatim (REQUIRED)\n"
            f"`{me}/original_{spec.name}.py` is a user-provided Python **script or function**. The "
            "user's model code itself must NOT change — your job is only to give it the plain entry "
            "point `main`.\n"
            "1. Read it and work out WHAT it computes — its real inputs (files it reads, CLI/argparse "
            "args, hardcoded paths, stdin) and outputs (files written, stdout, return value).\n"
            f"2. Copy the model's code into `{spec.name}.py` UNCHANGED — no renaming, no "
            "algorithm/constant/output-format changes. (If it is a plain script, you may enclose its "
            "top-level logic in a function so it is callable, but every statement's logic must be "
            "preserved.) Then add `main(inputs, outputs, params)` with the signature above: read the "
            "input paths, convert formats as needed, CALL the model, and write its results to the "
            "output paths.\n"
            "3. `main` must produce its results ONLY by calling the preserved model "
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
            "match without changing the model, print the discrepancy and FAIL — never fabricate "
            "data and never modify the model to force a pass.\n")
    elif user:
        parity_block = (
            "\n## [User Provided Model] — Refactor & Verify Parity (REQUIRED)\n"
            f"`{me}/original_{spec.name}.py` is a user-provided Python **script or function** (it may "
            "be a plain script: top-level statements, argparse, hardcoded paths, an "
            "`if __name__ == \"__main__\"` block).\n"
            "1. Read it and work out WHAT it computes — its real inputs and outputs.\n"
            f"2. Refactor that logic into `{spec.name}.py` as `main(inputs, outputs, params)` with the "
            "signature above, reading the input paths and writing the output paths it is given. "
            "Apply EXACTLY the changes the workflow requirements and feedback "
            "call for — everything else must preserve the original computation.\n"
            f"3. List the original's third-party imports in `{spec.name}.deps.txt` "
            f"(run the original with `{run_original}`).\n"
            "4. VERIFY PARITY: construct representative synthetic inputs; RUN the original to capture "
            "its baseline outputs; run your translation on the equivalent inputs via the stub "
            "test; compare the outputs.\n"
            f"5. Iterate until they match apart from the required changes, then write `{me}/"
            "parity_report.txt` noting what you compared, that they agree, and which deviations are "
            "intentional. If they cannot be made to match, print the discrepancy and FAIL — never "
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

## Function Signature  (write it in `{me}/{spec.name}.py`)
```python
{signature}
```
{return_line}
`inputs` / `outputs` map the slots below to LOCAL paths; read and write only those paths.
`params` is {{"rank": r, "max_rank": N}} for a ranked function, else {{}}.

## Inputs
{inputs_lines}

## Outputs
{outputs_lines}
{rank_block}
## PyPI Dependencies (third-party packages only — NOT stdlib, NOT any workflow system)
{deps}
Write the packages your code imports, one per line, to `{me}/{spec.name}.deps.txt`
(empty file if none; prefer headless variants such as opencv-python-headless).

## Required Secrets (credentials / API tokens)
Read each one with os.environ["NAME"] — never hardcode; a missing one raises KeyError, let it.
If you discover this function needs a credential not listed here, read it with
os.environ["NEW_NAME"] using an UPPER_SNAKE name — it is detected automatically.
{secrets_block}

## Data Flow  (for orientation: the runtime turns these file names into your paths)
### Incoming files (produced by upstream functions):
{incoming}

### Outgoing files (consumed by downstream functions):
{outgoing}

## Local Stub Test
Test call (rank 1): `{test_call}`
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
                    "note": f"the MGA lock belongs to session {held}; left in place"}
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
    """Credentials the code reads: UPPER_SNAKE os.environ / os.getenv names (plain contract)."""
    return list(PC.secret_names(code))


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


def read_assumptions(path: Path) -> list[str]:
    """curate-mga's interpretations while coding: one per line in its folder's assumptions.txt
    ("<topic> -> <reading used>"); a missing file, blank lines and "none" mean none."""
    if not path.is_file():
        return []
    lines = [" ".join(l.lstrip("-*• ").split()) for l in path.read_text(errors="replace").splitlines()]
    return [l for l in lines if l and l.lower() not in ("none", "none.")][:20]


def stub_result(log: Path) -> dict:
    """The result JSON at the top of stubtest.log (before its first blank line); {} if none."""
    try:
        return json.loads(log.read_text().split("\n\n", 1)[0])
    except (OSError, ValueError):
        return {}


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
    entry["empty_external_inputs"] = stub_result(log).get("empty_external_inputs") or []
    entry["assumptions"] = read_assumptions(node_dir(run, args.node) / "assumptions.txt")
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
        with runstate.locked(getattr(args, "run", None) or "/nonexistent"):
            result = commands[args.cmd](args)
    except (InputError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
