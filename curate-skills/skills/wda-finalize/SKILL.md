---
name: wda-finalize
description: CURATE stage 6 of 6 — summarize a finished run and, only with the user's approval, save its verified functions and/or the workflow to the catalog (Gate 5; local, no deploy). Run it by name; /curate runs it as part of the full pipeline.
argument-hint: "[--run <run_id>]"
disable-model-invocation: true
allowed-tools: Read, Write, Glob, Grep, Bash(uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/*)
---

# /wda-finalize

Plugin folder (`<plugin>` in the stage file): `${CLAUDE_PLUGIN_ROOT}`.
Script shorthand: `spec.py …` means `uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/spec.py …`
(likewise `catalog.py`, `scratch.py`, `emit.py`, `stubtest.py`). Each prints JSON with `"ok"`;
exit 1 = needs attention, 2 = bad input.

Arguments: `$ARGUMENTS`

## 1. Find or create the run

- `--run <run_id>`: `spec.py runs` lists the runs in this project; use the matching `run` path.
- No `--run`: `spec.py runs`; use the newest run whose `stage` is `finalize`. If there
  is none, say so and stop. Starting from files is not supported here (a bundle has no
  stub-test results); see `${CLAUDE_PLUGIN_ROOT}/references/stage-io.md`.

## 2. Begin

`spec.py begin --run <run> --stage finalize`. If `ok` is false, show the `error` exactly — it
names what to do instead — and stop, with one exception: if it has `needs_confirmation` (the run
is already past this stage), ask the user whether to go back, stating what it costs (the error
says). Only if they clearly say yes, run `begin` again with `--rewind --confirmed`; never pass
`--confirmed` without asking. Otherwise note the `mode` (fresh, resume or rewind).

## 3. Do the stage

Read `${CLAUDE_PLUGIN_ROOT}/references/stages/finalize.md` and follow it exactly. The arguments
contain `--chain` only when /curate started this stage; that decides whether you stop at the end
(`${CLAUDE_PLUGIN_ROOT}/references/gates.md`, "After the gate decision").
