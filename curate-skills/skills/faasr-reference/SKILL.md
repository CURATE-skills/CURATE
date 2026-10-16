---
name: faasr-reference
description: FaaSr Python function rules for curate-mga — the runtime API (faasr_get_file, faasr_put_file, faasr_log, faasr_secret, faasr_rank, faasr_get_folder_list), ranked fan-out/fan-in, data integrity, dependency files and local stub testing. Preloaded into the curate-mga subagent; not for direct use.
user-invocable: false
---

# FaaSr function reference

Adapted from CURATE-prototype `agents/fga.py` (`_SHARED_RULES`) and
`faasr/context_dir.py` (MIT, Copyright (c) 2026 Nolan Cutler; see the plugin's NOTICE).

A FaaSr function is one top-level Python function, run in a minimal headless Linux
container on GitHub Actions. Its only link to other functions is files in an S3 folder.

## Signature

```python
def <name>(folder: str, input1: str, ..., output1: str, ...) -> None
```

`folder` is the S3 folder; `inputN` / `outputN` are S3 file names (basenames only).
The exact signature is in the function's `spec.md`.

## Runtime API

```python
faasr_get_file(local_file="local.csv", remote_folder=folder, remote_file=input1)
faasr_put_file(local_file="local.csv", remote_folder=folder, remote_file=output1)
faasr_log("Processing complete")
token = faasr_secret("SOME_API_TOKEN")        # credentials — see Required Secrets in spec.md
r = faasr_rank()                               # {"rank": i, "max_rank": N} (ranked functions only)
names = faasr_get_folder_list(prefix=folder)   # FULL keys incl. folder prefix
```

- The runtime injects these names as globals. The file must contain **no faasr import
  of any kind**: no `import faasr`, no `from faasr import ...`, no `from FaaSr_py import ...`,
  no `faasr_stubs`, no `try/except ImportError` guard. Call them as bare names.
- `faasr_delete_file` exists at runtime but has no local stub; do not use it.
- **Never use boto3** or any direct S3 access.
- To read every file in a folder: `faasr_get_folder_list(prefix=...)`, then
  `faasr_get_file` each one with only the basename: `remote_file=name.rsplit("/", 1)[-1]`.
- Work on local temp files; upload with `faasr_put_file` at the end. **Every declared
  output must be uploaded** — a file written only locally is lost when the invocation ends.

## Data integrity (non-negotiable)

These are real scientific workflows. Never generate synthetic, mock, random, placeholder
or example data inside a function. Never add a `try/except` or fallback that fabricates,
simulates or hardcodes data when an API, URL, dataset, file, credential or network
resource is missing or fails. If a required source cannot be reached, `faasr_log` the
error and raise. Failing loudly is correct; inventing data is never acceptable.

## Secrets

Read each credential with `faasr_secret("NAME")`. Never hardcode one. If the function
needs a credential that `spec.md` does not list, introduce it with an UPPER_SNAKE_CASE
name (`^[A-Z][A-Z_0-9]*$`); it is detected automatically. A missing secret makes
`faasr_secret` raise — let it.

## Ranks (parallel instances)

- **Ranked function** (`rank: N > 1`): it runs as N parallel instances. Call
  `r = faasr_rank()` and process only shard `r["rank"]` (1..N). File names contain a
  `{rank}` placeholder; substitute it, e.g. `input1.format(rank=r["rank"])`.
  Implement ONE function, never N copies.
- **Not ranked**: `faasr_rank()` reports this function's own rank (1). Do not call it.
- **Fan-out** to a ranked successor ×N: write exactly N shards with the literal count N
  from `spec.md`, substituting `{rank}` with 1..N:
  ```python
  for i in range(1, N + 1):
      faasr_put_file(local_file=..., remote_folder=folder, remote_file=output1.replace("{rank}", str(i)))
  ```
- **Fan-in** from a ranked predecessor ×N: you run once after all N instances finish.
  Discover their outputs with `faasr_get_folder_list(prefix=...)` and read each one; do
  not assume a fixed count.
- `{rank}` is the only placeholder allowed in file names.

## Dependencies

Write the third-party PyPI packages the code imports to `<name>.deps.txt` in your
folder, one installable package name per line (the name you would `pip install`, not
the import name). Exclude the standard library, `faasr` and `boto3`. Prefer headless or
slim variants (e.g. `opencv-python-headless`, never `opencv-python`). Write an empty
file if there are none.

## Local stub testing

Run exactly the command in `spec.md` → "Local Stub Test":

```bash
uv run "<plugin>/scripts/stubtest.py" run --run <run dir> --node <name>
```

It runs your function through local stand-ins for the runtime API, with your folder as
the working directory:

- `faasr_get_file` reads your `test_output/`, then your `test_data/`, then the
  `test_output/` of every upstream function — so you are tested on the real outputs
  upstream functions produced. A file found nowhere is created empty, with a warning.
- `faasr_put_file` writes to your `test_output/`.
- `faasr_secret` reads environment variables; deployment credentials are not available.
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
