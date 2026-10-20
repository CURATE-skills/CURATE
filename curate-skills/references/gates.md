# Gates, routing, stopping and resuming (shared by every stage)

CURATE has five human gates. Each stage skill ends at its own gate (curate-reuse has none);
`spec.py next --run <run>` always names the skill for wherever the run is now.

| Stage skill | Run `stage` | Ends at | Original CURATE node (`orchestrator.py`) |
|---|---|---|---|
| `curate-skeleton` | `skeleton`, `gate1` | Gate 1 | `compose_skeleton`, `gate1_skeleton` |
| `curate-reuse` | `reuse` | shows decisions | `fca_resolve` (MCA part) |
| `curate-resolve` | `resolve`, `gate2` | Gate 2 | `fca_resolve` (WCA part), `gate2_candidates` |
| `curate-generate` | `generate`, `gate3` | Gate 3 | `fga_generate`, `gate3_code_review` |
| `curate-emit` | `emit`, `gate4` | Gate 4 | `gate4_deploy` (validate) |
| `wda-finalize` | `finalize` | Gate 5 | `gate5_output_review` (save) |

## Asking at a gate

Print the gate markdown (`spec.py show --run <run> --gate N`) exactly as returned. Then ask with
AskUserQuestion — options **Approve** and **Stop here**, noting that typing in "Other" requests
changes. Without AskUserQuestion, ask in chat: `approve` / `yes` / `y` / `lgtm` approve; `stop`
stops; anything else is a change request. Never approve on the user's behalf.

## Decisions

| Gate | Approve | Change request |
|---|---|---|
| 1 | `spec.py approve --gate 1` → `reuse` | `spec.py directive --gate 1 --text "<verbatim>"`, then redo the skeleton in this same stage (a refinement; not counted toward the cap) |
| 2 | `spec.py approve --gate 2` → `generate` | classify (`prompts/classify.md`), `spec.py directive --gate 2 --text "<verbatim>" --structural true\|false` → `skeleton` or `reuse` |
| 3 | `spec.py approve --gate 3` → `emit` | as Gate 2 with `--gate 3`; functions that still fit come back `[cached]` |
| 4 | `spec.py approve --gate 4` → `finalize` | as Gate 2 with `--gate 4` |

- **Cap.** If `directive` returns `"cap_reached": true`, nothing was changed. Ask: approve the
  current version as-is (`approve`), stop, or raise the cap (`spec.py set-cap --max N`, then repeat
  the same directive).
- **Stop.** `spec.py set-stage --run <run> --stage stopped` (records where it stopped).
- **Override a classification.** If the user disputes STRUCTURE/FUNCTION after it was recorded,
  go back with a rewind (below).
- **Rewinding** (going back to an earlier stage of a run that is already past it) is always the
  user's decision. Tell them what it costs — one revision round, and that stage plus every stage
  after it is redone (working code is kept for `[cached]` decisions; functions marked new are
  regenerated from scratch) — and ask. Only after a clear yes:
  `spec.py begin --run <run> --stage <S> --rewind --confirmed`. Never pass `--confirmed` without
  asking; `begin` refuses a rewind without it.

## After the gate decision

Run `spec.py next --run <run>` and tell the user the result in one line, e.g.
`Next: /curate-skills:curate-reuse --run <run_id>`.

- **Started standalone** (no `--chain` in the arguments): stop here. Do not start the next stage
  yourself, even if the user's reply was "approve".
- **Started by /curate** (`--chain` in the arguments): return to /curate, which continues with
  the skill `next` names.

## Starting and resuming a stage

Every stage starts with `spec.py begin --run <run> --stage <S>`:

| `mode` | Meaning |
|---|---|
| `fresh` | normal entry; S's earlier outputs (and everything after) were cleared |
| `resume` | S was already started, or the run is waiting at S's gate — continue (or just re-show the gate) |
| `rewind` | only with `--rewind --confirmed` after the user agreed: going back from a later stage; counts as one revision round |
| `refused` | show the `error` exactly and stop — it names the skill to run instead |
