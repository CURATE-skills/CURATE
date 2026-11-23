# Stage: emit (workflow draft, validation) → Gate 4

Paths below are relative to `<plugin>`; `spec.py …`, `emit.py …`, `catalog.py …` are the script
shorthand.

Run scripts in the plain form `uv run --quiet <plugin>/scripts/<script>.py …`: no variable assignments, no `&&` chains, no subshells.

Precondition: `spec.py begin --run <run> --stage emit` returned `ok` (it checked that every
function has code generated for the current spec). If the run is already at `gate4`, skip to
step 3.

1. `emit.py bundle --run <run> --out <project>/faasr_output` (`<project>` = the run's
   `project_dir` from `spec.py status`). It writes the **workflow draft**, `<run>/draft/`
   (`workflow.yml` in CWL, `inputs.yml`, `functions/`), built from the approved spec by code; never
   write or edit CWL yourself. It validates the draft (the CWL validator, then the FaaSr workflow
   built from it in memory, checked like a FaaSr bundle), records `draft_dir` and the validation
   with the spec's hash, and moves the run to `gate4`. It refuses to write anything if a secret
   value would leak. It also still writes the older FaaSr bundle under `faasr_output/`
   (`bundle_dir`), which only runs approved before workflow versions use; nothing from Gate 4 on
   depends on it, and its `bundle_note`, if any, needs no action.
   If `draft.problems` is not empty, the spec cannot be expressed as a workflow (each problem says
   why, e.g. a step that depends on two steps' results): show them at Gate 4 and ask for a change.
2. If `validation.ok` is false and `spec.py status` shows `auto_emit_retries` = 0:
   `spec.py directive --run <run> --gate emit --text "workflow validation failed: <error>"` (the
   run goes back to `reuse`), then finish as `gates.md` "After the gate decision" says. Otherwise
   show the error at Gate 4 and ask.
3. **Gate 4**: `spec.py show --run <run> --gate 4`, print its `markdown` verbatim as a message — never re-formatted (the user does not see tool results) — then its `review_line` (`Full review: <path>`) on its own line. **Print the content first, then ask; never ask before printing it**
   with AskUserQuestion — the returned `question`, `header` and `options`, no previews. Use the
   options exactly as returned: **Approve & deploy to FaaSr (cloud)** / **Approve & run on Pegasus
   (<where> · <image>)** / **Approve & run on Pegasus with a different image…** / **Approve, no
   deploy** when the workflow can run on Pegasus (then the question says to type "stop" to stop);
   otherwise the "different image" option is missing and **Stop here** is an option —
   every time, also after a change request or a question from the user
   (`<plugin>/references/gates.md`, "Asking at a gate").
   - Approve & deploy to FaaSr (cloud) → `spec.py approve --run <run> --gate 4 --deploy yes` (the
     run goes to the deploy stage, which asks again before each of its four steps).
   - Approve & run on Pegasus (<where> · <image>) → `spec.py approve --run <run> --gate 4 --deploy
     pegasus` (the run goes to the deploy stage). If it is refused, show the `error` exactly (it
     names why the workflow cannot run on Pegasus, as the Gate 4 content did) and ask again.
   - Approve & run on Pegasus with a different image… → this run only; the settings file is never
     changed:
     1. Ask for the image address (in chat, or AskUserQuestion with the user typing it in "Other").
        Only the address; never guess one.
     2. `spec.py pegasus-image --run <run> --image '<address>'` (read-only). If `ok` is false, show
        the `error` exactly (it says which addresses this runtime accepts) and go back to 1.
     3. Print its `summary` exactly (the Pegasus summary with that image, package warnings
        included), then ask with AskUserQuestion: the returned `question`, `header` and `options`
        (**Yes, use this image** / **Choose another image** / **Back**).
     4. **Yes** → `spec.py approve --run <run> --gate 4 --deploy pegasus --image '<the returned
        image>'`. **Choose another image** → 1. **Back** → ask Gate 4 again (step 3 above).
   - Approve, no deploy → `spec.py approve --run <run> --gate 4 --deploy no` (straight to
     finalize, as before).
   - **Approving publishes the draft** as the workflow's next version, `<name>-v<N>`, in
     `<project>/.curate/workflows/<name>-v<N>/` (read-only; never edited). Two answers can come
     back instead, with nothing published; ask, then repeat the same `approve` with the answer:
     - the `error` says the workflow is **identical to an existing version** → AskUserQuestion,
       header "Duplicate": **Keep both (a new version)** → add `--on-duplicate keep`; **Use
       `<that version>`** (drop the new one) → add `--on-duplicate drop`.
     - the `error` says the **workflow name is already used** and this run is not a new version of
       it → ask as in the skeleton stage's step 0 ("Name taken"), record it with `spec.py set-name`,
       then approve again.
   - After any approval: tell the user in one line `Published <workflow_id> (<version_dir>)` from
     the result (for FaaSr, the bundle is built next: `<version_dir>/runs/<run-id>/bundle/`). Then
     for each id in `reused_catalog_ids`, `catalog.py increment-usage --run <run> --id <id>`.
   - Changes → classify with `<plugin>/references/prompts/classify.md`, then
     `spec.py directive --run <run> --gate 4 --text "<verbatim>" --structural true|false`.
     On `cap_reached`, follow `gates.md` "Cap".
   - Stop (the **Stop here** option, or the user typed "stop") → `spec.py set-stage --run <run>
     --stage stopped`.
4. Finish as `gates.md` "After the gate decision" says.
