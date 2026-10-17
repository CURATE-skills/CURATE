---
name: curate
description: Build a FaaSr serverless scientific workflow from a natural-language sketch with CURATE — skeleton, catalog reuse, code generation and a validated FaaSr bundle, with human approval gates. Use when the user asks to compose, build, generate or revise a FaaSr workflow or a multi-step serverless pipeline, or runs /curate.
argument-hint: "<workflow sketch> | --resume <run_id> | --from <file> --stage <stage> [--catalog DIR] [--attach model.py] [--context FILE] [--mga-retries N] [--stubtest-mode strict|parity]"
allowed-tools: Read, Write, Glob, Grep, Bash(uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/*), Agent(curate-skills:curate-mga)
---

# /curate — the whole CURATE pipeline

/curate runs the six CURATE stages in order — skeleton → reuse → resolve → generate → emit →
finalize — by following the same stage files the stage skills (`/curate-skills:curate-skeleton`
…) use. It does no stage work of its own: `spec.py next` decides what comes next.

Plugin folder (`<plugin>` in the stage files): `${CLAUDE_PLUGIN_ROOT}`.
Script shorthand: `spec.py …` means `uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/spec.py …`
(likewise `catalog.py`, `scratch.py`, `emit.py`, `stubtest.py`). Each prints JSON with `"ok"`;
exit 1 = needs attention, 2 = bad input.

Arguments: `$ARGUMENTS`

## Start

- `--resume <run_id>`: find it with `spec.py runs`; also run `scratch.py unlock --run <run>`
  (clears this run's leftover subagent lock, if any).
- `--from <file> --stage <stage>`: `spec.py import --stage <stage> --from <file> …` with the
  extra flags described in `${CLAUDE_PLUGIN_ROOT}/references/stage-io.md`.
- Otherwise the text is the sketch: write it verbatim to `.curate/sketches/<short-name>.txt`
  and `spec.py import --stage skeleton --from <that file>`.

With `import`, always add `--config-catalog-dir '${user_config.catalog_dir}'
--config-mga-retries '${user_config.mga_retries}' --config-stubtest-mode '${user_config.stubtest_mode}'`
in single quotes exactly as written, and map `--catalog` → `--catalog-dir`, `--attach` →
`--user-model`, `--context` → `--context-file`. Tell the user the run id and the settings.

## The loop — standing instructions for this whole session

These hold until the run is done or stopped, including after the user answers a gate in a
later turn:

1. `spec.py next --run <run>` → `next_skill`, e.g. `curate-skills:curate-reuse`. If it is none,
   the run is done or stopped: summarize and stop.
2. Its stage is the part after `curate-` (`skeleton`, `reuse`, `resolve`, `generate`, `emit`);
   for `wda-finalize` it is `finalize`. Tell the user one line: `Stage: <stage>`.
3. `spec.py begin --run <run> --stage <stage>`. If refused, show the `error` exactly and stop.
4. Read `${CLAUDE_PLUGIN_ROOT}/references/stages/<stage>.md` and follow it **as if started with
   `--chain`**: at its end ("After the gate decision"), do not stop — go back to step 1.
5. At a gate, the turn ends while you wait for the user. When they answer, apply the decision
   as that stage file says, then go back to step 1.

Never pick the next stage yourself, never skip a gate, and never answer a gate for the user.
Stages that loop automatically here (a Gate 1 refinement, the emit retry back to reuse) just
show up as the next `next_skill`.
