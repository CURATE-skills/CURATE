# Stage: finalize (summary, save to the catalog) → Gate 5

The local stand-in for CURATE's post-run Workflow Deployment Agent (the original's Gate 5
`save [functions|workflow|both]`, `orchestrator.py` 173-226 `_do_save`). v1 and v2 do not deploy.
`spec.py …`, `catalog.py …` are the script shorthand.

Run scripts in the plain form `uv run --quiet <plugin>/scripts/<script>.py …`: no variable assignments, no `&&` chains, no subshells.

Precondition: `spec.py begin --run <run> --stage finalize` returned `ok` (it checked that the
workflow draft was validated and built from the current spec, and Gate 4 published it).

**An execution run** (started with `--start-at deploy`; `spec.py show --gate 5` returns
`execution_run: true`): print its `markdown` verbatim and its `review_line`, do step 2 if it
applies, then `spec.py set-stage --run <run> --stage done`. There is no question and nothing is
saved: the version was published (and saved) by the run that approved it. Steps 1, 3-5 do not apply.

1. **Gate 5 content.** `spec.py show --run <run> --gate 5`. Print its `markdown` verbatim as a
   message — the run summary (for a deployed run, the monitor result, and the note that output
   correctness is not judged) and **what would be saved**: the catalog folder, each function that
   would be stored (with its keywords) or skipped and why, and whether the workflow entry is new or
   updates one with the same name. Then print its `review_line` (`Full review: <path>`) on its own
   line. **Print the content first, then ask; never ask before printing it.** (If the preview says
   it could not run, use `catalog.py save-functions --run <run> --only-verified --dry-run` and
   `catalog.py save-workflow --run <run> --dry-run` and tell the user the same things.)
2. **Unconfirmed match.** If `monitor.py status --run <run>` shows `result` `success_unconfirmed`
   and `accepted` is empty, ask first, with AskUserQuestion — question "Some Actions runs were
   matched to this invocation by time only. Accept them as this run's?", header "Gate 5", options
   **Accept the unconfirmed match** / **Stop here**. Only after Accept: `monitor.py
   accept-unconfirmed --run <run> --confirmed`. Stop → `spec.py set-stage --run <run> --stage
   stopped`. Saving is refused until it is accepted.
3. **Gate 5 — ask** with AskUserQuestion whenever it is available (also if the user first asked
   something else): the returned `question`, `header` and `options` (**Save functions**, **Save
   workflow**, **Save both**, **Don't save**), no previews. Chat only without the tool. Never save without an
   explicit answer.
4. **Save** what was chosen: `catalog.py save-functions --run <run> --only-verified` and/or
   `catalog.py save-workflow --run <run>`. Only verified functions
   are saved: stub test `passed`, or `reused`. The workflow is saved as exactly the version Gate 4
   published (`workflow_id`), with its version folder, so the catalog and the project always agree;
   the catalog never numbers a workflow itself. A version already in the catalog is not stored
   twice (`already_in_catalog`).
5. **Report** the ids stored and anything skipped, then
   `spec.py set-stage --run <run> --stage done`. Mention that later runs using the same catalog
   folder can reuse these functions.

Entry metadata (from the original `_do_save`): the function spec (run-only fields dropped);
keywords = the name split on `_` plus the first five words of the description; provenance
`workflow:<workflow name>`; usage count 0. A workflow entry is upserted by name.
