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
| `curate-deploy` | `deploy` | four confirmed steps (no gate) | `gate4_deploy` (`_register_and_invoke`, after the upload steps) |
| `curate-monitor` | `monitor` | a result (success / success, unconfirmed match / failure / timeout) | `_monitor_execution` (`WorkflowRunner`) |
| `wda-finalize` | `finalize` | Gate 5 | `gate5_output_review` (save) |

## Asking at a gate

Do all three, at every gate, every time — including after a change request, after the user
asked to see something, and after the user answered in chat:

1. `spec.py show --run <run> --gate N` (N = 1–5). **Print its `markdown` exactly as a message,
   then its `review_line`** (`Full review: <path>`, the same content saved in the run's
   `review/gate<N>.md`) on its own line. **Print the content first, then ask — never ask before
   printing it**, and never leave it only in a tool result: the user does not see tool results
   (they are collapsed), so the content must be in your own message. Copy it verbatim: never
   re-format, re-order, summarize or rebuild it (e.g. as one field per line), and add nothing
   inside it. When the user asks to see a gate's table again, run `spec.py show` again and print
   its `markdown` verbatim — not your own rendering of it.
   The content ends with its marker line (`— end of Gate N · run <id> —`); print that too.
2. **Ask with AskUserQuestion whenever it is available** — never switch to plain chat because the
   previous answer came in chat or through "Other". Keep the menu short: the returned `question`
   and `header`, and the returned `options` as the options (labels only). At Gate 1 the
   `question` also holds the outline: use it exactly as returned. **Never give an option a
   `preview`** — the full content is already printed above. Typing in "Other" requests changes.
3. If the answer is not a decision (e.g. "show me the table", "show me the code"), show what was
   asked and go back to step 2 — with AskUserQuestion again. **Show the full code** (Gate 3):
   `spec.py show --run <run> --gate 3 --full`, print its `markdown` verbatim, then ask again. It is
   not a change request and costs no revision round.

**Gate decisions are always the user's.** Never approve, stop or submit a change request on the
user's behalf — not even one the user described before the gate, or one you proposed the wording
for. At a gate, show the display and wait; record only what the user types or chooses at that
gate (`directive --text` is their answer verbatim). Don't say you will ask for a change at a
gate ("at Gate 3 I'll ask for …"); say what the user can type there.

Only when AskUserQuestion is not available (e.g. a headless run): after the printed content, end
your message with the question and the options, in chat — `approve` / `yes` / `y` / `lgtm`
approve; `stop` stops; anything else is a change request. Never approve on the user's behalf.

## Decisions

| Gate | Approve | Change request |
|---|---|---|
| 1 | `spec.py approve --gate 1` → `reuse` | `spec.py directive --gate 1 --text "<verbatim>"`, then redo the skeleton in this same stage (a refinement; not counted toward the cap) |
| 2 | `spec.py approve --gate 2` → `generate` | classify (`prompts/classify.md`), `spec.py directive --gate 2 --text "<verbatim>" --structural true\|false` → `skeleton` or `reuse` |
| 3 | `spec.py approve --gate 3` → `emit` | as Gate 2 with `--gate 3`; functions that still fit come back `[cached]` |
| 4 | `spec.py approve --gate 4 --deploy yes` → `deploy`; `--deploy no` → `finalize` | as Gate 2 with `--gate 4` |

- **Cap.** If `directive` returns `"cap_reached": true`, nothing was changed. Ask: approve the
  current version as-is (`approve`), stop, or raise the cap (`spec.py set-cap --max N`, then repeat
  the same directive).
- **Stop.** `spec.py set-stage --run <run> --stage stopped` (records where it stopped). Tell the
  user the `resume_command` it returns (and `resume_stage_command`, to resume just that stage).
- **Override a classification.** If the user disputes STRUCTURE/FUNCTION after it was recorded,
  go back with a rewind (below).
- **Rewinding** (going back to an earlier stage of a run that is already past it) is always the
  user's decision. First mention the alternative that costs nothing: a new run from this run's
  approved results, `/curate-skills:curate --start-at <stage> --from-run <run_id>` (the original
  stays as it is; to change only some functions, start at generate with `--existing-code keep`
  and ask for the change at Gate 3). Tell them what the rewind costs — one revision round, and
  that stage plus every stage after it is redone (working code is kept for `[cached]` decisions;
  functions marked new are regenerated from scratch) — and ask with AskUserQuestion whenever it
  is available (options **Yes, rewind** / **No**; chat only without the tool). Only after a clear yes:
  `spec.py begin --run <run> --stage <S> --rewind --confirmed`. Never pass `--confirmed` without
  asking; `begin` refuses a rewind without it.

**Deploy steps are the user's too.** curate-deploy asks before each of its four steps (publish,
upload, register, invoke); `deploy.py <step> --confirmed` only after the user's yes to that step in
their latest answer (`stages/deploy.md`).

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
