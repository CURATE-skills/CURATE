---
name: curate-monitor
description: CURATE monitor stage — watch a deployed workflow's invocation read-only (Actions runs, FaaSr logs, fresh output files) until it succeeds, fails or the time budget runs out. Run it by name; /curate runs it after curate-deploy.
argument-hint: "--run <run_id>"
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Bash(uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/*)
---

# /curate-monitor

Plugin folder (`<plugin>` in the stage file): `${CLAUDE_PLUGIN_ROOT}`.
Script shorthand: `spec.py …` means `uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/spec.py …`
(likewise `monitor.py`). Each prints JSON with `"ok"`; exit 1 = needs attention, 2 = bad input.

Arguments: `$ARGUMENTS`

## 1. Find the run

`--run <run_id>`: `spec.py runs` lists the runs in this project; use the matching `run` path.
Without it, ask for `--run`.

## 2. Begin

`spec.py begin --run <run> --stage monitor`. If `ok` is false, show the `error` exactly — it
names what to do instead — and stop (a rewind needs the user's yes, as in every stage skill).

## 3. Do the stage

Read `${CLAUDE_PLUGIN_ROOT}/references/stages/monitor.md` and follow it exactly. The arguments
contain `--chain` only when /curate started this stage; that decides whether you stop at the end
(`${CLAUDE_PLUGIN_ROOT}/references/gates.md`, "After the gate decision").
