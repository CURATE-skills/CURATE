# CURATE workflow IR — v0.2 (draft)

A small, backend-neutral description of a CURATE workflow. CURATE's emit stage
writes this file once; one emitter per workflow system turns it into that
system's format (Pegasus now; FaaSr, CWL, Snakemake, Nextflow later).

## Design rules

1. **Dataflow is files.** A node reads named input files and writes named output
   files. Edges are not written down; they come from which node produces a file
   another node consumes (same model as Pegasus, Snakemake and CWL).
2. **Functions are plain.** Function code never calls a workflow system's API
   (no `faasr_get_file`, no Pegasus calls). Contract:

   ```python
   def main(inputs: dict, outputs: dict, params: dict) -> None:
       """inputs/outputs map logical names to local paths: a path, a list of paths,
       or (a folder input) a directory."""
   ```

   Each target turns its own world into this call, and the function is the same
   file on every target. For Pegasus the shim
   (`curate-skills/curate_ir/runner/curate_run.py`) calls it on the staged files; for
   FaaSr a generated wrapper (`curate-skills/scripts/faasr_wrapper.py`) downloads the
   inputs from S3, calls it, and uploads the outputs. This is what decouples CURATE
   from FaaSr. The validator refuses code that calls the FaaSr API, and a `contract`
   field (an earlier interim, now removed).
3. **The image is part of the workflow; the runtime is not.** Nodes name an OCI
   image (`docker:///…` or a saved `file://….tar`). Docker vs Apptainer is chosen
   per site by the backend, so the same IR runs unchanged on a laptop, the lab
   server, or OSPool.
4. **Self-contained nodes are checkable.** The validator flags what makes a node
   non-portable: absolute paths in params, undeclared files, missing producers,
   runtime package installs. These map to the paper's "Portability" and
   "Handoff" failure categories.

## Format (YAML or JSON)

```yaml
ir_version: "0.1"                       # or "0.2" for fan-out (see below)
name: corvallis-winter-weather          # [a-z0-9-]+
description: one line
defaults:
  image: docker:///python:3.12-slim      # used when a node has none
  resources: {cpus: 1, memory_mb: 2048}
inputs:                                 # files that exist before the run (may be none)
  - file: params.json
    location: input/params.json         # relative to the IR file, or file:///abs, https://, s3://
nodes:
  - id: make_data                       # unique, [a-z0-9_]+
    code: functions/make_data.py        # path relative to the IR file
    entrypoint: main                    # default: main
    inputs:  {params: params.json}      # logical name -> file name
    outputs: {numbers: numbers.csv}
    params:  {n: 100}                   # JSON-serialisable
    image: docker:///…                   # optional override
    resources: {cpus: 2, memory_mb: 4096}   # optional override
    packages: [numpy]                   # informational; must already be in the image
outputs:                                # deliverables (what a verifier grades)
  - file: summary.json
    deliver_as: results/summary.json    # optional path under the delivery folder
```

File names are flat and unique across the workflow (Pegasus logical file
names). A file is produced by at most one node.

## Mapping to backends

| IR | Pegasus | FaaSr | CWL | Snakemake | Nextflow |
|---|---|---|---|---|---|
| node | Job + Transformation (shim) | Function in FunctionList | CommandLineTool step | rule | process |
| file edge | File in/out (LFN) | S3 object + InvokeNext | File-typed output → input | output/input | path channel |
| image | Container in TC; Docker/Singularity per site | ActionContainers | DockerRequirement | `container:` | `container` |
| resources | pegasus/condor profiles | per-platform settings | ResourceRequirement | `resources:` | `cpus`/`memory` |
| inputs | Replica catalog | S3 inputs | job order file | config | params |
| outputs | stage_out to output site | S3 folder | workflow outputs | targets | publishDir |

Only the Pegasus emitter exists in this draft.

## v0.2: fan-out (parallel copies of a node)

Fixed parallel branches already work in v0.1: two nodes that read the same file
and don't depend on each other run at the same time. v0.2 adds *data-driven*
branches: one node definition, one parallel copy per value of a list.

```yaml
ir_version: "0.2"
axes:                                   # named lists of values (file-name safe)
  winter: [2019, 2020, 2021, 2022]
nodes:
  - id: detect_ice
    for_each: [winter]                  # one copy per value; several axes = every combination
    inputs:  {daily: daily.csv}         # 1 -> N: every copy reads the same file
    outputs: {ice: "ice_{winter}.json"} # must contain {winter}
    params:  {run_days: 5}              # each copy also gets params["winter"]
  - id: add_duration
    for_each: [winter]
    inputs:  {ice: "ice_{winter}.json"} # N -> N: copy 2019 waits only for copy 2019
    outputs: {row: "row_{winter}.json"}
  - id: assemble                        # no for_each over winter ...
    inputs:  {rows: "row_{winter}.json"}# ... N -> 1 gather: inputs["rows"] is a list of paths
    outputs: {result: result.json}
```

Rules:
- `{axis}` may appear in node input/output file names, string params, and workflow
  `inputs` / `outputs` (`file`, `location`, `deliver_as`).
- An output of a fan-out node must contain every axis it fans out over.
- An input placeholder for an axis the node does *not* fan out over is a **gather**:
  the function receives a list of paths, in axis order.
- The same holds for **outputs**: an output placeholder for an axis the node does
  *not* fan out over makes that output a list of paths, one per value, and the
  function must write every one (a 1 -> N "scatter" by a single step).
- A copy gets each of its axis values in `params` (as a string), unless `params`
  already sets that name or passes the value on under another name. A param whose
  whole value is one placeholder (`rank: "{rank3}"`) takes the axis value, as an
  int when the value is all digits.
- Copies are named `<id>__<value>` (e.g. `detect_ice__2019`).

### Folder inputs

An input can be a **folder**: the function receives one local directory and lists
it (`os.listdir`), instead of a path or a list of paths.

```yaml
  - id: shuffle
    inputs:  {counts: {folder: "map_counts_{rank3}.json"}}   # or a list of names / patterns
    outputs: {parts: "part_{rank5}.json"}                    # not fanned out: a list of 5 paths
```

`folder:` takes a file name or a list of them; each may hold placeholders, which
expand like a gather (every value of an axis the node does not fan out over). After
expansion the value is `{folder: [file, …]}`. The files are ordinary dataflow (each
needs a producer or a workflow input), so the backend stages them like any input;
the runner (`curate_run.py --in-dir name=file`) links them into one directory.

Two rules keep a folder input honest, because its function lists the directory and
must not filter it by name:
- **It holds only its own files.** When CURATE builds a workflow from a spec (the FaaSr
  bundle and the Pegasus IR), it refuses one where any other file (another step's output,
  or a file another step reads) starts with a folder input's prefix (the text before
  `{rank}`), e.g. `count_summary.json` next to a `count_{rank}.json` folder input. On FaaSr
  such a file would sit under the same S3 prefix and be downloaded into the folder.
- **An empty folder is an error.** The validator refuses a folder input with no files; the
  FaaSr wrapper raises before calling `main` when the prefix holds no objects; and the
  function itself must raise rather than return an empty or zero result.

### The runner's command line (`curate-skills/curate_ir/runner/curate_run.py`)

`--in name=file` (a path), `--in-list name=file` (a gather), `--in-dir name=file`
(a folder input), `--out name=file`, `--out-list name=file` (a list output), and
`--params-json`. The function is called `main(inputs, outputs, params)`, and every
declared output must exist afterwards. Inputs and outputs whose logical name starts
with `curate_done` are ordering markers: the function does not see them, and the
runner writes the output markers (empty) after `main` returns.
- `expand.py ir.yaml` prints the expanded node list; validation and every backend
  work on the expanded form.

| Fan-out in | Equivalent |
|---|---|
| Pegasus | one Job per copy (generated by the emitter) |
| CWL | `scatter` on a step input; gather = array input |
| Snakemake | wildcard in file names; gather = `expand()` |
| Nextflow | channel of values; gather = `.collect()` |
| FaaSr | a ranked action (`InvokeNext: name(N)`); the generated wrapper fills in each copy's `{rank}` |


## How CURATE maps a spec

CURATE builds two targets from one approved spec. Every step is a plain function
(`curate-skills/scripts/plain_contract.py`): slots `input1`…, `output1`… in the spec's
order, `params` `{rank, max_rank}` on a ranked step, the entry point `main`.

### Pegasus: the IR (`curate-skills/scripts/pegasus_ir.py`)

| Spec | IR |
|---|---|
| step with `rank: N` | `for_each: [rankN]`, axis `rankN: ["1".."N"]`; `params: {rank: "{rankN}", max_rank: N}` (an int per copy) |
| `{rank}` file | the axis of its ranked writer, else of the ranked steps that read it one copy per rank |
| unranked step writes a `{rank}` file | one list output (`output1: "f_{rankN}"`, not fanned out) |
| ranked reader of its own copy | the default: paired copy by copy (same axis) |
| ranked reader of every copy | the input says `read: all`: gather through the alias axis `rankN_all` |
| unranked reader of a `{rank}` file | gather (a list) |
| input `kind: folder` | `{folder: [...]}`: every workflow file whose name starts with the input's prefix (the text before `{rank}`), all copies |
| edge with no file between its steps | marker file `curate_done__<step>[_{rankN}]`; the later step waits for every copy |
| end files (written, read by no step) | `outputs`, delivered as `results/<file>`; an explicit list replaces this |

Refused, each with its own message: a step that is not a plain function (older
FaaSr-style code: regenerate it, or migrate its catalog with
`curate-skills/scripts/migrate_catalog.py`), conditional edges, per-rank N→M handoffs,
a folder input that matches no workflow file or would also hold another file, secrets,
steps without code.
Packages only warn: they must already be in the image.

### FaaSr: the bundle (`curate-skills/scripts/emit.py`, `faasr_wrapper.py`)

Each step ships as two files: `functions/<step>_main.py`, the function byte-identical
to the approved code, and `functions/<step>.py`, a generated wrapper that FaaSr calls
with the step's `Arguments` (`folder`, `input1`…, `output1`…, `{rank}` left in).
`FunctionGitRepo` lists both. The wrapper fills in `{rank}` from `faasr_rank()`,
downloads the inputs (every copy of a list; every object under a folder input's prefix,
and an empty folder is an error), calls `main`, forwards its stdout to `faasr_log`,
checks and uploads the outputs, and returns `main`'s value (which drives a conditional
`InvokeNext`). Secrets reach `main` as environment variables, as FaaSr's Actions set them.
