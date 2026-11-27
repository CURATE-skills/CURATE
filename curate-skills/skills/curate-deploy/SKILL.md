---
name: curate-deploy
description: CURATE deploy stage — put a Gate 4-approved version on FaaSr (GitHub Actions), one deployment per version and a payload and S3 folder per run, asking the user before each of the four steps, or (Gate 4 "Approve & run on Pegasus") prepare and submit it to Pegasus/HTCondor, asking before each step; also lists this project's FaaSr deployments (--list) and removes one run's files or a version's whole deployment (--teardown). Run it by name; /curate runs it after Gate 4.
argument-hint: "--run <workflow-id>/<run-id> [--overwrite-own-deploy] | --list | --teardown --run <workflow-id>/<run-id> | --teardown --workflow <workflow-id> --all"
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
- **`--teardown --run <workflow-id>/<run-id>`** (find the run as in 1; do not run `spec.py begin`) or
  **`--teardown --workflow <workflow-id> --all`**. Below, `<target>` is `--run <run>`, or
  `--workflow <workflow-id> --all`. A run's teardown deletes its payload, its S3 outputs and logs; the
  version's deployment stays. `--all` deletes the deployment (function files, Actions files, its
  marker), every run's payload and every run's S3 outputs and logs. Local run folders and their
  `outputs/` are never deleted.
  1. `deploy.py teardown <target>` (deletes nothing). Print its `summary` exactly. If `ok` is
     false without `needs_confirmation`, show the `error` / `problems` exactly and stop (`--all`
     refuses while a run of the version may still be running, and names those runs).
  2. Ask once with AskUserQuestion — question `Tear down <deploy_name>? <repo_files> repo files,
     <log_objects> log objects and <output_objects> output objects are listed above.` (the
     `counts`), header `Teardown`, options:
     **Keep outputs (default)** — delete only the repo files and FaaSr logs;
     **Delete all** — outputs too;
     **Download outputs, then delete** — saved first (a run: into its own `outputs/`; `--all`: to
     `<project>/teardown-outputs/<deploy_name>/`);
     **Cancel**.
     Without AskUserQuestion (or if the answer is a plain "yes" without a choice), the choice is
     **Keep outputs**; ask in chat and end your turn if there was no yes at all.
  3. **Only for `--all`, if `other_projects` is not empty:** ask separately with AskUserQuestion —
     question `The deployment's marker lists other projects using it. Remove it anyway?`, header
     `Teardown`, options **Remove it anyway** / **Cancel**. Only after **Remove it anyway**, add
     `--other-projects` below.
  4. Only after one of the first three choices in step 2, in the user's answer just now:
     `deploy.py teardown <target> --confirmed --plan <plan_id> --outputs keep|delete|download`
     (keep / delete / download, matching the choice), with the `plan_id` from step 1. Print its
     `message`, or its `error` exactly. Never pass `--confirmed` or `--other-projects` otherwise,
     and never delete anything by other means. **Cancel** → nothing is deleted.

  Stop after either.

## 1. Find the run

`--run <workflow-id>/<run-id>` (a run id works too; a session id stands for that session's current
run): `spec.py runs` lists the runs in this project as `runs`; use the matching `run` path as `<run>`.
Without it, ask for `--run`. To deploy an approved version again, start a new run of it:
`/curate-skills:curate --start-at deploy --workflow <workflow-id>` (or `--from-run <session_id>`).

## 2. Begin

`spec.py begin --run <run> --stage deploy`. If `ok` is false, show the `error` exactly — it
names what to do instead — and stop (a rewind needs the user's yes, as in every stage skill).

## 3. Do the stage

Read `${CLAUDE_PLUGIN_ROOT}/references/stages/deploy.md` and follow it exactly. The arguments
contain `--chain` only when /curate started this stage; that decides whether you stop at the end
(`${CLAUDE_PLUGIN_ROOT}/references/gates.md`, "After the gate decision").
