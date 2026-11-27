# Stage: deploy on Pegasus (Gate 4: Approve & run on Pegasus)

Paths below are relative to `<plugin>`; `spec.py …`, `pegasus_deploy.py …` are the script shorthand.

Run scripts in the plain form `uv run --quiet <plugin>/scripts/<script>.py …`: no variable assignments, no `&&` chains, no subshells.

`<run>` is the run's `<workflow-id>/<run-id>`: for a session that reached this stage, `spec.py status
--session <session>` shows it as `reference`; a run started with `--start-at deploy` is its own. Every
command below takes it as `--run <run>` (spec.py too: it acts on the session the run belongs to).

Precondition: `spec.py begin --run <run> --stage deploy` returned `ok` and `spec.py status --run
<run>` shows `deploy_choice` `pegasus`.

Running on Pegasus writes files on this machine (and, with `CURATE_PEGASUS_HOST`, on that host) and
submits jobs to HTCondor. For a run whose Gate 4 published a version, everything goes to that
version's run, `<project>/.curate/workflows/<workflow-id>/runs/<run-id>/` (prepare builds
`bundle/` there from the approved version and writes `manifest.json`), and on the host to
`~/curate-runs/<workflow-id>/<run-id>/`. The folder holds `workflow.yml`, Pegasus's own workflow
(no host paths); the host writes its catalogs at launch. Runs approved before versions are refused.
**Each step is the user's decision, asked right before it runs.** A yes to
one step is never a yes to the next, and nothing said earlier replaces asking.

Settings (`CURATE_PEGASUS_HOST`, `CURATE_PEGASUS_RUNTIME`, `CURATE_PEGASUS_IMAGE`) are read by the
script from the environment or `~/.config/curate/.env`; never open that file. If a Pegasus tool is
missing, point the user to `/curate-skills:curate-doctor --pegasus`.

## Before the first step: permission for later turns

Only if `pegasus_deploy.py status --run <run>` shows no step done yet: `allow_deploy.py --check
--pegasus` (read-only; the same check as doctor --pegasus). If `ok` is false, ask once with
AskUserQuestion — question `The Pegasus run needs permission to run its scripts in later turns. Add
it to your settings?`, header `Permission`, options **Yes** / **No**:
- **Yes**: show the returned `command` exactly (e.g. `! uv run --quiet <plugin>/scripts/allow_deploy.py
  --pegasus`), for the **user** to type into Claude Code. **Never run it yourself**, by any means.
  Say it adds two rules (pegasus_deploy.py, monitor.py) to `~/.claude/settings.json`, backs the
  file up and shows the change, and the returned `takes_effect`. Wait for the user's next message.
- **No**: go on, and tell the user in one line each: in auto mode, the run will stop after the
  first step (auto mode blocks its later commands); manual mode (Shift+Tab) asks before each
  command instead.

Never edit a settings file yourself.

## 1. Preflight (read-only)

`pegasus_deploy.py preflight --run <run>`, plus `--inputs <folder>` if the user named a folder for
the workflow's input files. **Print its `summary` exactly as a message** (it names the image and
where it came from: chosen for this run at Gate 4 or "Start this run?", `CURATE_PEGASUS_IMAGE`, or
the built-in default; the image is checked here too). If `ok` is false, list
the `problems` exactly. If one says the workflow needs input files, ask the user for the folder
that holds them (only a path; never read the files), then run the preflight again with `--inputs`.
Otherwise stop (step 4).

## 2. The steps

The steps are in `steps`: `prepare`, `launch` on this machine; `prepare`, `copy`, `launch` with an
ssh host. `pegasus_deploy.py status --run <run>` → `next_step`. For that step:

1. `pegasus_deploy.py <step> --run <run>` (plus `--inputs <folder>` for prepare, as in the
   preflight) **without** `--confirmed`. It writes nothing and returns `needs_confirmation` with a
   `plan` (`what`, `where`, `paths`). If it returns `problems`, show them exactly and stop (step 4).
2. Tell the user, in your message, step N of M: the plan's `what`, `where`, and every path in `paths`.
3. Ask with AskUserQuestion — question `Pegasus step N/M: <what>?`, header `Pegasus N/M`, options
   **Yes, <step>** and **Stop here**. Without AskUserQuestion, ask in chat and end your turn.
4. Only if the user chose **Yes** for this step in their answer just now: the same command with
   `--confirmed`. Anything else is not a yes.
5. Show the returned `progress` line exactly (e.g. `prepared · launched`) and the `message`, then go
   back to `pegasus_deploy.py status`.

`launch` submits the workflow and returns at once: HTCondor's DAGMan runs it, and nothing waits.
The run moves to `monitor`, and `state.pegasus.run_dir` records the Pegasus run folder, so the
monitor finds it again later, also after a logout or reboot.

Never pass `--confirmed` without the user's yes to that step in their latest answer, and never pass
`--overwrite-own` unless the user chose it (below).

## 3. When a step fails

Show the `error` exactly; the run stays at `deploy`. What the step wrote is in the
run's `deploy-log.jsonl`. Ask with AskUserQuestion — **Retry, removing what this step wrote**
/ **Stop here** — and only after the first, repeat the step (from 2.1) with `--overwrite-own` added
to both calls. It removes only what that step itself logged; anything else stays refused.

## 4. Stopping

`spec.py set-stage --run <run> --stage stopped`; print its `stop_message` exactly (`/curate
--resume` first; the curate-deploy command after it continues with the next unfinished step).
Nothing is undone.

## 5. Done

Finish as `<plugin>/references/gates.md` "After the gate decision" says; `spec.py next` names
`curate-monitor`.
