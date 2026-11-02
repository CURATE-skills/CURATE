---
name: curate-deploy
description: CURATE deploy stage — put a Gate 4-approved bundle on FaaSr (GitHub Actions) under its own "-cs" name, asking the user before each of the four steps. Run it by name; /curate runs it after "Approve & deploy" at Gate 4.
argument-hint: "--run <run_id> [--overwrite-own-deploy]"
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Bash(uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/*)
---

# /curate-deploy

Plugin folder (`<plugin>` in the stage file): `${CLAUDE_PLUGIN_ROOT}`.
Script shorthand: `spec.py …` means `uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/spec.py …`
(likewise `deploy.py`). Each prints JSON with `"ok"`; exit 1 = needs attention, 2 = bad input.

Arguments: `$ARGUMENTS`

## 1. Find the run

`--run <run_id>`: `spec.py runs` lists the runs in this project; use the matching `run` path.
Without it, ask for `--run`. To deploy a bundle approved in an earlier run, start a new run from it:
`/curate-skills:curate --start-at deploy --from-run <run_id>`.

## 2. Begin

`spec.py begin --run <run> --stage deploy`. If `ok` is false, show the `error` exactly — it
names what to do instead — and stop (a rewind needs the user's yes, as in every stage skill).

## 3. Do the stage

Read `${CLAUDE_PLUGIN_ROOT}/references/stages/deploy.md` and follow it exactly. The arguments
contain `--chain` only when /curate started this stage; that decides whether you stop at the end
(`${CLAUDE_PLUGIN_ROOT}/references/gates.md`, "After the gate decision").
