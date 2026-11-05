---
name: curate-deploy
description: CURATE deploy stage — put a Gate 4-approved bundle on FaaSr (GitHub Actions) under its own per-run name, asking the user before each of the four steps; also lists this project's deployments (--list) and removes one run's deployment (--teardown). Run it by name; /curate runs it after "Approve & deploy" at Gate 4.
argument-hint: "--run <run_id> [--overwrite-own-deploy] | --list | --teardown --run <run_id>"
disable-model-invocation: true
allowed-tools: Read, Glob, Grep, Bash(uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/*)
---

# /curate-deploy

Plugin folder (`<plugin>` in the stage file): `${CLAUDE_PLUGIN_ROOT}`.
Script shorthand: `spec.py …` means `uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/spec.py …`
(likewise `deploy.py`). Each prints JSON with `"ok"`; exit 1 = needs attention, 2 = bad input.

Run scripts in the plain form `uv run --quiet <plugin>/scripts/<script>.py …`: no variable assignments, no `&&` chains, no subshells.

Arguments: `$ARGUMENTS`

## 0. --list or --teardown (not the deploy stage)

- **`--list`**: `deploy.py list`; print its `markdown` exactly and stop. It only reads this
  project's runs.
- **`--teardown --run <run_id>`** (find the run as in 1; do not run `spec.py begin`):
  1. `deploy.py teardown --run <run>` (deletes nothing). Print its `summary` exactly. If `ok` is
     false without `needs_confirmation`, show the `error` / `problems` exactly and stop.
  2. Ask once with AskUserQuestion — question `Tear down <deploy_name>? <repo_files> deploy files,
     <log_objects> log objects and <output_objects> output objects are listed above.` (the
     `counts`), header `Teardown`, options:
     **Keep outputs (default)** — delete only the deploy files and FaaSr logs;
     **Delete all** — outputs too;
     **Download outputs, then delete** — saved to `<project>/teardown-outputs/<deploy_name>/` first;
     **Cancel**.
     Without AskUserQuestion (or if the answer is a plain "yes" without a choice), the choice is
     **Keep outputs**; ask in chat and end your turn if there was no yes at all.
  3. Only after one of the first three in the user's answer just now:
     `deploy.py teardown --run <run> --confirmed --plan <plan_id> --outputs keep|delete|download`
     (keep / delete / download, matching the choice), with the `plan_id` from step 1. Print its
     `message`, or its `error` exactly. Never pass `--confirmed` otherwise, and never delete
     anything by other means. **Cancel** → nothing is deleted.

  Stop after either.

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
