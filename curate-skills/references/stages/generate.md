# Stage: generate (one curate-mga subagent per function + stub tests) → Gate 3

Paths below are relative to `<plugin>`; `spec.py …`, `scratch.py …`, `stubtest.py …` are the
script shorthand.

Precondition: `spec.py begin --run <run> --stage generate` returned `ok`. It already checked
the spec and the foreground setting. If the run is already at `gate3`, skip to step 4.

1. Read `<plugin>/references/prompts/mga-spawn.md` and follow it exactly.
2. `scratch.py build --run <run>` — add `--resume` when `begin` returned `mode: resume`, which
   keeps functions that already finished.
3. Handle each node in the returned `order`, one at a time, as `mga-spawn.md` says (reused
   functions are only stub-tested; the rest get a `curate-mga` subagent each). Then
   `spec.py end --run <run> --stage generate`.
4. **Gate 3**: `spec.py show --run <run> --gate 3`, print its `markdown` verbatim as a message — never re-formatted (the user does not see tool results) — then its `review_line` (`Full review: <path>`) on its own line. **Print the content first, then ask; never ask before printing it**
   with AskUserQuestion — the returned `question`, `header` and `options` (Approve / Show the full
   code / Stop here), no previews — every time, also after a change request or a
   question from the user (`<plugin>/references/gates.md`, "Asking at a gate").
   - Show the full code → `spec.py show --run <run> --gate 3 --full`, print its `markdown`
     verbatim, then ask again (not a change request; no revision round). For one function, Read
     `<run>/functions/<N>/<N>.py` and show it.
   - Approve → `spec.py approve --run <run> --gate 3`.
   - Changes → classify with `<plugin>/references/prompts/classify.md`, then
     `spec.py directive --run <run> --gate 3 --text "<verbatim>" --structural true|false`
     (functions that still fit come back `[cached]`). On `cap_reached`, follow `gates.md` "Cap".
   - Stop → `spec.py set-stage --run <run> --stage stopped`.
5. Finish as `gates.md` "After the gate decision" says.
