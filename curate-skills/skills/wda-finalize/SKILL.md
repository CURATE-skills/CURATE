---
name: wda-finalize
description: Finish a CURATE run after Gate 4 — summarize the workflow and, on the user's approval, save its verified functions (and optionally the whole workflow) to the CURATE catalog for reuse. Local only; no deploy, no monitoring. Invoked by /curate after Gate 4, or directly with a run id.
argument-hint: "[run_id]"
allowed-tools: Read, Bash(uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/*)
---

# wda-finalize — summary and catalog save

The local stand-in for CURATE's post-run Workflow Deployment Agent (Gate 5). The original
summarizes a deployed run and lets the user `save [functions|workflow|both]` to the catalog
(CURATE-prototype `orchestrator.py` 173-226 `_do_save`, `tui/output_review.py`). v1 does not
deploy, so this summarizes the validated bundle and saves.

Scripts (shorthand as in /curate): `spec.py …` = `uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/spec.py …`,
likewise `catalog.py`.

Arguments: `$ARGUMENTS`

## Steps

1. **Find the run.** Use the run id from the arguments or from /curate. Without one, run
   `spec.py runs` and pick the newest run at `stage: finalize`; if there is none, say so and stop.
   Confirm with `spec.py status --run <run>` that `validation.ok` is true; if not, stop and
   tell the user to finish Gate 4 in /curate first.
2. **Summarize.** `spec.py summary --run <run>`; print its `markdown` exactly.
3. **Preview the save** (nothing is written yet):
   - `catalog.py save-functions --run <run> --only-verified --dry-run`
   - `catalog.py save-workflow --run <run> --dry-run`

   Tell the user, in a short list: the catalog folder (`catalog_dir`); which functions would be
   stored, each with its keywords; which would be skipped and why (`already in catalog`,
   `not verified`, `no code`); and whether the workflow entry would be new or would update an
   existing one with the same name.
4. **Ask** (AskUserQuestion; chat fallback): **Save functions**, **Save workflow**,
   **Save both**, **Don't save**. Never save without an explicit answer.
5. **Save** what was chosen, with the same commands minus `--dry-run`. Only verified functions
   are saved: stub test `passed`, or `reused` from the catalog or cache (catalog-sourced
   functions and byte-identical code are skipped, as in the original).
6. **Report** the ids stored and anything skipped, then `spec.py set-stage --run <run> --stage done`.
   Mention that the next /curate run with the same catalog folder can reuse these functions.

## Metadata written (from the original `_do_save`)

Each function entry: a new id; the function spec (name, description, inputs, outputs, PyPI
dependencies, secret names, rank, source, code; run-only fields dropped); keywords = the
name split on `_` plus the first five words of the description; provenance
`workflow:<workflow name>`; usage count 0. A workflow entry is upserted by workflow name,
keeping its id and usage count.
