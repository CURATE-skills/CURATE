---
name: curate
description: Build a FaaSr serverless scientific workflow from a natural-language sketch with CURATE: skeleton, catalog reuse, code generation, a validated bundle, approval gates. Use to build, generate or revise a FaaSr workflow or multi-step serverless pipeline. To change or redo part of an existing CURATE run, never run CURATE's scripts or rewind yourself: name the command, say what it redoes, and ask. Change some functions: /curate-skills:curate --start-at generate --from-run <run_id> --existing-code keep (new run keeping all approved code; the user types the change at Gate 3, only that function is redone). Regenerate all: same with --existing-code ignore. Alternative the user types: /curate-skills:curate-generate --run <run_id> (rewinds that run; 1 revision round). Other stages: replace generate, drop --existing-code. After an explicit yes to a /curate-skills:curate command you named, invoke this skill with its arguments. Gate decisions are the user's: never approve or submit a change for them.
argument-hint: "<workflow sketch> | --start-at <stage> (--from-run <run_id> | --from-bundle <folder> | --from-file <file> | --sketch \"<text>\" | --sketch-file <file>) | --resume <run_id> | --from <file> --stage <stage> [--catalog DIR] [--attach model.py] [--context FILE] [--mga-retries N] [--stubtest-mode strict|parity]"
allowed-tools: Read, Write, Glob, Grep, Bash(uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/*), Agent(curate-skills:curate-mga)
---

# /curate — the whole CURATE pipeline

/curate runs the CURATE stages in order — skeleton → reuse → resolve → generate → emit →
deploy (only after **Approve & deploy** at Gate 4) → finalize — by following the same stage files
the stage skills (`/curate-skills:curate-skeleton` …) use. It does no stage work of its own: `spec.py next` decides what comes next.

Plugin folder (`<plugin>` in the stage files): `${CLAUDE_PLUGIN_ROOT}`.
Script shorthand: `spec.py …` means `uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/spec.py …`
(likewise `catalog.py`, `scratch.py`, `emit.py`, `stubtest.py`). Each prints JSON with `"ok"`;
exit 1 = needs attention, 2 = bad input.

Arguments: `$ARGUMENTS`

## Start

- `--start-at <stage>` with exactly one source starts a **new** run; the source is never changed:
  `--from-run <run_id>` (copies that run's gate-approved results; `--start-at deploy` deploys
  that run's Gate 4-approved bundle), `--from-bundle <folder>`
  (generate or emit), `--from-file <file>` (see `stage-io.md`), `--sketch "<text>"` (write it to
  `.curate/sketches/<short-name>.txt` first) or `--sketch-file <file>`.
  1. `spec.py start --at <stage> <source flag> [--existing-code …] [init flags]`. It only
     creates the new run; nothing is done to the source. (`--dry-run` previews without creating.)
  2. If it refused, show the `error` exactly and stop — never guess another source or stage.
     A `needs_choice` gets the Keep / Rewrite / Ignore question below.
  3. **Ask before any stage work** — also when the user already said yes to the command:
     print the returned `summary` exactly as a message, then ask with AskUserQuestion — question
     "Start this run?", header "Start", options **Yes** and **Stop**, each with the summary as
     `preview`. Use AskUserQuestion whenever it is available; only without it (e.g. headless)
     end with "Reply **yes** to start or **stop** to leave it." and end the turn. Wait.
     - **Yes** → `spec.py confirm-start --run <new run>` (for `--start-at deploy` this records
       the user's Yes as the deploy choice), then the loop.
     - **Stop** → `spec.py set-stage --run <new run> --stage stopped`. Nothing is deleted: the
       source was never touched, and the new run stays, marked stopped and unchanged otherwise.
       Tell the user the `resume_command` it returns.
- `--resume <run_id>`: find it with `spec.py runs`; also run `scratch.py unlock --run <run>`
  (clears this run's leftover subagent lock, if any).
- `--from <file> --stage <stage>`: `spec.py import --stage <stage> --from <file> …` with the
  extra flags described in `${CLAUDE_PLUGIN_ROOT}/references/stage-io.md`.
- Otherwise the text is the sketch: write it verbatim to `.curate/sketches/<short-name>.txt`
  and `spec.py import --stage skeleton --from <that file>`.

With `import` or `start`, always add `--config-catalog-dir '${user_config.catalog_dir}'
--config-mga-retries '${user_config.mga_retries}' --config-stubtest-mode '${user_config.stubtest_mode}'`
in single quotes exactly as written, and map `--catalog` → `--catalog-dir`, `--attach` →
`--user-model`, `--context` → `--context-file`. Tell the user the run id and the settings.
- If `import` or `start` fails with `needs_choice: "existing_code"` (some functions already have
  code and you didn't say what to do with it): ask the user with AskUserQuestion — question "What
  should happen to the existing code of <functions>?", options **Keep**, **Rewrite**, **Ignore**
  with the returned `options` texts as descriptions — then rerun the same command with
  `--existing-code keep|rewrite|ignore`. If you cannot ask (no AskUserQuestion, e.g. a headless
  run), show the error and the three options and stop. Never choose for the user.

## The loop — standing instructions for this whole session

These hold until the run is done or stopped, including after the user answers a gate in a
later turn:

1. `spec.py next --run <run>` → `next_skill`, e.g. `curate-skills:curate-reuse`. If it is none,
   the run is done or stopped: summarize and stop (for a stopped run, give its `resume_command`).
2. Its stage is the part after `curate-` (`skeleton`, `reuse`, `resolve`, `generate`, `emit`,
   `deploy`);
   for `wda-finalize` it is `finalize`. Tell the user one line: `Stage: <stage>`.
3. `spec.py begin --run <run> --stage <stage>`. If refused, show the `error` exactly and stop.
4. Read `${CLAUDE_PLUGIN_ROOT}/references/stages/<stage>.md` and follow it **as if started with
   `--chain`**: at its end ("After the gate decision"), do not stop — go back to step 1.
5. At a gate, the turn ends while you wait for the user. When they answer, apply the decision
   as that stage file says, then go back to step 1.

Never pick the next stage yourself, never skip a gate, and never answer a gate for the user:
no approval, stop or change request on their behalf — not even a change they described before
the run started or one you worded for them. At a gate, show the display and wait; record only
what they type or choose there (`gates.md`, "Gate decisions are always the user's"). If they
described a change earlier, say what they can type at that gate; don't say you will submit it.
Stages that loop automatically here (a Gate 1 refinement, the emit retry back to reuse) just
show up as the next `next_skill`.
