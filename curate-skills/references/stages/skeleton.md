# Stage: skeleton (WCA pass 1) → Gate 1

You act as CURATE's Workflow Composition Agent. Paths below are relative to the plugin folder
given by the skill that sent you here (`<plugin>`); `spec.py …` is its script shorthand.

Run scripts in the plain form `uv run --quiet <plugin>/scripts/<script>.py …`: no variable assignments, no `&&` chains, no subshells.

Precondition: `spec.py begin --run <run> --stage skeleton` returned `ok` (fresh or resume).
If the run is already at `gate1`, skip to step 4.

1. Read `<plugin>/references/prompts/wca-system.md` and `<plugin>/references/prompts/wca-skeleton.md`.
2. `spec.py prompt-context --run <run> --stage skeleton`. Write the skeleton JSON to
   `<run>/work/skeleton.json` as `wca-skeleton.md` says (on a refinement round, revise the
   previous draft it returns).
3. `spec.py skeleton --run <run> --json <run>/work/skeleton.json`. On a parse error, fix the file
   and re-run.
4. **Gate 1**: `spec.py show --run <run> --gate 1`, print its `markdown` verbatim as a message — never re-formatted (the user does not see tool results) — then its `review_line` (`Full review: <path>`) on its own line. **Print the content first, then ask; never ask before printing it**
   with AskUserQuestion — the returned `question`, `header` and `options`, no previews — every time, also after a change request or a
   question from the user (`<plugin>/references/gates.md`, "Asking at a gate"). At Gate 1 the
   returned `question` holds the whole outline (the step blocks and the graph) followed by the
   question: pass it as the question text **exactly as returned** — don't shorten it, don't move
   the outline into a preview, and still print the `markdown` before asking.
   - Approve → `spec.py approve --run <run> --gate 1`.
   - Changes → `spec.py directive --run <run> --gate 1 --text "<verbatim>"`, then go back to step 2
     (still this stage).
   - Stop → `spec.py set-stage --run <run> --stage stopped`.
5. Finish as `gates.md` "After the gate decision" says.
