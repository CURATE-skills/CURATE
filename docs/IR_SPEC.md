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
   def main(inputs: dict[str, str], outputs: dict[str, str], params: dict) -> None:
       """inputs/outputs map logical names to local file paths."""
   ```

   Each backend ships a small shim (`runner/curate_run.py`) that turns its own
   world into this call. For FaaSr the shim downloads/uploads S3 objects; for
   Pegasus it is just local paths. This is what decouples CURATE from FaaSr.
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
inputs:                                 # files that exist before the run
  - file: params.json
    location: file:///abs/path/params.json   # or https://, s3://
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
- Copies are named `<id>__<value>` (e.g. `detect_ice__2019`).
- `expand.py ir.yaml` prints the expanded node list; validation and every backend
  work on the expanded form.

| Fan-out in | Equivalent |
|---|---|
| Pegasus | one Job per copy (generated by the emitter) |
| CWL | `scatter` on a step input; gather = array input |
| Snakemake | wildcard in file names; gather = `expand()` |
| Nextflow | channel of values; gather = `.collect()` |
| FaaSr | to check (parallel invocations of one action) |

