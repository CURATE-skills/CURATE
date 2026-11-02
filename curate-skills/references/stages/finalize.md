# Stage: finalize (summary, save to the catalog) → Gate 5

The local stand-in for CURATE's post-run Workflow Deployment Agent (the original's Gate 5
`save [functions|workflow|both]`, `orchestrator.py` 173-226 `_do_save`). v1 and v2 do not deploy.
`spec.py …`, `catalog.py …` are the script shorthand.

Precondition: `spec.py begin --run <run> --stage finalize` returned `ok` (it checked that the
bundle was validated and built from the current spec).

1. **Summarize.** `spec.py summary --run <run>`; print its `markdown` exactly. For a deployed run
   it includes the monitor summary (Actions, outputs, the note that output correctness is not
   judged).
   - **Unconfirmed match.** If `monitor.py status --run <run>` shows `result`
     `success_unconfirmed` and `accepted` is empty, ask first, with AskUserQuestion — question
     "Some Actions runs were matched to this invocation by time only. Accept them as this
     run's?", header "Gate 5", options **Accept the unconfirmed match** / **Stop here**. Only
     after Accept: `monitor.py accept-unconfirmed --run <run> --confirmed`. Stop →
     `spec.py set-stage --run <run> --stage stopped`. Saving is refused until it is accepted.
2. **Preview the save** (nothing is written yet):
   - `catalog.py save-functions --run <run> --only-verified --dry-run`
   - `catalog.py save-workflow --run <run> --dry-run`

   Tell the user, briefly: the catalog folder; which functions would be stored, with their
   keywords; which would be skipped and why (`already in catalog`, `not verified`, `no code`);
   and whether the workflow entry would be new or would update one with the same name.
3. **Gate 5 — ask** with AskUserQuestion whenever it is available (also if the user first asked
   something else): **Save functions**, **Save workflow**, **Save both**, **Don't save**, with the
   preview from step 2 as context. Chat only without the tool. Never save without an explicit answer.
4. **Save** what was chosen, with the same commands minus `--dry-run`. Only verified functions
   are saved: stub test `passed`, or `reused`.
5. **Report** the ids stored and anything skipped, then
   `spec.py set-stage --run <run> --stage done`. Mention that later runs using the same catalog
   folder can reuse these functions.

Entry metadata (from the original `_do_save`): the function spec (run-only fields dropped);
keywords = the name split on `_` plus the first five words of the description; provenance
`workflow:<workflow name>`; usage count 0. A workflow entry is upserted by name.
