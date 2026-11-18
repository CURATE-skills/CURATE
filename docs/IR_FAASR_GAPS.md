# What the CURATE IR cannot (yet) represent from FaaSr

To-do list for the later IR → FaaSr converter (and for IR v0.3). Each row is
something CURATE's FaaSr emitter (`curate-skills/scripts/emit.py`,
`emit_faasr_json`) or the FaaSr runtime uses that the IR (`docs/IR_SPEC.md`)
has no field for. The Pegasus target (`curate-skills/scripts/pegasus_ir.py`)
refuses, warns about, or approximates each one as listed. The fields in the last
column are **proposals only**; none exists yet.

| FaaSr feature (`emit_faasr_json` / runtime) | Pegasus target today | Suggested IR field |
|---|---|---|
| Conditional `InvokeNext` (`{"True": [...], "False": [...]}`) | refused, with an explicit message | node `run_if: {file: decision.json, equals: true}`. Backends with a fixed DAG would still refuse it, or run the node and let the runner skip it, writing empty outputs. |
| `Secrets` / `faasr_secret` | refused | node `secrets: [NAME]` (names only); each backend maps them to its own store (GitHub Actions secrets, a Pegasus env profile or credential file) |
| N→M handoff read one copy per rank (writer ×N, reader ×M, M ≠ N) | refused | explicit `inputs: {input1: {file: "p_{rank3}.txt", read: one\|all}}`. A real N→M handoff still needs a mapping rule (e.g. `select: "(r-1) % 3 + 1"`), so it stays refused until there's a use case. |
| How a ranked reader reads a `{rank}` file | inferred from the code (`read_mode`: `input1.replace("{rank}", …)` = one copy, `faasr_get_folder_list` = all copies; neither = refused) | the same `read: one\|all` on the input, so nothing is guessed from code |
| Number of files found at run time (`faasr_get_folder_list` over an unknown count) | must be fixed when the workflow is built | `inputs: {x: {glob: "part_*.json"}}`. Pegasus can't do this; it needs a fixed count, or a workflow that plans its next part as it runs. |
| `FunctionGitRepo` (per-function GitHub repos) | functions copied into the run folder | `code: git+https://…/repo@ref#path/fn.py` |
| `PyPIPackageDownloads` (installed at run time) | warning only; nothing is installed | `packages` stays informational, plus a workflow `image_build: {base, packages}` (the planned per-workflow image phase) |
| `Type: "Python"` (FaaSr also runs R) | Python only | node `language: python\|r`, with a runner per language |
| `DataStores` / `DefaultDataStore` / `folder` (S3 namespace) | flat file names in each job's folder | workflow `data: {folder}` as a hint to the backend; inputs already accept `s3://` locations |
| `FaaSrLog` / `LoggingDataStore`, `InvocationIDFromDate` | the backend's own logs and run folder | none: these are backend and run settings, not part of the workflow |
| `ComputeServers` / `FaaSServer` per action | one site per run | node `site` hint, only if mixed sites are ever needed |
| `ActionContainers` (per-function container) | `defaults.image`; per-node `image` already exists | none needed: already representable |
| FaaSr fan-in: a step may run once per predecessor | runs exactly once | none (the IR's behaviour is the intended one); worth a line in the spec |
