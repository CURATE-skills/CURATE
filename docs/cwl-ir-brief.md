# Task: make CWL the CURATE workflow IR

Branch: create `cwl-ir` from `decouple-faasr` (currently at `14bfad1` or later). Commit after each phase. Do not merge.

## Decision (already made, don't revisit)

- CURATE's workflow IR becomes **CWL v1.2**, stored as `workflow.yml` (YAML, CWL syntax).
- The IR describes only the workflow: steps, data, dependencies, software, resources. Everything about running it (Pegasus catalogs and properties; FaaSr servers, S3 stores, credentials) is produced by the target's builder after the user picks Pegasus or FaaSr at Gate 4.
- Nothing runs `workflow.yml` directly. Only CURATE's builders read it, with `cwl-utils`. So steps are CWL `Operation`s (inputs/outputs, no implementation), not `CommandLineTool`s. The CWL never references `curate_run.py`; the Pegasus builder still uses it as each job's executable.
- Do **not** use `pegasus-cwl-converter`: it rejects CWL v1.2, ignores `scatter`, and fails on `File[]`. Write our own builders.
- Builders wrap the tested `main(inputs, outputs, params)` and never rewrite it (existing design rule).
- Run values go in a per-run inputs file (`inputs.yml`); the values the workflow was tested with also go in the workflow as `default`s.
- The old IR (v0.2) stays working until phase 7 shows parity.

## IR spec (write this up in phase 1)

CWL v1.2 restricted to a CURATE profile, plus eight `curate:` extensions in a `$namespaces` block. Pick one namespace URI, define it as a single constant, and use it everywhere.

**Allowed:** `Workflow` with inline `Operation` steps; `File`, `File[]`, `Directory` (workflow inputs only), `string`, `int`, `float`, `boolean`, arrays of these; `scatter` with `dotproduct` / `nested_crossproduct` / `flat_crossproduct`; `ScatterFeatureRequirement`; `DockerRequirement` (`dockerPull`) on workflow or step; `SoftwareRequirement`; `ResourceRequirement` (`coresMin`, `ramMin`); `default` on workflow inputs; `label`, `doc`. `MultipleInputFeatureRequirement` with `linkMerge: merge_flattened`, **only** on step inputs marked `curate:folder` (a folder gathering files from several sources).

**Refused with a clear message:** `when`, `pickValue`, `InlineJavascriptRequirement` and any `$(...)`/`${...}`, `ExpressionTool`, `CommandLineTool`, sub-workflows, `valueFrom`, `secondaryFiles`, `linkMerge` other than the default (except `merge_flattened` on a `curate:folder` input), several sources on any other input, loops.

**Extensions:**

| Field | Where | Meaning |
| --- | --- | --- |
| `curate:function: {code, entrypoint}` | step `hints` | The tested Python function the step runs. Required on every step. |
| `curate:deliver_as: "<path>"` | workflow output | Where the final file goes under `deliver/`. May contain `{axis}` placeholders for `File[]` outputs, filled in axis order. |
| `curate:folder: true` | step input (`in:` entry) | The `File[]` arriving here is handed to the function as one local directory (today's `{folder: ...}` input). |
| `curate:after: [step, ...]` | step `hints` | Control-only dependency: this step runs after those steps (every copy of a scattered one) although no file passes between them. No marker files; the Pegasus builder adds a job dependency, the FaaSr builder an `InvokeNext`. |
| `curate:run_if: {step, result}` | step `hints` | Run this step only if `step`'s `main` returned `result` (`true`/`false`); implies `after: [step]`. Matches FaaSr's conditional `InvokeNext`, which the FaaSr builder emits. The Pegasus builder refuses it with a clear message, and Gate 4 hides Pegasus for such workflows (as today). |
| `curate:secrets: [NAME, ...]` | step `hints` | Names of the secrets the step reads from its environment. Never values. Each builder maps names to its own mechanism (FaaSr secrets, a Pegasus env profile). |
| `curate:axis: <axis input>` | `Operation` output of type `File[]` | The list has one file per value of that axis input (its length is the axis's length), in axis order. Required on every `File[]` output of an unscattered step. A consumer that pairs with the list (`dotproduct`) must scatter over the same axis; the validator refuses a mismatch. |
| `curate:name: "<file name>"` | `Operation` output | The output's file name in the spec; builders append it (placeholders filled) to generated names, so a function gets the same file names and extensions as in its stub test. |

**Mapping from IR v0.2:**

| IR v0.2 | CWL IR |
| --- | --- |
| node `code`/`entrypoint` | `run: {class: Operation, ...}` + `curate:function` |
| node `inputs:`/`outputs:` file names | `Operation` inputs/outputs (`File`, `File[]`) |
| `params` | step inputs, typed, sourced from workflow inputs or `default` |
| workflow `inputs:` with `location` | workflow inputs; locations in `inputs.yml` and as `default` |
| `deliver_as` | `curate:deliver_as` |
| `defaults.image`, node `image` | `DockerRequirement` (workflow-level hint, step-level override) |
| `packages` | `SoftwareRequirement.packages` |
| `defaults.resources` | `ResourceRequirement` |
| `axes: {a: [...]}` | one workflow input per axis, values as `default`, typed as IR v0.2 hands the value to the function: `int[]` when every step takes it through a whole-value renamed param (`rank: "{rank3}"`) and the values are all digits; `string[]` when a step takes it under its own name (v0.2 injects `"2019"`). An all-digit axis used both ways is refused. |
| `for_each: [a]` | `scatter: [a]` |
| `for_each: [a, b]` | `scatter: [a, b]`, `scatterMethod: nested_crossproduct` |
| full gather (placeholder axis not in consumer's `for_each`) | consumer input takes the scattered output as `File[]` |
| partial gather (consumer keeps some axes) | producer's `scatter` order puts the kept axes **outermost**; consumer uses `scatter: [kept_axis, input]`, `scatterMethod: dotproduct` (example below) |
| output placeholder not in node's `for_each` (one step writes all files) | `Operation` output `File[]` with `curate:axis: <axis>`, one file per axis value, in axis order |
| axis value injected into `params`; renamed via `rank: "{rank3}"` | step input sourced from the axis input under the param's name |
| `{folder: "part_{r}.csv"}` input | `File[]` input + `curate:folder: true` |
| `{axis}` in intermediate file names | dropped; the builder names intermediate files (deterministically, e.g. `<step>__<values>__<output>`) |
| external `{axis}` file family (workflow input `x_{r}.csv`) | workflow input `File[]`; a scattered consumer takes it with `scatter: [axis, input]`, `scatterMethod: dotproduct` |
| `curate_done*` marker files (edge with no file) | `curate:after` |
| spec conditional edge | `curate:run_if` on the target step |
| spec `secrets` | `curate:secrets` |
| expressions | still refused |

Partial-gather pattern (validated with `cwltool --validate`):

```yaml
steps:
  detect:                       # for_each: [winter, station]
    scatter: [winter, station]  # kept axis (winter) first = outermost
    scatterMethod: nested_crossproduct
    in: {winter: winter, station: station}
    out: [ice]
    run: {class: Operation, inputs: {winter: string, station: string}, outputs: {ice: File}}
  assemble:                     # for_each: [winter]; gets each winter's list of station files
    scatter: [winter, ice]
    scatterMethod: dotproduct
    in: {winter: winter, ice: detect/ice}
    out: [out]
    run: {class: Operation, inputs: {winter: string, ice: "File[]"}, outputs: {out: File}}
```

`cwltool` accepts the swapped order too, but the files then end up in the wrong groups. Our validator must catch it. If two consumers need different groupings from the same producer, refuse with a message: plain CWL can't express that without JavaScript.

## Workflow and run IDs (decided)

- **Workflow ID** = `<name>-v<N>`, e.g. `corvallis-winter-v3`. Name: slug of the workflow title (`[a-z0-9-]`), unique in the catalog (no automatic suffix). Parse with `^(.+)-v(\d+)$`.
- **The name is asked for at the sketch**, checked against the catalog then, and stored in the generation run's metadata. The version number is assigned when Gate 4 approves.
- **Every approved generation of an existing workflow creates a new version.** Emit writes a *draft* (`workflow.yml`, `inputs.yml`, `functions/`) inside the generation run folder. Gate 4 approves the draft by its tree hash. Only on approval is the version number assigned and the draft published to `.curate/workflows/<workflow-id>/` (an atomic copy, never edited afterwards). A rejected or rewound draft never becomes a version.
- **Bundle manifest.** When a builder writes a bundle into `runs/<run-id>/`, it also writes a small manifest: the version folder's hash, the CURATE version (git commit), the target, and the bundle's own hash. Deploy checks that the version hash matches the approved one and records the bundle hash.
- **Run ID** = `YYYYMMDD-HHMMSS-<4 hex>`, e.g. `20261118-012903-a3f9`. Full reference: `<workflow-id>/<run-id>`. This replaces today's IDs like `20261118-012903-corvallis-winter`.
- **Two kinds of run, both with this run-ID format.** A *generation session* stays at `.curate/runs/<run-id>/` (unchanged, so `--from-run`, rewinds and snapshots keep working). An *execution run* (after Gate 4) goes under `.curate/workflows/<workflow-id>/runs/<run-id>/`. Paths on the Pegasus host use the execution run's full reference.
- **Which workflow a generation belongs to** comes from lineage (the session reused a catalog workflow, or came from `--from-run` of a session of that workflow) or from the name. If the name is taken with no lineage, ask (at the sketch): a new name, or a new version of the existing workflow.
- **Uniqueness** is checked against the global catalog (`~/.curate/catalog`). Project `.curate/workflows/` folders only use the IDs.
- **The catalog moves to workflow ID + version keys in this task.** Saving a workflow with an existing name creates a new version instead of updating the entry in place (replaces today's `catalog.py` upsert by name). The old uuid is kept as a stored alias.
- **Content hash** = sha256 over the normalized `workflow.yml` with `label`, `doc`, every `s:` field, `curate:content_hash` and all input `default`s removed, plus the function files. It never decides the version; it only detects duplicates: if a new version is identical to an existing one, tell the user and let them keep both or drop the new one.
- **Imported workflows** = `<source prefix>-<source id>-v<source version>`, e.g. `wfh-1063-v2` for WorkflowHub. Store the source DOI/URL and title with it. (Import itself is to-do step 11, not this task.)
- **In `workflow.yml`:**

  ```yaml
  $namespaces:
    s: https://schema.org/
    curate: <the CURATE namespace URI>
  label: Corvallis winter ice comparison
  s:identifier: corvallis-winter-v3
  s:version: "3"
  curate:content_hash: sha256:...
  ```

- **Layout:** `.curate/workflows/<workflow-id>/workflow.yml` (tested values as `default`s) and `.curate/workflows/<workflow-id>/runs/<run-id>/` holding that run's `inputs.yml`, generated bundle and target, outputs, logs and status. Reorganizing the rest of `.curate/` is to-do step 5; here, only make emit and the builders write to this layout.
- **Open, don't decide:** whether catalog functions get their own IDs and versions.

## Phases

### 0. Recon (report before writing code)

- Read `curate_ir/` (`validate.py`, `expand.py`), `scripts/pegasus_ir.py`, `pegasus_backend.py`, `write_bundle`, emit, Gate 4, and `docs/IR_FAASR_GAPS.md`.
- Find **who writes the IR today**: the agent (prompt/skill text), deterministic code, or both. List every place that reads or writes IR v0.2.
- Check whether anything depends on `{axis}` in file names (catalog matching, reuse, MGA, deliverables, monitoring, `download-outputs`).
- List the IR features each existing workflow and catalog entry uses (axes, partial gather, output lists, folder inputs, renamed params).
- **Report:** a file list with what changes in each, the answer to "who writes the IR", and any feature this spec misses. If the agent writes IR by hand, stop and ask me how generation should produce CWL before phase 6.

### 1. Spec doc

- Write `docs/CWL_IR.md`: the profile, the extensions, the mapping table above, and one full example (weather, 4 steps, fan-out + gather + `deliver_as` + per-step image).
- **Done when:** the doc exists and the example passes `cwltool --validate` (an "unknown hint" for `curate:function` is expected).

### 2. Loader and validator

- `curate_ir/cwl/load.py`: load `workflow.yml` (+ optional `inputs.yml`) with `cwl-utils` into one plain internal structure. Accept any file extension.
- `curate_ir/cwl/validate.py`: the profile checks; every step has `curate:function` and its file exists; types match between sources and sinks; fan-out sizes come only from workflow inputs/defaults; partial-gather order; no conflicting groupings; axis values file-name safe; `deliver_as` placeholders match the output's axes; `curate:after`/`curate:run_if` name existing steps and make no cycle; `merge_flattened` only on `curate:folder` inputs; every `File[]` output of an unscattered step has `curate:axis` naming an axis input, and a consumer pairing with it scatters over that axis; secret names only (UPPER_SNAKE). Clear one-line errors, like the existing validator.
- **Done when:** unit tests cover each refusal and each allowed pattern.

### 3. IR v0.2 → CWL converter

- `curate_ir/cwl/from_ir.py`: convert any valid IR v0.2 workflow to `workflow.yml` + `inputs.yml`.
- **Done when:** it converts every existing test workflow and catalog workflow, and every result passes phase 2's validator.

### 4. `expand_cwl.py`

- Expand `scatter` into concrete jobs, producing **the same v0.1-shaped node list `expand.py` produces today** (inputs/outputs may be lists; `{"folder": [...]}` for folder inputs; axis values in `params`; ids `node__value`). Then the existing backends can take its output unchanged.
- Port from `expand.py`: `_combos`, gather lists in axis order, param injection (digits → `int`, renames), folder inputs, ids.
- **Parity test (key):** for every workflow from phase 3, `expand_cwl(from_ir(ir))` equals `expand(ir)`, apart from intermediate file names. Make the comparison ignore names but check structure, order and wiring.

### 5. Builders

- Pegasus builder: `workflow.yml` → `expand_cwl` → existing Pegasus path (`pegasus_backend.py` prepare/submit). Expansion happens locally; the expanded form is what gets shipped, so the Pegasus host never needs `cwl-utils`. `curate:after` becomes a job dependency; `curate:run_if` is refused with a clear message.
- FaaSr builder: `workflow.yml` → FaaSr `workflow.json` + generated wrappers (download → `main()` → upload). It reads `scatter` directly (one ranked action per step, not `expand_cwl`'s job list) and refuses a step with more than one scatter axis, with a clear message. `curate:after` and `curate:run_if` become `InvokeNext` (conditional for `run_if`).
- Both honor `curate:deliver_as`, `curate:secrets`, per-step images and packages.
- This is also to-do step 3: emit no longer writes a FaaSr bundle; the builder runs only after Gate 4 picks a target.
- **Done when:** both builders produce bundles for every converted test workflow; the existing tests pass.

### 6. Emit writes CWL

- Emit writes `workflow.yml` + `inputs.yml` instead of IR v0.2. Use deterministic code; the agent should never hand-write CWL. Phase 0 found that no agent writes the IR today: `pegasus_ir.build_ir(spec)` builds it from the approved WorkflowSpec.
- Build the CWL **from the spec**, reusing `from_ir`'s mapping for everything IR v0.2 covers (steps, files, fan-out, gathers, folders, list outputs, rank params, deliverables, images, packages). IR v0.2 cannot carry conditional edges, secrets or file-less edges, so `from_ir(build_ir(spec))` alone would drop them: take `curate:run_if`, `curate:secrets` and `curate:after` directly from the spec (its conditional edges, `node.secrets`, and edges with no file between their steps). The Pegasus builder, not the CWL, refuses `run_if` and secrets.
- Update skill/gate text that mentions the IR format.
- Assign workflow and run IDs as in "Workflow and run IDs" above: ask for the name at the sketch and check it against the global catalog (stored in the generation run's metadata); emit writes the draft into the generation run folder; when Gate 4 approves, find the workflow from lineage or the name, number the new version, compare its content hash with existing versions (an identical one: tell the user, keep both or drop the new one), and publish the draft to the new layout. The catalog saves by workflow ID + version (a new version, never an in-place update).
- **Done when:** a fresh `/curate` run produces a valid `workflow.yml` with `s:identifier`/`s:version`/`curate:content_hash`, in `.curate/workflows/<workflow-id>/`, and reaches Gate 4. Generating an existing workflow again creates the next version; an identical regeneration is reported as a duplicate.

### 7. End-to-end check

- Weather and MapReduce: generate with `/curate`, build, and run on **real Pegasus on maxwell** and **real FaaSr**.
- **MapReduce:** compare the deliverables byte-for-byte. Its intermediates come from an unseeded `random.shuffle`
  in `generate_text`, so they differ between any two runs; don't change the fixture's functions.
- **Weather:** run Pegasus and FaaSr on the same saved input data (or record the data fetched once and reuse it),
  so differences cannot come from the live data changing.
- **Two real-system checks:**
  - (a) FaaSr with the features example: a `curate:run_if` step that is also invoked by a data producer, with the
    branch both taken and not taken. Until this passes, `run_if` is documented as untested on FaaSr.
  - (b) Pegasus on maxwell: a step with `curate:secrets` actually sees the variable inside its container.
- **Done when:** the deliverables are byte-identical across Pegasus and FaaSr, and byte-identical to the old IR
  v0.2 path on the same inputs; (a) and (b) pass.

### 8. Migrate and clean up (only after phase 7 passes)

- Migrate the catalog from uuid keys to workflow ID + version (dry run first, same style as the phase 4 catalog migration in step 1; the uuid stays as an alias). The catalog stores WorkflowSpecs, not IR v0.2, so there is no IR to convert.
- **Version numbers are never renumbered.** Since phase 6, each old uuid entry holds a number: the first time it is seen it gets the lowest free number (entries first seen together: oldest first), recorded in `<catalog>/reservations.json` and always read back from there (`ids.reserved_numbers`), so it can never shift. New versions are numbered after the held ones. The migration makes the recorded numbers official; it never moves a published version. A recorded number stays held even if its entry is removed.
- Remove the IR v0.2 reader, `expand.py`, and old-format emit. Keep `from_ir.py` for one release to convert old runs.
- Update `docs/IR_FAASR_GAPS.md` and the README.

## Rules

- Never modify a function's code to fit a backend.
- Keep the old IR path working until phase 8.
- Add tests with each phase; the full test suite must pass at every commit.
- If something in this spec turns out wrong, stop and report instead of working around it.
- At the end of each phase, report what changed, test results, and anything surprising.
