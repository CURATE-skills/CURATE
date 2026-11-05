# Stage: finalize (summary, save to the catalog) → Gate 5

The local stand-in for CURATE's post-run Workflow Deployment Agent (the original's Gate 5
`save [functions|workflow|both]`, `orchestrator.py` 173-226 `_do_save`). v1 and v2 do not deploy.
`spec.py …`, `catalog.py …` are the script shorthand.

Precondition: `spec.py begin --run <run> --stage finalize` returned `ok` (it checked that the
bundle was validated and built from the current spec).

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
   something else): **Save functions**, **Save workflow**, **Save both**, **Don't save**, with the
   returned `compact` as each option's `preview`. Chat only without the tool. Never save without an
   explicit answer.
4. **Save** what was chosen: `catalog.py save-functions --run <run> --only-verified` and/or
   `catalog.py save-workflow --run <run>`. Only verified functions
   are saved: stub test `passed`, or `reused`.
5. **Report** the ids stored and anything skipped, then
   `spec.py set-stage --run <run> --stage done`. Mention that later runs using the same catalog
   folder can reuse these functions.

Entry metadata (from the original `_do_save`): the function spec (run-only fields dropped);
keywords = the name split on `_` plus the first five words of the description; provenance
`workflow:<workflow name>`; usage count 0. A workflow entry is upserted by name.
