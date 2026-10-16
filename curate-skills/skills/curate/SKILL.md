---
name: curate
description: Build a FaaSr serverless scientific workflow from a natural-language sketch with CURATE — skeleton, catalog reuse, code generation and a validated FaaSr bundle, with human approval gates. Use when the user asks to compose, build, generate or revise a FaaSr workflow or a multi-step serverless pipeline, or runs /curate.
argument-hint: "<workflow sketch> | --resume <run_id> [--catalog DIR] [--attach model.py] [--context FILE] [--mga-retries N] [--stubtest-mode strict|parity]"
allowed-tools: Read, Glob, Grep, Bash(uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/*), Agent(curate-skills:curate-mga)
---

# /curate — CURATE workflow gate loop

You orchestrate CURATE (arXiv 2608.04270): you act as the Workflow Composition Agent (WCA)
and the Module Candidate Agent (MCA) yourself, spawn one `curate-mga` subagent per function
for code generation, and stop at five human gates. Deterministic work is done by scripts;
never edit `state.json` by hand.

Scripts (always run exactly like this; each prints JSON with `"ok"`):

```
uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/spec.py <command> ...
uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/catalog.py <command> ...
uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/scratch.py <command> ...
uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/emit.py <command> ...
```

Below, `spec.py ...` is shorthand for the first line. Exit 0 = ok, 1 = needs attention
(read the JSON), 2 = bad input (fix what you wrote, then re-run).

References (read each when its stage starts, not all up front):
`references/wca-system.md`, `references/wca-skeleton.md`, `references/mca-candidates.md`,
`references/mca-user-model.md`, `references/wca-resolve.md`, `references/classify.md`,
`references/mga-spawn.md`, `references/gates.md`, `references/state-contract.md`.

## 0. Start or resume

Arguments: `$ARGUMENTS`

- `--resume <run_id>`: find the run with `spec.py runs`, run `scratch.py unlock --run <run>`
  (clears a stale lock), read `spec.py status --run <run>` and continue at its `stage`
  (table in `references/gates.md`).
- Otherwise the remaining text is the sketch. If there is none, ask the user for it.
  Write the sketch verbatim to a temp file and create the run:

```
spec.py init --request-file <file> [--user-model P]... [--context-file P]...
  [--catalog-dir D] [--mga-retries N] [--stubtest-mode M]
  --config-catalog-dir "${user_config.catalog_dir}"
  --config-mga-retries "${user_config.mga_retries}"
  --config-stubtest-mode "${user_config.stubtest_mode}"
```

Map `--attach` → `--user-model`, `--context` → `--context-file`, `--catalog` →
`--catalog-dir`. Tell the user the run id and the settings (`catalog_dir`,
`mga_max_retries`, `stubtest_mode`) with where each came from.

## 1. Skeleton → Gate 1

1. Read `references/wca-system.md` and `references/wca-skeleton.md`.
2. `spec.py prompt-context --run <run> --stage skeleton`; write `work/skeleton.json`.
3. `spec.py skeleton --run <run> --json <run>/work/skeleton.json`. If it reports a parse
   error, fix the file and re-run.
4. **Gate 1**: `spec.py show --run <run> --gate 1`, print its `markdown` exactly, then ask
   (see "Asking at a gate").
   - Approve → `spec.py approve --run <run> --gate 1` → step 2.
   - Changes → `spec.py directive --run <run> --gate 1 --text "<verbatim>"` → back to 1.2
     (a refinement round; it does not count toward the iteration cap).

## 2. Resolve → Gate 2

1. Read `references/mca-candidates.md` (and `mca-user-model.md` if any node has a user model).
2. `spec.py prompt-context --run <run> --stage mca`; decide every node; write
   `work/candidates.json`; `spec.py apply-candidates ...`. Mention any `notes` it returns.
3. Read `references/wca-resolve.md`; `spec.py prompt-context --run <run> --stage resolve`;
   write `work/resolved.json`; `spec.py merge-resolved ...`. One fix pass on `error` problems.
4. **Gate 2**: `spec.py show --run <run> --gate 2`, print its `markdown` exactly, then ask.
   - Approve → `spec.py approve --run <run> --gate 2` → step 3.
   - Changes → classify with `references/classify.md`, then
     `spec.py directive --run <run> --gate 2 --text "<verbatim>" --structural true|false`;
     go to step 1.2 if `next_stage` is `skeleton`, else step 2.

## 3. Generate → Gate 3

1. `spec.py preflight`. If it fails, `spec.py set-stage --run <run> --stage generate`, show
   its error, and stop: the user must relaunch with the variable set, then resume.
2. Read `references/mga-spawn.md` and follow it exactly.
3. `scratch.py build --run <run>` (add `--resume` when resuming at `stage: generate`), then
   handle each node in the returned `order`, one at a time, per `mga-spawn.md`.
4. **Gate 3**: `spec.py show --run <run> --gate 3`, print its `markdown` exactly, then ask.
   If the user wants to see code, Read `<run>/functions/<N>/<N>.py` and show it.
   - Approve → `spec.py approve --run <run> --gate 3` → step 4.
   - Changes → classify with `references/classify.md`, then
     `spec.py directive --run <run> --gate 3 --text "<verbatim>" --structural true|false`;
     `skeleton` → step 1.2, `resolve` → step 2 (unchanged functions come back `[cached]`).

## 4. Emit → Gate 4

1. `emit.py bundle --run <run> --out <project>/faasr_output` (project = the run's
   `project_dir`). It writes `workflow.json`, the functions, `spec.json`, `external_inputs.json`
   and a README, validates with FaaSr_py (or structural checks if FaaSr_py cannot run), and
   records `bundle_dir` and `validation`. It refuses to write if a secret value would leak.
2. If `validation.ok` is false: when `spec.py status` shows `auto_emit_retries` = 0, run
   `spec.py directive --run <run> --gate emit --text "FaaSr validation failed: <error>"` and go
   back to step 2 (resolve) once automatically; otherwise show the error and ask the user.
3. **Gate 4**: `spec.py show --run <run> --gate 4`, print its `markdown` exactly, then ask.
   - Approve → `spec.py approve --run <run> --gate 4`; for each id in `reused_catalog_ids`,
     `catalog.py increment-usage --run <run> --id <id>`; then step 5.
   - Changes → classify, `spec.py directive --run <run> --gate 4 ...`, route as at Gate 3.

## 5. Finalize

Not built yet (milestone M6, `wda-finalize`). After Gate 4 approval, tell the user the bundle
path and that the run is at `stage: finalize`.

## Asking at a gate

Print the gate markdown first, unchanged. Then use AskUserQuestion with the options
**Approve** and **Stop here**, noting that typing in "Other" requests changes. Without
AskUserQuestion, ask in chat: `approve` / `yes` / `y` / `lgtm` approve; `stop` stops;
anything else is a change request.

- **Stop** → `spec.py set-stage --run <run> --stage stopped`; tell the user how to resume.
- **Iteration cap**: when `directive` returns `"cap_reached": true`, do not loop again
  silently. Ask: approve the current version as-is, stop, or raise the cap
  (`spec.py set-cap --run <run> --max N`).
- **Override**: if the user disputes your STRUCTURE/FUNCTION call, move the run with
  `spec.py set-stage --run <run> --stage skeleton|resolve` and continue from that stage.

## Rules

- You write only `work/*.json` (and the sketch temp file). Everything else in the run is
  written by the scripts or by `curate-mga` subagents.
- Never paraphrase a gate display, never skip a gate, never approve on the user's behalf.
- Keep chat short between gates: one line on what you are doing, then the gate.
