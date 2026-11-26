# What the CURATE IR cannot (yet) represent from FaaSr

What FaaSr offers that the CWL IR (`docs/CWL_IR.md`) has no field for, or only one target
supports. Each row says what the two builders do today: the FaaSr builder
(`curate-skills/scripts/cwl_faasr.py`, writes `workflow.json`) and the Pegasus builder
(`curate-skills/scripts/cwl_pegasus.py`, writes Pegasus's own `workflow.yml`). Fields in the last
column are **proposals only**; none exists yet. Functions are not a gap: every step is a plain
`main(inputs, outputs, params)` that runs unchanged on both targets (FaaSr through a generated
wrapper, Pegasus through `curate_ir/runner/curate_run.py`).

| FaaSr feature | FaaSr builder | Pegasus builder | Suggested IR field |
|---|---|---|---|
| Conditional `InvokeNext` (`{"True": [...], "False": [...]}`) | `curate:run_if` becomes a conditional `InvokeNext` (one condition per step; untested on real FaaSr) | refused with a clear message; Gate 4 does not offer Pegasus | none: `curate:run_if` exists. Pegasus could run the step and let the runner skip it, writing empty outputs. |
| `Secrets` (environment variables the function reads) | `curate:secrets` names become the workflow's `Secrets` | HTCondor `getenv` of the names (values come from the submit environment) | none: `curate:secrets` exists |
| File-less edges | `curate:after` becomes `InvokeNext` | a job dependency | none: `curate:after` exists |
| N→M handoff read one copy per rank (writer ×N, reader ×M, M ≠ N) | refused | refused | a mapping rule (e.g. `select: "(r-1) % 3 + 1"`); it stays refused until there's a use case. (A reader that needs every copy already says `read: all` in the spec.) |
| Number of files found at run time (FaaSr lists the S3 prefix when the step runs) | a folder input lists its prefix at run time | a folder input holds the files known when the workflow is built | `curate:glob` on an input. Pegasus can't do this; it needs a fixed count, or a workflow that plans its next part as it runs. |
| A folder input gathering several sources (`linkMerge: merge_flattened`) | refused: a FaaSr folder lists one prefix | accepted | none needed. Specs never gather several sources into a folder (see `CWL_IR.md`); it is there for imported CWL. |
| Several axes on one step (`nested_crossproduct`, two-axis gathers) | refused: one ranked action per step, over one axis | accepted | none needed: CWL `scatter` already says it |
| `FunctionGitRepo` (per-function GitHub repos) | each step ships as its unchanged function plus a generated wrapper | functions copied into the workflow folder and staged as replicas | `code: git+https://…/repo@ref#path/fn.py` |
| `PyPIPackageDownloads` (installed at run time) | from `SoftwareRequirement` | warning only; the packages must already be in the image | `SoftwareRequirement` stays informational for Pegasus, plus a per-workflow image build (the planned image phase) |
| `Type: "Python"` (FaaSr also runs R) | Python only | Python only | a `curate:function` `language: python\|r`, with a runner per language |
| `DataStores` / `DefaultDataStore` / `folder` (S3 namespace) | from the run's settings and `FAASR_S3_*` | flat file names in each job's folder | none: backend and run settings, not part of the workflow |
| `FaaSrLog` / `LoggingDataStore`, `InvocationIDFromDate` | fixed values, the run's data store | the backend's own logs and run folder | none: backend and run settings |
| `ComputeServers` / `FaaSServer` per action | one compute server per run | one site per run | a step `site` hint, only if mixed sites are ever needed |
| `ActionContainers` (per-function container) | every action gets FaaSr's default container; `DockerRequirement` is not used | per-job container from `DockerRequirement` | none in the IR (`DockerRequirement` says it); the FaaSr builder could map it to `ActionContainers` |
| FaaSr fan-in: a step may run once per predecessor | FaaSr's own behaviour | runs exactly once | none (running once is the intended behaviour); worth a line in the spec |
