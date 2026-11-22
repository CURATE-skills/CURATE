# Stage: emit (FaaSr workflow.json, bundle, validation) → Gate 4

Paths below are relative to `<plugin>`; `spec.py …`, `emit.py …`, `catalog.py …` are the script
shorthand.

Run scripts in the plain form `uv run --quiet <plugin>/scripts/<script>.py …`: no variable assignments, no `&&` chains, no subshells.

Precondition: `spec.py begin --run <run> --stage emit` returned `ok` (it checked that every
function has code generated for the current spec). If the run is already at `gate4`, skip to
step 3.

1. `emit.py bundle --run <run> --out <project>/faasr_output` (`<project>` = the run's
   `project_dir` from `spec.py status`). The bundle goes to a new folder
   `faasr_output/<workflow>-<run_id>/` (`-2`, `-3`… if emitted again; an existing folder is never
   overwritten), recorded as `bundle_dir`. It writes `workflow.json`, the functions, `spec.json`,
   `external_inputs.json` and a README; validates with FaaSr_py (or structural checks if FaaSr_py
   cannot run); records `bundle_dir` and the validation with the spec's hash; and moves the run to
   `gate4`. It refuses to write anything if a secret value would leak.
2. If `validation.ok` is false and `spec.py status` shows `auto_emit_retries` = 0:
   `spec.py directive --run <run> --gate emit --text "FaaSr validation failed: <error>"` (the run
   goes back to `reuse`), then finish as `gates.md` "After the gate decision" says. Otherwise show
   the error at Gate 4 and ask.
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
   - After any approval: for each id in `reused_catalog_ids`,
     `catalog.py increment-usage --run <run> --id <id>`.
   - Changes → classify with `<plugin>/references/prompts/classify.md`, then
     `spec.py directive --run <run> --gate 4 --text "<verbatim>" --structural true|false`.
     On `cap_reached`, follow `gates.md` "Cap".
   - Stop (the **Stop here** option, or the user typed "stop") → `spec.py set-stage --run <run>
     --stage stopped`.
4. Finish as `gates.md` "After the gate decision" says.
