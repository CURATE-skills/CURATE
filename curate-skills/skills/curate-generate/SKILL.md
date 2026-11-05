---
name: curate-generate
description: CURATE stage 4 of 6 — write and stub-test every function, one curate-mga subagent at a time, and stop at Gate 3. Run it by name; /curate runs it as part of the full pipeline.
argument-hint: "--run <run_id> | --from <spec.json> [--functions DIR] [--existing-code keep|rewrite|ignore] [--review] [--catalog DIR] [--mga-retries N] [--stubtest-mode strict|parity]"
disable-model-invocation: true
allowed-tools: Read, Write, Glob, Grep, Bash(uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/*), Agent(curate-skills:curate-mga)
---

# /curate-generate

Plugin folder (`<plugin>` in the stage file): `${CLAUDE_PLUGIN_ROOT}`.
Script shorthand: `spec.py …` means `uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/spec.py …`
(likewise `catalog.py`, `scratch.py`, `emit.py`, `stubtest.py`). Each prints JSON with `"ok"`;
exit 1 = needs attention, 2 = bad input.

Run scripts in the plain form `uv run --quiet <plugin>/scripts/<script>.py …`: no variable assignments, no `&&` chains, no subshells.

Arguments: `$ARGUMENTS`

## 1. Find or create the run

- `--run <run_id>`: `spec.py runs` lists the runs in this project; use the matching `run` path.
- `--from <file>`: create a run from your file —
  `spec.py import --stage generate --from <file> [--functions DIR] [--existing-code keep|rewrite|ignore] [--review] [--catalog-dir D] [--mga-retries N]
  [--stubtest-mode M] [--user-model P]... [--context-file P]...
  --config-catalog-dir '${user_config.catalog_dir}' --config-mga-retries '${user_config.mga_retries}'
  --config-stubtest-mode '${user_config.stubtest_mode}'`.
  Keep the `--config-*` values in single quotes exactly as written. Map `--catalog` →
  `--catalog-dir`, `--attach` → `--user-model`, `--context` → `--context-file`. Tell the user the
  run id, the settings and any `warnings`. Accepted files: `${CLAUDE_PLUGIN_ROOT}/references/stage-io.md`.
- If the import fails with `needs_choice: "existing_code"` (some functions already have code
  and you didn't say what to do with it): ask the user with AskUserQuestion — question "What
  should happen to the existing code of <functions>?", options **Keep**, **Rewrite**, **Ignore**
  with the returned `options` texts as descriptions — then rerun the same import with
  `--existing-code keep|rewrite|ignore`. If you cannot ask (no AskUserQuestion, e.g. a headless
  run), show the error and the three options and stop. Never choose for the user.
- Neither: ask for `--run` or `--from`. On exit 2, show the `error` exactly and stop.

## 2. Begin

`spec.py begin --run <run> --stage generate`. If `ok` is false, show the `error` exactly — it
names what to do instead — and stop, with one exception: if it has `needs_confirmation` (the run
is already past this stage), ask the user whether to go back, stating what it costs (the error
says). Only if they clearly say yes, run `begin` again with `--rewind --confirmed`; never pass
`--confirmed` without asking. Otherwise note the `mode` (fresh, resume or rewind).

## 3. Do the stage

Read `${CLAUDE_PLUGIN_ROOT}/references/stages/generate.md` and follow it exactly. The arguments
contain `--chain` only when /curate started this stage; that decides whether you stop at the end
(`${CLAUDE_PLUGIN_ROOT}/references/gates.md`, "After the gate decision").
