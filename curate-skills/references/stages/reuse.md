# Stage: reuse (catalog search + MCA decisions) → shows decisions

You act as CURATE's Module Candidate Agent. Paths below are relative to `<plugin>`; `spec.py …`
is the script shorthand. This stage has no gate.

Precondition: `spec.py begin --run <run> --stage reuse` returned `ok`.

1. Read `<plugin>/references/prompts/mca-candidates.md`, and `<plugin>/references/prompts/mca-user-model.md`
   if any node has a user model.
2. `spec.py prompt-context --run <run> --stage reuse`. It includes each node's BM25 catalog
   matches (top 5), any prior implementation, and the standing change requests. Decide every
   node independently, as `mca-candidates.md` says, and write `<run>/work/candidates.json`.
3. `spec.py apply-candidates --run <run> --json <run>/work/candidates.json`. On exit 2, fix the
   file and re-run. This moves the run to `resolve`.
4. **Show the decisions** as a table — function, source (`new`, `catalog`, `adapt`, `cached`,
   user model), and origin (`← [catalog] name` / `← [cache] name`) — plus every `note` and
   `repair` the command returned, and one line per non-`new` decision saying why.
5. Finish as `<plugin>/references/gates.md` "After the gate decision" says (there was no gate:
   just report `next` and stop, or return to /curate with `--chain`).
