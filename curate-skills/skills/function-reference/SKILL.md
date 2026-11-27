---
name: function-reference
description: The plain function contract for curate-mga — one main(inputs, outputs, params) on local paths that runs unchanged on FaaSr, Pegasus and the local stub test; ranks, fan-out and fan-in (lists and folders), secrets, logging, data integrity, dependency files and stub testing. Preloaded into the curate-mga subagent; not for direct use.
user-invocable: false
---

# Function reference: the plain contract

Run scripts in the plain form `uv run --quiet <plugin>/scripts/<script>.py …`: no variable assignments, no `&&` chains, no subshells.

Adapted from CURATE-prototype `agents/fga.py` (`_SHARED_RULES`) and
`faasr/context_dir.py` (MIT, Copyright (c) 2026 Nolan Cutler; see the plugin's NOTICE),
rewritten for CURATE's plain contract.

A CURATE function is ONE Python file defining `main`. The same file runs unchanged on every
target: on FaaSr a generated wrapper downloads its inputs and uploads its outputs; on Pegasus
the job's staged files are handed to it; the local stub test hands it copies of real files.
It never knows which one is calling it.

## Signature

```python
def main(inputs: dict, outputs: dict, params: dict) -> None:   # -> bool for a conditional step
```

- `inputs` / `outputs` map slot names (`input1`, `input2`, …, `output1`, …, in the order of
  your `spec.md`) to **local paths**. Your `spec.md` says what each slot holds:
  - **a path** (`str`): one file. For a ranked function it is already THIS rank's copy.
  - **a list of paths** (`list[str]`, rank order 1..N): every copy of a file family. As an
    input, read them all; as an output, write EVERY path in the list.
  - **a folder** (`str`, a directory): every file whose name starts with a prefix. List it with
    `os.listdir(inputs["input1"])` and read each file; never assume how many there are.
- `params` is `{"rank": r, "max_rank": N}` for a ranked function, and `{}` otherwise.
- Return `None`, or `True` / `False` when `spec.md` says the step has conditional edges (the
  value picks which successors run).
- Read inputs only from `inputs`, write outputs only to `outputs`. **Every declared output must
  exist when `main` returns**, or the step fails. Scratch files go in a `tempfile` directory.

## What the file must never contain

- Any `faasr_*` name (`faasr_get_file`, `faasr_put_file`, `faasr_rank`, `faasr_log`,
  `faasr_get_folder_list`, …) or any import of `faasr`, `FaaSr_py` or `faasr_stubs`. They are
  the older, FaaSr-only style; a generated function that uses them fails its stub test.
- `boto3` or any other direct S3 / cloud-storage access: the workflow system moves the files.
- File names or paths written into the code: use the paths you are given. (A file name may
  appear in a log message.) Never build a path from `{rank}`: the paths are already resolved.
  That includes name filters on a folder input (`startswith("count_")`, `endswith(".json")`):
  if a file is ever renamed, the filter silently matches nothing.
- A branch that treats a missing input or an empty fan-in as "nothing to do" and still writes a
  result. Raise instead.

## Ranks (parallel instances)

- **Ranked function** (`rank: N > 1`): it runs as N instances. Use `params["rank"]` (1..N) to
  pick this instance's share of the work (e.g. the rank-th variable). Implement ONE `main`,
  never one function per rank.
- **Fan-out** (an unranked function feeding a ranked successor ×N): the output is a list of N
  paths; write one shard to each, in order (path i is read by rank i). Take N from
  `len(outputs["output1"])`; don't hard-code it.
- **Fan-in** (a function after a ranked step): you run once, after every instance finished. The
  input holding their outputs is a list of paths or a folder, as `spec.md` says. Never assume a
  fixed count.
- **A fan-in with no files is an error.** If a list or folder input of upstream outputs holds no
  files, raise, naming the input. Never return a zero or empty result for "no inputs":
  "whatever files exist" means some copies may be missing, never that none at all is fine.
- A folder input always exists and holds exactly the files meant for it. Don't check that it
  exists, and don't filter it by file name: read every file in it.

## Logging and errors

Use `print()` (or `logging`) for progress and diagnostics: on FaaSr each line goes to the FaaSr
log, on Pegasus and in the stub test to the job's output. On an error, print what went wrong and
raise; never swallow an exception.

## Data integrity (non-negotiable)

These are real scientific workflows. Never generate synthetic, mock, random, placeholder
or example data inside a function. Never add a `try/except` or fallback that fabricates,
simulates or hardcodes data when an API, URL, dataset, file, credential or network
resource is missing or fails. If a required source cannot be reached, print the error and
raise. Failing loudly is correct; inventing data is never acceptable.

## Secrets

Read each credential with `os.environ["NAME"]` — see Required Secrets in `spec.md`. Never
hardcode one. If the function needs a credential that `spec.md` does not list, read it the
same way with an UPPER_SNAKE_CASE name (`^[A-Z][A-Z_0-9]*$`); it is detected automatically. A
missing secret raises `KeyError` — let it.

## Dependencies

Write the third-party PyPI packages the code imports to `<name>.deps.txt` in your
folder, one installable package name per line (the name you would `pip install`, not
the import name). Exclude the standard library, any workflow system and `boto3`. Prefer
headless or slim variants (e.g. `opencv-python-headless`, never `opencv-python`). Write an
empty file if there are none.

## Local stub test

Run exactly the stub-test command from your spawn prompt (`spec.md` → "Local Stub Test" shows
the same command with `<plugin>` standing for the plugin folder):

```bash
uv run --quiet <plugin>/scripts/stubtest.py run --session "<session folder>" --node <name>
```

It calls your `main` with your folder as the working directory, once per rank:

- Each input file is looked up in your `test_output/`, then your `test_data/`, then the
  `test_output/` of every upstream function (nearest first), and **copied** into
  `stub_in/r<rank>/` — so you are tested on the real outputs upstream functions produced. A
  folder input is a directory under `stub_in/` holding every matching file found that way.
- Outputs are written to `test_output/`.
- A missing input: in strict mode, a file an upstream function should have written is an error
  (the wiring is wrong, or that function's test failed). An external input found nowhere is
  replaced by an EMPTY file with a warning, and the user sees that warning at Gate 3.
- Your environment variables (secrets) are passed through; deployment credentials are not.
- The run's stub-test mode is stated in `CONTEXT.md`: `strict` runs every rank, installs
  your `deps.txt` in an isolated environment and requires every declared output;
  `parity` runs rank 1 only without installing dependencies.

You are not done until it passes on representative inputs. A pure-local function
(parse, validate, transform, simulate, plot) must pass; a parse, validation or logic
error is a real defect to fix, never "expected placeholder failure". If an input exists
in `test_data/` or upstream `test_output/`, test on it. If it exists nowhere, build a
representative input in your own `test_data/` first. The only acceptable failure is a
genuine external fetch (API, URL, database) that needs network or credentials
unavailable here.
