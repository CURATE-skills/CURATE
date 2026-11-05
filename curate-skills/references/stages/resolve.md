# Stage: resolve (WCA pass 2: files, data flow, packages) → Gate 2

You act as CURATE's Workflow Composition Agent again. Paths below are relative to `<plugin>`;
`spec.py …` is the script shorthand.

Precondition: `spec.py begin --run <run> --stage resolve` returned `ok`. If the run is already
at `gate2`, skip to step 4.

1. Read `<plugin>/references/prompts/wca-system.md` and `<plugin>/references/prompts/wca-resolve.md`.
2. `spec.py prompt-context --run <run> --stage resolve`; write the complete WorkflowSpec to
   `<run>/work/resolved.json` as `wca-resolve.md` says.
3. `spec.py merge-resolved --run <run> --json <run>/work/resolved.json`. If any problem has
   severity `error`, fix `resolved.json` once and re-run; if errors remain, show them at Gate 2.
4. **Gate 2**: `spec.py show --run <run> --gate 2`, print its `markdown` verbatim as a message — never re-formatted (the user does not see tool results) — then its `review_line` (`Full review: <path>`) on its own line. **Print the content first, then ask; never ask before printing it**
   with AskUserQuestion — the returned `question` and `header`, options Approve / Stop here, each
   with the returned `compact` as `preview` — every time, also after a change request or a
   question from the user (`<plugin>/references/gates.md`, "Asking at a gate").
   - Approve → `spec.py approve --run <run> --gate 2`.
   - Changes → classify with `<plugin>/references/prompts/classify.md`, then
     `spec.py directive --run <run> --gate 2 --text "<verbatim>" --structural true|false`.
     If it returns `cap_reached`, follow `gates.md` "Cap".
   - Stop → `spec.py set-stage --run <run> --stage stopped`.
5. Finish as `gates.md` "After the gate decision" says. After a change request the next skill
   is curate-skeleton (structural) or curate-reuse (functional).
