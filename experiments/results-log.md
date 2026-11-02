# curate-skills results log

One entry per test run. Record cost and turns per stage, and every write-guard denial with
whether it was legitimate (a real attempt to write outside the function's folder) or a false
positive (the target was inside the folder but the guard could not prove it).

- **Headless runs** (`claude -p --output-format json`): cost (`total_cost_usd`) and turns
  (`num_turns`) come from the CLI's JSON result.
- **Interactive runs**: run `/cost` before quitting and record the dollar figure and API/wall
  time. `python3 experiments/tools/transcript_usage.py <session-id>` sums the session and
  subagent transcripts, but transcripts do not always store a message's final usage, so its
  output-token counts are a **lower bound** (one `make_numbers` message with two Write calls
  shows 7 output tokens).
- **Model**: from `modelUsage` in the headless JSON result, or the `model` field of the
  transcript's assistant messages (main session and subagents).
- **Denials**: from `.curate/mga_guard.log` (entries without `"debug"`).

Claude Code 2.1.299 for every run.

## Summary

| # | Date | Run | Model | Mode | Stages | Cost (USD) | Turns | Guard denials (legit / false +) |
|---|---|---|---|---|---|---|---|---|
| 1 | 2026-10-15 | M3 guard smoke test | claude-opus-5-5 | headless | — | not captured | not captured | 2 (2 / 0) |
| 2 | 2026-10-15 | E2 MapReduce (paraphrased prompt), through Gate 2 | claude-opus-5-5 | headless, 2 turns | skeleton + Gate 1; resolve + Gate 2 | 0.309 + 0.664 = 0.973 | 10 + 9 = 19 | 0 |
| 3 | 2026-10-15 | M5 test 1: sequential spawning | claude-opus-5-5 | interactive, background subagents | generate (3 functions) | not captured | 17 main + 19 subagent messages | 3 (0 / 3) |
| 4 | 2026-10-15 | M5 test 2: foreground spawning | claude-opus-5-5 | interactive, `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` | generate (3 functions) | 0.72 | 13 main + 13 subagent messages | 0 |
| 5 | 2026-10-15 | Opus 4.8 availability check | claude-opus-4-8 | headless, 1 call | — | 0.184 | 1 | — |
| 6 | 2026-10-15 | E2 MapReduce (reconstructed prompt), Gates 1–4 | claude-opus-5-5 | headless, 5 turns, foreground subagents | all through Gate 4 | 2.369 | 54 | 0 |
| 7 | 2026-10-15 | E2 finalize (`/curate --resume` → `wda-finalize`) | claude-opus-5-5 | headless, 2 turns | Gate 5 (summary, save both) | 0.293 | 11 | — |
| 8 | 2026-10-15 | `curate-catalog` import seed + list | claude-opus-5-5 | headless, 1 call | — | 0.197 | 6 | — |
| 9 | 2026-10-15 | `curate-catalog where` (setting substitution check) | claude-opus-5-5 | headless, 1 call | — | 0.104 | — | — |
| 10 | 2026-10-15 | M7 behaviour test: retries 0, failing function, resume, iteration cap, stop | claude-opus-5-5 | headless, 2 sessions, 6 turns | Gates 1–3, resume, revise, stop | 1.158 + 0.277 = 1.435 | 44 | 0 |
| 11 | 2026-10-17 | v2-files V2 checks: skeleton / reuse / resolve from imports, wrong-order start | claude-opus-5-5 | headless, 4 sessions | one stage each | 0.286 + 0.330 + 0.334 + 0.103 = 1.053 | 35 | 0 |
| 12 | 2026-10-17 | v2-files: Gate 3 functional revision across 4 separate sessions | claude-opus-5-5 | headless, 4 sessions, 6 turns | generate → reuse → resolve → generate | 1.666 | 60 | 0 |
| 13 | 2026-10-17 | v2-files: first /curate end to end (E2, reconstructed prompt) | claude-opus-5-5 | headless, 1 session, 6 turns | all six stages | 2.920 | 70 | 0 |
| 14 | 2026-10-17 | V4: E2 via /curate (v2-files) | claude-opus-5-5 | headless, 1 session, 6 turns | all six stages | 2.831 | 69 | 0 |
| 15 | 2026-10-17 | V4: E2 stage by stage, one session per stage (v2-files) | claude-opus-5-5 | headless, 6 sessions, 11 turns | all six stages | 3.063 | 86 | 0 |
| 16 | 2026-10-17 | V4: fresh-session baseline (plugin loaded, one-word reply) | claude-opus-5-5 | headless, 2 sessions | — | 0.066 + 0.004 | 2 | — |
| 17 | 2026-10-18 | Bug from a user test: `/curate-skills:curate-generate --from …/spec.json` (bundle spec with inline code) | — | interactive (user) | generate | not recorded | — | — |
| 18 | 2026-10-19 | Finding from a user test: plain request "re-generate the code for run …" → Claude rewound the run itself, without asking | — | interactive (user) | generate (rewind) | not recorded | — | — |
| 19 | 2026-10-19 | Bug from the same test: after a rewind to generate, old code was placed in the folders of functions marked new | — | interactive (user) | generate | not recorded | — | — |
| 20 | 2026-10-19 | U0: re-test of the plain request "I want to re-generate the code for run X" (commit `a5170ad`) | claude-opus-5-5 | headless, 3 fresh sessions | — | 0.131 | 3 | — |
| 21 | 2026-10-19 | U1: missing `--existing-code` with a spec that has code (headless) | claude-opus-5-5 | headless, 2 sessions | generate import | 0.117 + 0.137 = 0.254 | 5 | — |
| 22 | 2026-10-19 | U4: `/curate --start-at` from a run and from a bundle; plain request after the description change | claude-opus-5-5 | headless, 11 sessions | generate / emit | 2.925 | 85 | 0 |
| 23 | 2026-10-19 | U4 finish: "Start this run?" confirmation (yes / stop), resume commands, Gate 3 kept labels | claude-opus-5-5 | headless, 2 sessions × 2 turns | emit (from bundle) | 0.284 + 0.190 = 0.474 | 16 | 0 |
| 24 | 2026-10-20 | U5: docs; plain-request evidence summary and U2/U3 recommendation | — | — | — | 0 | — | — |
| 25 | 2026-10-20 | User's interactive weather run (`20261020-215732-corvallis-winter-weather`): gate-display bugs, plus what worked | claude-opus-5-5 | interactive (user) | all six stages | not recorded | — | — |
| 26 | 2026-10-20 | Fix check: every gate prints its display and asks with its table (headless, small squares workflow) | claude-opus-5-5 | headless, 6 turns | all six stages | 1.892 | 56 | 0 |

## Open items

- **Marketplace install check: deferred** (2026-10-16, not publishing yet). Install the plugin from
  the local marketplace and confirm whether `${user_config.*}` settings are filled in, which
  `--plugin-dir` does not do (run #9). Procedure: plain `claude`;
  `/plugin marketplace add /Users/laurakuo/curate`; `/plugin install curate-skills@curate-local`
  with `catalog_dir=/tmp/curate-mkt-test`; then `/curate-skills:curate-catalog where` and record
  the exact command run; uninstall afterwards.

## Catalog check (2026-10-15)

Verified from each run's pinned `state.json` `catalog_dir`: every run used a temporary catalog
in the session scratchpad — #2 `e2cat`, #3–#4 `m5cat` (never created), #6–#7 `e2fullcat`
(holds the 5 E2 functions + 1 workflow), #8 `e4cat`. #1's run folder was deleted after the test
(it was created with `--catalog-dir`); #5 and #9 touched no catalog (#9 only read the default
location). The default `~/.curate/catalog` does not exist: nothing was ever written there.

## 1. M3 guard smoke test

- Throwaway project; one locked function `hello`; one `curate-mga` subagent asked to probe the guard.
- Output was plain text, so cost and turns were not captured. Later runs use `--output-format json`.
- Denials:
  1. Write to `<project>/probe_outside.txt` — **legitimate** (intentional probe).
  2. Bash `echo bad > <project>/probe_bash.txt` — **legitimate** (intentional probe).
- Confirmed: `agent_type` is `curate-skills:curate-mga`; `skills: [faasr-reference]` preloads with the bare name.

## 2. E2 MapReduce word count, through Gate 2

- Sketch: my paraphrase of the SeBS-Flow MapReduce workload plus the paper's two bullets
  verbatim. Superseded by the reconstructed prompt in `prompts/e2-mapreduce.md` (which fixes
  N=3 mappers and W=5000 words); this run is a pipeline check, not an E2 result.
- Empty temporary catalog (`CURATE_CATALOG_DIR`); settings: retries 3, stub tests strict.
- Session `c0bebd5f-a3f1-410d-8477-c79a6736a4b2`.

| Stage | Turn | Cost (USD) | Turns | Result |
|---|---|---|---|---|
| sketch → skeleton → Gate 1 | 1 | 0.309 | 10 | 6 nodes; `map_count` ×4, `reduce_counts` ×5 |
| approve → MCA + resolve → Gate 2 | 2 | 0.664 | 9 | all 6 `[new]`; `check` clean; display printed verbatim |

- Denials: none (no `curate-mga` ran).
- Notes: 6 functions vs the paper's 5 (`write_totals` added); the gate question fell back to chat
  (no AskUserQuestion in headless mode).

## 3. M5 test 1: sequential `curate-mga` spawning, interactive

- Run `20261015-213233-m5-sequential-spawn-test`: `make_numbers` → `square_part` ×2 → `sum_squares`.
- Session `c715fc0f-d19e-417d-bc22-8e6a81885f55`; subagents ran in the **background**.
- Result: all three passed strict stub tests on the first attempt (`total.txt` = 385); 0 write
  violations; subagent windows did not overlap (21:36:06–21:36:30, 21:37:01–21:37:25,
  21:40:05–21:40:31).
- **Problem**: the session went idle after each subagent finished until the user typed.

| Transcript | Assistant messages | Input | Output | Cache read | Cache write |
|---|---|---|---|---|---|
| main | 17 | 40 | 4,197 | 702,275 | 23,682 |
| `make_numbers` subagent | 6 | 12 | 748 | 64,963 | 16,072 |
| `square_part` subagent | 6 | 12 | 927 | 71,499 | 12,174 |
| `sum_squares` subagent | 7 | 14 | 1,042 | 85,735 | 12,349 |

- Denials (all **false positives**): each subagent first tried
  `cat > $D/<name>.py <<EOF` with `$D` pointing at its own folder; the guard cannot resolve `$D`
  and denies by design. Each then succeeded with the Write tool.
  1. `make_numbers` — false positive
  2. `square_part` — false positive
  3. `sum_squares` — false positive
- Fix applied: `curate-mga` now told to use Write/Edit and literal absolute paths in Bash.
- Also found: `make_numbers`' spec asked for 2 shards of a shared, unranked file; template fixed.

## 4. M5 test 2: foreground `curate-mga` spawning, interactive

- Run `20261015-220729-m5-foreground-test-squares` (same three functions as test 3).
- Session `927ecc28-3534-4f48-888b-df1c20f622d8`, launched with `env -u
  CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`; the variable came only from
  `.claude/settings.local.json` (`env`). `spec.py preflight` passed.
- Result: all three subagents ran in the **foreground**, in one turn, with no user nudges; all
  passed strict stub tests on the first attempt (`total.txt` = 385); 0 write violations; no
  overlap (22:12:04–22:12:11, 22:12:29–22:12:41, 22:12:59–22:13:12).
- Cost (from `/cost`): **$0.72**, 1m 35s API time, 2m 55s wall time.
- Denials: none (the Write/Edit rule removed the test-3 false positives).
- Finding: the Agent call returns only metadata; the `MGA_RESULT` report arrives as a separate
  message right after (the subagent's handback). The generate step must read that message.

| Transcript (lower bound) | Assistant messages | Output tokens | Cache read | Cache write |
|---|---|---|---|---|
| main | 13 | ≥3,862 | 487,888 | 45,921 |
| `make_numbers` subagent | 3 | ≥47 | 20,906 | 13,686 |
| `square_part` subagent | 5 | ≥1,008 | 53,446 | 10,630 |
| `sum_squares` subagent | 5 | ≥1,108 | 52,516 | 10,089 |

## 5. Opus 4.8 availability

- `claude -p --model claude-opus-4-8` answered; `modelUsage` reported `claude-opus-4-8`.
  Cost $0.184 for a one-word reply (mostly prompt-cache creation).
- So the paper's model is available for comparison runs: launch with `--model claude-opus-4-8`;
  `curate-mga` uses `model: inherit`, so its subagents follow.

## 6. E2 MapReduce, reconstructed prompt, through Gate 4

- Prompt: `prompts/e2-mapreduce.md` (**reconstructed**: SeBS-Flow §5 MapReduce paragraph +
  the paper's two bullets). Empty temporary catalog; retries 3; stub tests strict.
- Headless, `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`; turn 1 = the sketch, turns 2–5 = `approve`
  (no revisions). Session `d8df3b2b-1f1d-4b8d-ad80-9a55dc466cab`, run `20261015-222147-mapreduce-we-base-our`.
- `total_cost_usd` in a resumed session is cumulative, so per-stage cost = difference.

| Stage | Turn | Cost (USD) | Turns | Wall (s) | Result |
|---|---|---|---|---|---|
| sketch → skeleton → Gate 1 | 1 | 0.213 | 10 | 42 | 5 nodes: `generate_text`, `split`, `map` ×3, `shuffle`, `reduce` ×5 |
| approve → MCA + resolve → Gate 2 | 2 | 0.324 | 8 | 50 | all `[new]`; check clean |
| approve → generate (5 `curate-mga`) → Gate 3 | 3 | 1.595 | 29 | 283 | 5/5 passed strict stub tests on attempt 1; 0 violations |
| approve → emit → Gate 4 | 4 | 0.158 | 5 | 23 | bundle valid at level `faasr_py` |
| approve → finalize (not built) | 5 | 0.079 | 2 | 21 | stopped at `stage: finalize` |
| **Total** | | **2.369** | **54** | **419** | |

- Correctness (local stub run only, not deployed): reducers produced bird, cat, dog, horse, pig
  = 1000 each (W=5000, M=5), the expected counts.
- Functions generated: 5, matching the paper's Table I for E2.
- Denials: none. 5 subagents, one at a time, no overlap.
- Bug found: Gate 4 listed 2 of 5 `<FAASR_*>` placeholders (regex excluded digits, so the
  `S3` names were missed); fixed, with a test.

## 7. E2 finalize and catalog save

- Session `bcf0b7ad-5b49-4765-a3b3-7ba92c644e61`: turn 1 `/curate-skills:curate --resume 20261015-222147-mapreduce-we-base-our`
  (resumed at `stage: finalize` → `wda-finalize`), turn 2 `Save both`.

| Stage | Turn | Cost (USD) | Turns | Result |
|---|---|---|---|---|
| resume → summary + dry-run save preview | 1 | 0.217 | 8 | summary printed verbatim; 5 functions would be stored, none skipped |
| `Save both` → save + done | 2 | 0.076 | 3 | 5 function entries + 1 workflow entry; run `stage: done` |
| **Total** | | **0.293** | **11** | |

- Catalog (`e2fullcat`) exported as the E4 seed:
  `curate-skills/tests/fixtures/catalog-e2-mapreduce/` (5 functions, 1 workflow; code from
  run #6, Opus 5.5).
- BM25 on the seed with E4-style step queries: split, map, reduce rank first; shuffle ranks
  third (still in the top 5 the MCA sees); unrelated queries ("visualize…", "extract words from
  words.pdf") return only weak hits, which the MCA must turn down.
- Keywords include filler ("the", "(n=3") — the original's rule (name parts + first five
  description words), kept as is.

## 8–9. `curate-catalog` skill

- #8: imported `tests/fixtures/catalog-e2-mapreduce` into a fresh folder (6 entries: 5
  functions, 1 workflow; none skipped) and listed it correctly.
- #9 finding: with `--plugin-dir`, `${user_config.catalog_dir}` in skill text reaches the
  session **unsubstituted**, even with `"default": ""` in `plugin.json`. Inside double quotes
  zsh fails on it ("bad substitution"); both sessions silently dropped the flag. Fix: the skills
  now single-quote the placeholders, so the literal reaches the script, which treats it as unset
  (verified under zsh). Open question for M7: whether settings are filled in when the plugin is
  installed from the marketplace rather than loaded with `--plugin-dir`.

## 10. M7 behaviour test

- Sketch: two functions; `slow_step` must `time.sleep(90)`, which exceeds the 60 s stub-test
  limit, so it fails deterministically. `--mga-retries 0`, stub tests strict, temp catalog `m7acat`.
- Session A `8c8dd30d-8414-4c7f-a9e4-6df8199b10a0` (turns 1–3); session B `bcfd7bcc-04d2-46c4-a81f-58bf36490426` (turns 4–6, a fresh session resuming the run).
- Before session B: `spec.py set-cap --max 1`; and `set-stage gate3` by hand, because the
  skill did not yet set that stage (bug found here, fixed for future runs).

| Stage | Turn | Cost (USD) | Turns | Wall (s) | Result |
|---|---|---|---|---|---|
| sketch → Gate 1 | 1 | 0.252 | 10 | 43 | 2 nodes |
| approve → Gate 2 | 2 | 0.209 | 8 | 41 | both `[new]` |
| approve → generate → Gate 3 | 3 | 0.697 | 16 | 475 | `make_greeting` passed; `slow_step` **failed, 1 attempt** (60 s timeout); exactly 2 `curate-mga` spawns |
| new session: `/curate --resume` | 4 | 0.150 | 4 | 14 | Gate 3 re-shown (iteration 0/1) |
| "Remove the 90-second sleep…" | 5 | 0.066 | 3 | 10 | classified FUNCTION → `resolve`; `cap_reached` → asked raise / approve as-is / stop, did not loop |
| "stop" | 6 | 0.061 | 3 | 9 | `stage: stopped`; directive kept |
| **Total** | | **1.435** | **44** | **592** | |

- Verified: retries 0 → one spawn per function, failing function recorded `failed` (attempts 1)
  and shown at Gate 3; resume in a new session; the cap stops the loop; stop.
- The subagent kept the required 90 s sleep (did not shorten it to pass). In turn 3 the session
  also re-ran the stub test with `--timeout 150` as a diagnostic (passed); the recorded result
  stayed `failed`.
- Bugs found and fixed: (1) the stage was not set to `gate3` after generation nor `gate4` after
  emit, so a resume would have re-entered generation; (2) Gate 3's failure note showed a
  mid-word slice of the output; it now shows the last error line.

## 11. v2-files V2 checks (branch `v2-files`, commit `18633cb`)

First runs of the stage skills. Each check is its own headless session in its own project;
fixtures from `curate-skills/tests/fixtures/e2-stages/` (from run #6).

| Check | Invocation | Catalog | Session | Cost (USD) | Turns | Wall (s) | Result | Run stage after |
|---|---|---|---|---|---|---|---|---|
| A | `/curate-skills:curate-skeleton --from sketch.txt` | empty | `703ebb30-075c-437b-9a6a-00257f0070aa` | 0.286 | 12 | 35 | Gate 1 shown (5 functions; map ×3, reduce ×5), waited | `gate1` |
| B | `/curate-skills:curate-reuse --from skeleton.json` | E2 seed | `b77e9bbd-16d7-4025-8cc2-6a0114eb2e61` | 0.330 | 9 | 32 | all 5 reused from the catalog unchanged, reason per function; printed next skill and stopped | `resolve` |
| C | `/curate-skills:curate-resolve --from skeleton.json --candidates candidates.json` | empty | `f719eff6-eee4-4a13-beff-0db76661d927` | 0.334 | 11 | 38 | Gate 2 shown, no problems, waited | `gate2` |
| D | `/curate-skills:curate-resolve --run <A's run>` (wrong order) | — | `52c2e394-20d1-41c7-8711-b4baa2a1a5a7` | 0.103 | 3 | 10 | refused (below); run unchanged | `gate1` |

- D's error: `the run has not reached resolve yet (it is at stage gate1); run
  /curate-skills:curate-skeleton --run 20261017-160422-sketch first`. Reworded afterwards to name
  the gate to approve.
- Transcripts: each session read only its stage file, the matching prompt files and `gates.md`;
  no Skill tool calls; stage commands in order (`import` → `begin` → `prompt-context` → work →
  `show`/`next`). Stage order correct in all four; no unprompted or wrong skill starts.
- Denials: none (no `curate-mga` ran).

## 12. v2-files: Gate 3 functional revision across separate sessions

- Project with an empty catalog. Each stage in a fresh headless session (turn 2 of S1 and S3 resumes
  that session to answer its gate). Run `20261017-161635-spec-resolved`.

| Session | Invocation | Cost (USD) | Turns | Result |
|---|---|---|---|---|
| S1 `65579c7e-e6f7-4513-9774-1fda73118f4f` | `/curate-skills:curate-generate --from spec-resolved.json --functions functions --functions-as reuse` | 0.329 | 14 | 5 imported functions stub-tested (no subagents), Gate 3 |
| S1 turn 2 | "In shuffle, sort the words alphabetically before partitioning." | 0.084 | 4 | classified FUNCTION, revision 1/5, next = curate-reuse, stopped |
| S2 `759681d4-e017-4275-82d0-a21b2319cd43` | `/curate-skills:curate-reuse --run …` | 0.301 | 11 | 4 × `cached ← [cache]`, `shuffle` `adapt ← [cache]` |
| S3 `1edb84d6-3b33-461f-bebb-94d608d6335b` | `/curate-skills:curate-resolve --run …` | 0.304 | 11 | Gate 2 (iteration 1/5), no problems |
| S3 turn 2 | "approve" | 0.051 | 2 | next = curate-generate, stopped |
| S4 `c08a6003-bb44-4b60-b76e-598974ac03e3` | `/curate-skills:curate-generate --run …` | 0.597 | 18 | 4 cached functions stub-tested only; `shuffle` regenerated by 1 subagent, passed attempt 1; Gate 3 |
| **Total** | | **1.666** | **60** | |

- `[cached]` after the revision: `generate_text`, `split`, `map`, `reduce`. Regenerated: `shuffle`
  (its existing code already sorted words; the subagent found nothing to change).
- Guard log: exactly 1 `curate-mga` spawn (`shuffle`), 0 denials. Reducers: 1000 each.
- Stage order correct; no unprompted or wrong skill starts (each session ran only the stage it was given).

## 13. v2-files: first /curate end to end (E2, reconstructed prompt)

- Session `502083f2-f797-4664-bc23-0c3d549483ab`, empty catalog, retries 3, strict. Turn 1 = the sketch;
  turns 2–5 = `approve`; turn 6 = `Save both`.

| Stage | Turn | Cost (USD) | Turns | Wall (s) |
|---|---|---|---|---|
| sketch → skeleton → Gate 1 | 1 | 0.317 | 13 | 38 |
| approve → reuse → resolve → Gate 2 | 2 | 0.413 | 15 | 58 |
| approve → generate (5 `curate-mga`) → Gate 3 | 3 | 1.701 | 27 | 281 |
| approve → emit → Gate 4 | 4 | 0.196 | 7 | 16 |
| approve → finalize: summary + save preview (Gate 5) | 5 | 0.164 | 5 | 16 |
| `Save both` → saved, done | 6 | 0.128 | 3 | 14 |
| **Total** | | **2.920** | **70** | **424** |

- Stage order from the transcript: skeleton → (approve) → reuse → resolve → (approve) → generate
  (5 spawns) → (approve) → emit → (approve) → finalize → (Save both). `spec.py next` before every
  stage after the first; reuse continued into resolve without stopping; no Skill tool calls; no
  stage skipped or repeated; no unprompted or wrong starts.
- Results: 5 functions generated, all passed strict stub tests on attempt 1; FaaSr_py validation
  passed; reducers 1000 each; 5 functions + 1 workflow saved to the temp catalog; run `done`.
- Guard log: 5 spawns, one at a time, 0 denials.
- Compared with v1 on the same prompt (runs #6 + #7: $2.369 + $0.293 = $2.662, 65 turns):
  v2-files $2.920, 70 turns. One run each; not a measured difference.

## 14–16. V4: E2 on v2-files via /curate and stage by stage (commit `ee64eda`)

Reconstructed E2 prompt; each run in its own project with an empty catalog; retries 3, strict;
every gate answered `approve`, Gate 5 `Save both`. Both runs ran at the same time.

### 14. Via /curate (one session `6b7e8639-c29f-49c0-8fbb-36cbf41ae99a`)

| Stage | Turn | Cost (USD) | Turns | Wall (s) |
|---|---|---|---|---|
| sketch → skeleton → Gate 1 | 1 | 0.331 | 13 | 41 |
| approve → reuse → resolve → Gate 2 | 2 | 0.456 | 15 | 69 |
| approve → generate (5 spawns) → Gate 3 | 3 | 1.548 | 26 | 255 |
| approve → emit → Gate 4 | 4 | 0.225 | 8 | 29 |
| approve → finalize: summary + preview | 5 | 0.167 | 5 | 21 |
| `Save both` → done | 6 | 0.104 | 2 | 12 |
| **Total** | | **2.831** | **69** | **426** |

### 15. Stage by stage (one fresh session per stage skill; gate answered by resuming it)

| Stage skill session | Session | First turn (USD) | Gate answer (USD) | Stage total (USD) | Turns | Wall (s) |
|---|---|---|---|---|---|---|
| skeleton | `81c6b133-9d6c-4a19-b204-f0d7bd3c2b40` | 0.282 | 0.052 | 0.334 | 12 | 37 |
| reuse | `a2cd200b-066b-4d99-a1d7-405c09fed607` | 0.290 | — | 0.290 | 10 | 39 |
| resolve | `4d18c77b-96c9-4862-b34a-9b96a85e94bc` | 0.332 | 0.050 | 0.382 | 13 | 44 |
| generate | `b34a0f73-42dd-4da3-8c01-23d10e2db235` | 1.521 | 0.058 | 1.579 | 30 | 269 |
| emit | `c884b960-7011-4a3f-87d4-7b5e46bb6cc8` | 0.174 | 0.058 | 0.233 | 11 | 23 |
| finalize | `2d168f11-4c9b-444f-a45b-02f7cf768d07` | 0.152 | 0.093 | 0.245 | 10 | 28 |
| **Total** | | | | **3.063** | **86** | **441** |

### 16. Startup cost of a fresh session

A fresh headless session with the plugin loaded, asked to reply `OK`: **$0.066**
the first time (prompt cache cold) and **$0.004** right after (cache warm).
Stage by stage used 5 more sessions than /curate, so the pure startup share is about
$0.02–0.33 of the difference; the rest is each session re-reading its stage file, `gates.md`
and prompts, and finding the run.

### Comparison (single runs; observations, not measured differences)

| | v1 /curate (#6 + #7) | v2-files /curate (#13) | v2-files /curate (#14) | v2-files stage by stage (#15) |
|---|---|---|---|---|
| Cost (USD) | 2.662 | 2.920 | 2.831 | 3.063 |
| Turns | 65 | 70 | 69 | 86 |
| Functions generated | 5 | 5 | 5 | 5 |
| Stub tests (attempt 1) | 5/5 | 5/5 | 5/5 | 5/5 |
| Reducer counts | 1000 each | 1000 each | 1000 each | 1000 each |
| FaaSr_py validation | passed | passed | passed | passed |
| Saved to catalog | 5 + workflow | 5 + workflow | 5 + workflow | 5 + workflow |

- Stage order (transcripts): #14 skeleton → reuse → resolve → generate → emit → finalize, with
  `spec.py next` before each stage after the first; #15 each session ran only its own stage
  and stopped with the next skill named. No Skill tool calls, no unprompted or wrong starts,
  no stage skipped or repeated in either run.
- Guard: 5 spawns each, one at a time; 0 denials.
- Stage by stage cost +8% vs /curate in this pair of runs; turns +17.

### Line counts (v1 tag vs v2-files, `experiments/tools/line_counts.py --compare`)

| Bucket | v1 | v2-files | Change |
|---|---|---|---|
| `spec.py` alone | 1,343 | 1,816 | +473 |
| Vendored Python | 1,537 | 1,541 | +4 |
| Ported/new Python (`spec`, `scratch`, `runstate`, hook) | 2,256 | 2,804 | +548 |
| **Python total** | **3,793** | **4,345** | **+552** |
| Tests | 1,467 | 1,902 | +435 |
| Skill, stage and reference text | 1,058 | 1,369 | +311 |
| **All** | **6,318** | **7,616** | **+1,298** |

## 17. Bug: bundle spec with inline code silently used as seeds (found in a user test, 2026-10-18)

- **What happened** (reported by the user): `/curate-skills:curate-generate --from
  faasr_output/normal_chunk_stats/spec.json` — a bundle `spec.json` with inline code and no
  `--functions` flag — placed the old code in the function folders as seeds, labelled every node
  `[new]`, and still spawned 3 `curate-mga` subagents that only wrote `deps.txt` files.
- **Cause**: `spec.py import` only applied `--functions-as` to code from a `--functions` folder.
  Code inside the spec kept the spec's `source: new`; at generate, `new` nodes with code are
  regenerated with that code as the "previous implementation", and Gate 3 showed `[new]`.
- **Fix (chosen: require an explicit choice)**: at generate, any function that has code — from
  `--functions` or inline in the spec — needs `--functions-as reuse` (`[cached] ← [import]`,
  stub-tested only, no subagents) or `--functions-as seed` (`[adapt] ← [import]`, regenerated
  from that code). Without it, the import stops with an error naming the functions and both
  options; no run is created. Gate 3 now shows what actually happens.
- **Related fix**: bundles were named after the workflow (`faasr_output/<workflow>/`), so two
  runs of the same workflow (e.g. one imported from the other's `spec.json`) competed for the
  same folder name (the second got `-2`, untraceable to its run). The folder is now
  `faasr_output/<workflow>-<run_id>/`, recorded as `bundle_dir` in `state.json`; emitting again in
  the same run adds `-2`, `-3`…, and an existing non-empty folder is never overwritten (as before).
- **Tests**: `test_generate_spec_with_inline_code_needs_explicit_choice`,
  `test_generate_inline_code_labels_match_what_happens` (reuse and seed: sources, which nodes get
  a subagent, Gate 3 tags), `test_bundle_folder_is_per_run_and_never_overwritten` (two runs →
  two folders; same run twice → `-2`, first folder untouched, `bundle_dir` updated). The existing
  `test_bundle_never_overwrites` already covered the `-2` suffix. Mutation check: restoring the
  silent seeding fails 2 tests; dropping the run id fails 1.
- **Renamed afterwards** (2026-10-19): `--functions-as reuse|seed` became `--existing-code
  keep|rewrite|ignore` (`ignore` = drop the code and generate from scratch); the old spelling is a
  hidden alias. Gate labels for imported code are now `kept: your code` and `rewritten from your
  code` instead of `[cached] ← [import]` / `[adapt] ← [import]`.

## 18. Finding: a plain request led to an unasked rewind (user test, 2026-10-19)

- **What happened** (reported by the user): in project `curate-tests`, the user typed plainly "I
  want to re-generate the code for run 20261018-215939-normal-stats", naming no skill. Claude did
  not point to a stage command; it ran `spec.py` itself and rewound the run to generate, using
  revision round 1 of 5, without asking first. The scripts allowed it.
- **Why**: the stage skills cannot be started by Claude (`disable-model-invocation`), so nothing
  steered it to them, and `spec.py begin --rewind` needed no confirmation.
- **Fix**:
  - `spec.py begin --rewind` now refuses without `--confirmed`, changing nothing; the refusal has
    `needs_confirmation` and states the cost ("going back to generate uses one revision round
    (0 of 5 used) and redoes generate and every stage after it … Ask them; only if they say yes,
    run begin again with --rewind --confirmed"). The refusal for a run past the stage now says
    "Ask the user first".
  - The six stage skills and `gates.md` ("Rewinding") say to ask the user, state the cost, and
    pass `--confirmed` only after a clear yes. `--confirmed` is an instruction-level guard: it
    makes the step explicit and visible, but a model that ignores instructions could still pass it.
  - `/curate`'s description (always visible to Claude) now says: for a request to redo one stage of
    an existing run, don't run CURATE's scripts; tell the user the stage command, e.g.
    `/curate-skills:curate-generate --run <run_id>`. The README has a "Redoing a stage" paragraph.
- **Tests**: `test_rewind_without_confirmation_changes_nothing` (state byte-identical after the
  refused rewind); `test_rewind_needs_flag_moves_code_and_counts` updated to pass `--confirmed`.
- **Not re-tested yet** with a live session: whether the description change makes Claude name the
  command instead of acting. That needs the same plain request in a fresh session.

## 19. Bug: old code placed in "new" functions after a rewind (same user test)

- **What happened**: after the rewind to generate, `scratch.py build` copied each function's
  previous `.py` into its folder (and added a "Previous Implementation" note) although the
  functions were marked `new`, so the subagents would have kept the old code. Same class of bug
  as #17.
- **Fix**, in two places:
  - `scratch.py build` places code in a function's folder only when the function is meant to
    start from it — reused (catalog/cached), rewritten (adapt), or a user model's original. A
    function marked `new` starts empty, with no "Previous Implementation" note, wherever stale
    code came from.
  - A confirmed rewind to generate (or earlier) removes the code of functions marked `new` from
    the spec; it is kept in `prior_spec` (now a real copy) for later `[cached]` decisions, so a
    failed regeneration cannot leave stale code for emit. Cached and adapt functions keep theirs.
- **Tests**: `test_rewind_to_generate_starts_new_functions_empty` (from gate4: new functions lose
  code and start empty; cached is reused untouched; adapt is seeded; prior_spec keeps all code),
  `test_build_never_seeds_a_new_function_with_code`. Mutation check: removing the `--confirmed`
  check, the strip on rewind, or the build guard each fails one test.
- **The user's run** (`curate-tests/.curate/runs/20261018-215939-normal-stats`) was not touched by
  these fixes; it was rewound under the old code.

## 20. U0: re-test of finding #18 (plain request, no skill named)

- Setup: a throwaway copy of run #14's project (`20261017-163538-mapreduce`, `stage: done`);
  plugin loaded with the same tool permissions as the user's test (`Skill Read Write Glob Grep
  Bash(uv run *) Agent`, so Claude *could* run `spec.py`); `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`.
  Prompt, verbatim: "I want to re-generate the code for run 20261017-163538-mapreduce."
- Repeated 3 times, each a fresh headless session.

| # | Session | Cost (USD) | Turns | Tool calls | Named the command | Stated the cost (revision round) |
|---|---|---|---|---|---|---|
| 1 | `478b2b10-1b52-4b49-adff-d2867d87266b` | 0.070 | 1 | none | yes | no |
| 2 | `29a17d41-2b60-4b97-98f7-a22278aec823` | 0.054 | 1 | none | yes | no |
| 3 | `3eab87c4-d5e8-4fa1-b355-f5a074d22c4d` | 0.007 | 1 | none | yes | no |

- Reply (run 1): "To re-generate the code for that run, type this command:
  `/curate-skills:curate-generate --run 20261017-163538-mapreduce`. It will ask you to confirm
  before it goes back to the code-generation stage. Anything produced after that stage, like the
  validated FaaSr bundle, may need to be rebuilt afterwards."
- Result: in 3/3 runs Claude named the stage command and did nothing else; `state.json` and every
  file in the project are byte-identical afterwards (sha1 of all files compared).
- Gaps: none of the replies stated the cost (one revision round), and none offered the safe
  alternative (a new run), which does not exist yet; both are planned for U4's description change.
- Caveat: headless, not interactive like the user's test; 3 runs only.

## 21. U1: asking for `--existing-code`

- Change: when code is present and `--existing-code` is missing, `spec.py import` creates nothing
  and returns `needs_choice: "existing_code"` with `options` (keep / rewrite / ignore, one line
  each), the `functions` concerned and the `flag`. The generate skill and `/curate` ask with
  AskUserQuestion and rerun with the answer; where they cannot ask, they stop and list the options.
- Headless check (no question tool), `tests/fixtures/e2-stages/spec-with-code.json`, empty catalog:

| Invocation | Session | Cost (USD) | Turns | Result | Runs created |
|---|---|---|---|---|---|
| `/curate-skills:curate-generate --from spec-with-code.json` | `498e40b8-428e-46c0-81dd-9218229c4c14` | 0.117 | 2 | stopped without choosing; named the 5 functions; listed Keep / Rewrite / Ignore; gave the command with `--existing-code keep` as an example; offered to redo the import if the user replies with a choice | 0 |
| `/curate-skills:curate --from spec-with-code.json --stage generate` | `1f52d233-ce03-4e99-9a95-d21f569232b1` | 0.137 | 3 | same: stopped, listed the options, example command | 0 |

- Tests: `test_missing_existing_code_returns_a_choice_to_ask` (fields, nothing created, no choice
  needed for a spec without code), `test_other_errors_carry_no_choice`.
- Not tested: the interactive path (AskUserQuestion in a live session); needs a user session.

## 22. U4: `/curate --start-at` with typed sources

Source for every check: a throwaway copy of run #14's project (`20261017-163538-mapreduce`,
`stage: done`, with its bundle `faasr_output/mapreduce_word_count`). Each check in its own copy.

| Check | Session | Cost (USD) | Turns | Result |
|---|---|---|---|---|
| A. `/curate-skills:curate --start-at generate --from-run 20261017-163538-mapreduce` | `efb64db1-b8a8-4b93-bec2-c91352b93dcd` | 1.559 | 30 | new run `20261019-224212-mapreduce-word-count`; copied the Gate 2 snapshot (no code); 5 `curate-mga` spawns, 5/5 passed strict stub tests on attempt 1; reducers 1000 each; stopped at Gate 3 |
| B. `/curate-skills:curate --start-at emit --from-bundle …/mapreduce_word_count` | `8e5b89ee-e480-4e00-a625-5d1c03e316e5` | 0.264 | 11 | new run `20261019-224713-spec`; bundle validated (`faasr_py`) into `faasr_output/mapreduce_word_count-20261019-224713-spec`; warned that unverified code won't be saved; stopped at Gate 4 |
| B, repeated after the summary changes (3×) | `1db359c8-8135-46fe-8549-9a2c1ad6d7b8`, `9dd15c24-3711-4c10-96d4-3b8cd5be45eb`, `f752e9f6-d094-4e3a-99c7-285f02350701` | 0.276 / 0.265 / 0.250 | 11 / 11 / 10 | same result each time; the "what will be used" summary was **not** printed (see below) |
| C. plain request "I want to re-generate the code for run …" (3×), first description | `dff37b72-f7cc-4312-b1bf-de550c445768`, `ccef7125-2292-4505-82ee-0296ee356c43`, `938cef98-62a3-4653-98d5-341797a32f2e` | 0.114 / 0.050 / 0.052 | 1 each | safe option first and the revision-round cost stated, **but an invented stage name** (`--start-at codegen`, `curate-codegen`): wrong commands; read-only Glob/Grep/`find` calls; no changes |
| C, after naming the stages in the description (3×) | `39ae04f5-dbf2-41a3-ba64-341b110dab69`, `ea72703f-4572-4115-a5b1-565f4f27216a`, `11b0933e-72a6-460a-9309-fae7a77aba87` | 0.075 / 0.010 / 0.010 | 1 each | **3/3 correct**: no tool calls; exact `/curate-skills:curate --start-at generate --from-run …` first; exact `/curate-skills:curate-generate --run …` second with "1 revision round"; no changes |

- Source untouched: in every check the source run folder and the source bundle are byte-identical
  afterwards (sha1 of every file); new bundles went to new per-run folders.
- Stage order: A start → next → begin(generate) → 5 spawns → end; B start → next → begin(emit).
  No Skill tool calls, no unprompted or wrong starts.
- **Unmet: "print what will be used before starting".** `spec.py start` returns a ready-made
  `summary` (markdown: source, snapshot/file, functions with code and stub-test results, what is
  copied and redone, settings). In 4 headless attempts with three wordings (print after the dry
  run; print as the next message after `start`; print as part of the loop's first `Stage:` line),
  the session never printed it — it went from the tool call straight on, at most with its own
  one-line "new run …" message. Interactive sessions not tested.
- Tests: `tests/test_start.py`, 15 tests (each source and stage; dry run creates nothing; refusals
  for a gate never approved, a superseded approval, v1, partial and unknown ids, bundle without
  spec or code, wrong source for the stage, not exactly one source, `--existing-code` off
  generate; source byte-identical; `copied` record; summary). Mutation check: ignoring superseded
  approvals fails 1 test; copying the source's old code fails 5; writing to the source fails 5.

## 23. U4 finish: confirmation before starting, resume commands, Gate 3 labels

- **Confirmation (fixes #22's unmet item):** after `spec.py start`, `/curate` shows the returned
  `summary` inside an AskUserQuestion "Start this run? Yes / Stop" — or, headless, prints the
  summary and "Reply yes to start or stop to leave it." — and waits. Stop = `set-stage stopped` on
  the new run: nothing is deleted, the source was never touched, the new run stays (stopped, no
  stage work), and the resume command is given.

| Session | Turn | Cost (USD) | Turns | Result |
|---|---|---|---|---|
| Y `a7fe0c67-9774-4d3e-8138-5d493ec2eb0e` | `/curate --start-at emit --from-bundle …` | 0.137 | 2 | summary **printed**, then "Reply yes to start or stop" |
| Y | `yes` | 0.147 | 9 | emit ran; bundle valid (`faasr_py`) in a new per-run folder; Gate 4 |
| N `49ab3074-e630-4c14-b705-fe9d3cb19570` | same | 0.134 | 2 | summary printed, asked |
| N | `stop` | 0.056 | 3 | new run `20261019-225851-spec` marked stopped (from emit), nothing deleted; gave `/curate-skills:curate --resume …` and `/curate-skills:curate-emit --run …` |

- Source run and source bundle byte-identical afterwards in both sessions.
- **Bug found and fixed**: the summary of a real (not dry-run) start from a bundle or file showed
  "Workflow: `None`, 0 functions" and an empty "Redoes" — the import path did not fill those
  fields. Now filled; test `test_real_start_from_bundle_summary_is_complete`.
- **Resume commands** (from the user's interactive U1 test): for a stopped run, `spec.py next`,
  `status` and `set-stage stopped` return `resume_command` (`/curate-skills:curate --resume <id>`)
  and `resume_stage_command` (the stage skill for where it stopped). Test
  `test_stopped_run_names_the_resume_command`.
- **Gate 3 labels for kept code** (from the same test): confirmed `kept: your code` after the full
  generate stage (build → stub-test → record → end). The "Stub test" column now says
  `passed (not regenerated)` / `FAILED (not regenerated)` instead of `reused` / `reused_failed`
  (display only; state unchanged). Test `test_gate3_after_a_kept_import_says_kept_and_passed`.

## 24. U5: docs, and the plain-request evidence behind the U2/U3 decision

Docs: README "Start points" and "Redoing a stage" sections (new run first, rewind second with its
cost); `references/stage-io.md` "Start points" (sources, which approval is used, what is and is
not copied, refusals); `references/gates.md` "Rewinding" now offers the new-run alternative first.

Plain request "I want to re-generate the code for run <id>" (no skill named), all evidence so far:

| Entry | `/curate` description | Sessions | Ran CURATE scripts / changed the run | Named a correct command | Safe option first | Stated the cost |
|---|---|---|---|---|---|---|
| #18 | original (no guidance) | 1 (user, interactive) | **yes** — rewound the run, used round 1/5 | — | — | — |
| #20 | "name the stage command; don't run scripts" | 3 (headless) | no (0 tool calls; files byte-identical) | 3/3 (`curate-generate --run`) | n/a (no new-run option yet) | 0/3 |
| #22 | "+ safe option first", generic `<stage>` | 3 (headless) | no (read-only Glob/Grep/`find` only) | **0/3** (invented `codegen`) | 3/3 | 3/3 |
| #22 | "+ stages named" (current) | 3 (headless) | no (0 tool calls; files byte-identical) | 3/3 (both exact) | 3/3 | 3/3 |

Since the description changed: 9/9 sessions changed nothing; the last 3/3 gave exactly the right
two commands in the right order with the cost. Limits: headless only, one phrasing, no session
that already had /curate or a stage skill loaded.

Recommendation:
- **U3 (transcript-parsing guard): not needed now.** The failure it targets (#18) has not recurred
  in 9 sessions, and the guard would depend on Claude Code's transcript format. Instead, keep a
  cheap regression check: a few phrasings ("redo the resolve step for run X", "go back to Gate 2
  in run X", "regenerate run X's code") in fresh headless sessions, about $0.05–0.10 each, run
  before each release and logged here.
- **U2 (enforced rewind confirmation): defer, but test the gap first.** Today `--confirmed` is
  instruction-level: nothing stops a model from passing it without asking. Not yet tested: a user
  inside a /curate or stage-skill session saying "go back and regenerate" — the case where the
  model has the commands loaded. If that test shows a rewind without a user yes, the smallest
  enforced version is: `begin --rewind` refuses with a short phrase (e.g. `rewind generate 3538`);
  a `UserPromptSubmit` hook (standard library, ~40 lines) records a confirmation only when the
  user's own message contains that phrase; `begin --rewind --confirmed` requires that record and
  consumes it. No transcript parsing, no PostToolUse hook, no write guard (forging the record
  would need a deliberate shell write, not an accident).

## 25. User's interactive weather run (v2-files, project `curate-tests/weather`)

Run `20261020-215732-corvallis-winter-weather`; session `857857f4-20d4-4fcc-a81f-dedce2ffbb03`.
A plain-language request ("Build me a FaaSr workflow that compares this winter's weather in
Corvallis, Oregon with the past 10 years…"), 3 functions (`download_ghcn_station`,
`process_weather_variable` ×3, `plot_weather_comparison`).

**What worked**
- The plain request started /curate without a slash command (its description triggered it).
- The Gate 2 change ("only the 10-year baseline window is extended by 30 days …") was classified
  FUNCTION and routed back through reuse → resolve; Gate 2 then showed iteration 1/5.
- All 3 functions passed strict stub tests on the first attempt, using real NOAA GHCN-Daily data
  (the download stub test fetched the real ~13 MB station CSV). Bundle passed FaaSr_py
  validation; functions and workflow saved to the project catalog; run `done`.

**Bugs (from the user and the transcript)**
1. **Gate displays not printed before the question (Gates 1 and 2).** The session ran
   `spec.py show`, then asked "Approve this skeleton?" with AskUserQuestion; the table was only in
   the tool result, which the user does not see. The user had to ask for the table at both gates.
2. **"Rank: 3" twice at Gate 1.** Not reproduced: neither `spec.py show --gate 1` for this run's
   approved skeleton nor anything the session printed contains a duplicated "Rank: 3" (the rank
   appears once in the Rank column and as "×3" in the diagram; a later message explains "rank 1 is
   PRCP, rank 2 is TMIN, rank 3 is TMAX"). Asked the user where it appeared; a regression test
   now pins one rank cell per function.
3. **Question tool dropped.** After the user typed "show me the gate 2 table first" into the
   question, the session switched to plain chat ("as a chat question this time") — and stayed in
   chat for every later gate (Gates 2 again, 3, 4 and 5).

**Fix**
- `spec.py show` also returns `question`, `header` and `compact` (a short plain-text table).
- `gates.md` "Asking at a gate" and every stage's gate step: (1) print the `markdown` as a message
  (tool results are not visible); (2) ask with AskUserQuestion whenever it is available — every
  time, also after a change request, a question or an answer in chat — with `compact` as the
  options' `preview`; (3) if the answer is a request to see something, show it and ask again with
  AskUserQuestion. Chat only without the tool, ending with the `compact` table and the question.
  Gate 5 (finalize) gets the same "whenever it is available" rule.
- Tests: `test_show_returns_question_header_and_compact_table` (Gates 1–4),
  `test_gate1_shows_each_rank_once`, `test_gate_procedures_always_print_then_ask_with_the_table`.

## 26. Fix check: tables printed and in the question at every gate (headless)

Small workflow ("Squares": `make_numbers`, `square_part` ×2, `sum_squares`), empty catalog; turns:
sketch, `approve` ×4, `Don't save`. Session `218dab58-f703-4548-959e-73a6d6eb19b5`, run `20261020-222644-squares`.
Headless has no AskUserQuestion, so this checks the chat fallback.

| Turn | Gate | Cost (USD) | Turns | Full display printed | Compact table + question at the end |
|---|---|---|---|---|---|
| 1 | 1 | 0.285 | 12 | yes | yes |
| 2 | 2 | 0.334 | 14 | yes | yes |
| 3 | 3 | 0.891 | 17 | yes | yes |
| 4 | 4 | 0.159 | 6 | yes | yes |
| 5 | 5 | 0.146 | 5 | yes | yes |
| 6 | `Don't save` → done | 0.077 | 2 | — | — |
| **Total** | | **1.892** | **56** | | |

- Each gate message: the `## Gate N` display, then the `compact` table in a code block and the
  question ("Approve the generated code? Reply approve … or stop …"). All functions passed on
  attempt 1; run `done`.
- Not covered here: the AskUserQuestion path (interactive only) — whether the preview shows the
  table and whether the tool is kept after an "Other" answer needs an interactive re-test.

## 27. User's run B (single-function change) and bugs 4–5

Run B `20261020-223413-spec`, started from run A (`20261020-215732-corvallis-winter-weather`,
#25) with `/curate-skills:curate --start-at generate --from-bundle <run A's bundle>
--existing-code keep`, to change only the plot to °F. Run B's history: `imported` (Gate 2),
`approve` 2, `revise` at Gate 3 "In plot_weather_comparison only, show max and min temperatures
in °F …" (FUNCTION), `approve` 2, 3, 4; `done`. Neither run nor their bundles were modified;
everything below used a copy of `.curate/`, `faasr_output/` and `catalog/` in the scratchpad.

**What worked (run B)**
- All 3 functions kept at the start: Gate 3 showed "kept: your code", 0 attempts, no subagents.
- The Gate 3 change was announced as FUNCTION before it was recorded.
- At reuse only `plot_weather_comparison` was marked `adapt`; the other two stayed `cached`
  (final stub tests: `reused`, `reused`, `passed` in 1 attempt).
- Exactly 1 subagent ran; the diff was minimal; the main session checked the figure.

**Bug 3, continued (question style).** In run B, several prompts were plain chat ("Reply yes /
approve / stop") although AskUserQuestion was available, including the "Start this run?"
confirmation. Fix: /curate's start confirmation now says to print the summary, then ask with
AskUserQuestion *whenever it is available, also when the user already said yes*, with the summary
as `preview`; the Keep / Rewrite / Ignore question covers `start` as well as `import`; the
rewind question in `gates.md` is an AskUserQuestion (**Yes, rewind** / **No**). Gates: #25's fix.

**Bug 4: wrong advice for "change one function in a finished run".** Claude suggested
`--start-at generate --from-run <run A>` or `curate-generate --run <run A>` saying only the plot
would be regenerated, then that `--from-run … --existing-code keep` carries the code over.
- Checked on the copy (dry run, before the fix): `uses history/003-gate2-approve.json`, all 3
  functions `new`, no code — `--existing-code` was **silently ignored** with `--from-run`, and
  both suggested commands regenerate all 3 functions. The advice was wrong.
- Fix (supported, not just documented): starting generate from a run whose Gate 3 was approved
  now needs `--existing-code`; without it, `needs_choice: "existing_code"` and nothing is
  created. `keep` / `rewrite` put the code of the latest Gate 3 approval snapshot onto the Gate 2
  spec by function name (`cached` / `adapt`); `ignore` generates everything (the old behaviour).
  `keep`/`rewrite` without an approved, unsuperseded Gate 3 is refused and points to
  `--from-bundle … --existing-code keep`. History adds `code_snapshot`, `code_sha256`,
  `existing_code`. The start summary has a "Generate:" line naming the kept functions and the
  ones that get a subagent.
- On the copy after the fix: `--existing-code keep` → uses Gate 2 snapshot + code from
  `004-gate3-approve.json`; `require` clean; `begin` fresh; `scratch.py build` → `generate`
  false for all 3 (no subagents); source copy byte-identical (hash before/after, and `diff -rq`
  against the original run).
- Advice: /curate's description, README ("Change one function", "Redoing a stage"), `gates.md`
  and `stage-io.md` now say: change some functions → `--start-at generate --from-run <id>
  --existing-code keep`, then ask for the change at Gate 3; regenerate all → `--existing-code
  ignore`; the rewind (`curate-generate --run`) regenerates the new functions and uses a round.
- Behaviour change to note: `--start-at generate --from-run X` without the flag now asks (or,
  headless, refuses) when X has approved code. Existing tests now pass `--existing-code ignore`.

**Bug 5: Claude refused to run the command it named after "yes, start it".** /curate's
description now says: never run scripts or rewind on a plain request; name the command, say what
it redoes, and ask; *after an explicit yes to a `/curate-skills:curate` command you named, invoke
this skill with its arguments*. "Start this run?" still follows. Stage-skill commands (the
rewind) still have to be typed (they are not model-invocable). Description shortened to 994
characters (≤ 1024 so the last rule isn't cut off in listings).

**Headless check** (copy of the weather project; session `5c15440b-7cdf-4e59-8030-d89de2c5f128`)

| Turn | Prompt | Cost (USD, cumulative) | What happened |
|---|---|---|---|
| 1 | "Run 20261020-215732-corvallis-winter-weather is finished. I only want the plot function changed to show temperatures in °F; leave the other functions as they are. How do I do that?" | 0.081 | no tool calls; named `--start-at generate --from-run … --existing-code keep`, said only the plot is redone after asking at Gate 3, gave the rewind alternative and its cost, asked "Should I start the new run?" |
| 2 | "yes, start it" | 0.166 | `Skill(curate-skills:curate, "--start-at generate --from-run … --existing-code keep")` → one `spec.py start` → printed the summary ("Generate: keeps … all 3 … no subagent") and "Reply **yes** to start or **stop**" (headless fallback); no stage work |

Source run copy unchanged. One wording to watch: turn 2 ended with "At Gate 3, I'll ask for
`plot_weather_comparison` to show temperatures in °F" — it should present Gate 3 and let the user
answer, not submit the change itself. Not run further (would cost a generate/emit round); worth
checking in the next interactive test.

**Bug 2 ("Rank: 3" twice):** still not reproduced in the script output or run A's transcript; the
regression test from #25 stays. Need to know where it appeared (Gate 1 table, the diagram, or a
message Claude wrote).

**Tests:** 215 pass (209 → 215): `test_from_run_at_generate_asks_about_the_approved_code`,
`test_from_run_at_generate_carries_the_gate3_code[keep|rewrite]`,
`test_from_run_at_generate_ignore_says_everything_is_written`,
`test_keep_without_approved_code_is_refused`, `test_curate_advice_and_confirmations`.
`claude plugin validate --strict`: passes (only the existing icon warning). Not covered
headlessly: the AskUserQuestion path (interactive only).

## 28. Gate decisions are the user's; gate displays verbatim (bug 2 found)

**Bug 2, explained by the user.** "Rank: 3" appeared twice in the Gate 1 table Claude showed
*after the user asked to see it*, in a per-function layout ("Function: … / Rank: 3 / Rank: 3 /
What it does: …") — not the script's table. So Claude re-rendered `spec.py show` output itself
and duplicated a line; the script was never at fault (consistent with #25/#27).
Fix: `gates.md` step 1 — copy the `markdown` verbatim; never re-format, re-order, summarize or
rebuild it (e.g. one field per line), add nothing inside it; when the user asks to see a gate's
table again, run `spec.py show` again and print its `markdown` verbatim. Every stage's gate step
now says "print its `markdown` verbatim as a message — never re-formatted".

**Gates are always the user's decision** (from #27's "At Gate 3, I'll ask for the plot change").
`gates.md` gains "Gate decisions are always the user's": never approve, stop or submit a change
request on the user's behalf — not even one described before the gate or one Claude worded; show
the display and wait; record only what the user types or chooses there; say what the user can
type, not that Claude will submit it. /curate's loop rules say the same, and its description ends
"Gate decisions are the user's: never approve or submit a change for them." (description
trimmed to 1001 characters; the trigger sentence is now "Use to build, generate or revise a
FaaSr workflow or multi-step serverless pipeline.").

**Headless re-check** (same two turns as #27, copy of the weather project; session
`eaa55bdc-58de-4f2c-b1c1-d18e8556f1de`): turn 1 $0.062, no tool calls, named the `keep` command,
"At Gate 3, you describe the change … Those decisions are yours, and I won't approve anything for
you", asked. Turn 2 ("yes, start it") $0.148 cumulative: `Skill(curate-skills:curate, …)` → one
`spec.py start` → summary, "At Gate 3 you can type your change, for example …", "Reply yes to
start or stop". Run A copy identical to the original. Minor: turn 1 described the rewind as
"allows only one revision round" (it *uses* one of the run's rounds) — wording only.

Tests: 216 pass (+`test_gates_are_the_users_and_displays_are_verbatim`; the stage-file test now
checks the "verbatim … never re-formatted" wording). `claude plugin validate --strict` passes.

## 29. D0: vendored deploy/runtime code and a read-only deploy preflight

Plan: deploy, monitor and a CURATE-style Gate 5 review (D0–D4). D0 writes nothing to GitHub.

**Vendored** (`scripts/vendor/`, 2,696 lines with headers): CURATE's `deploy/register_workflow.py`,
`invoke_workflow.py`, `native_containers.txt`, `faasr/runtime/*` and `faasr/artifacts.py`. A test
diffs each against `ref/curate`: the only differences are the 3-line header, relative imports,
`faasr_validate` for `validate_faasr_json`, and import-time logging to stderr instead of stdout.

**New:** `scripts/leakcheck.py` (49 lines: by-value, as emit, plus token/key patterns and
`redact`); `scripts/deploy.py` (391 lines, `preflight` only, stdlib, read-only GitHub GETs);
`tests/test_deploy.py` (299 lines, 26 tests). Suite 242 passed; plugin validates.

**How FaaSr finds function code** (asked before choosing the deploy layout):
- Local FaaSr_py 0.1.13 (used by CURATE's invoke) and FaaSr-Backend `main` (installed in the
  `ghcr.io/faasr/github-actions-python` image, built by FaaSr-Docker
  `faas_specific/github-actions.Dockerfile` with `FAASR_INSTALL_REPO=FaaSr/FaaSr-Backend` at a
  release tag; latest tag 2.1.0): a `FunctionGitRepo` entry ending in `.py` fetches only that one
  file (contents API, `ref` = branch); FaaSr-Backend writes it to `/tmp/functions/<InvocationID>/`
  and `faasr_import_function_walk` imports every `.py` in that folder (stem = module name) and
  returns the first that defines `FunctionName`. Non-`.py` entries download a repo/subfolder.
- So folders are never loaded wholesale for curate-skills' single-file entries, and each action's
  container sees only its own file. The per-workflow folder `functions/<Name>-cs/<fn>.py` keeps
  CURATE's `functions/*.py` untouched and the module name valid (a `<fn>-cs.py` stem would not be).
  The preflight refuses any `FunctionGitRepo` entry that is not a `.py` under that folder.
- Note: in 0.1.13 the raw file goes to `/tmp/functions/` but the walk searches
  `/tmp/functions/<InvocationID>`; only the container's version matters at run time.

**Preflight on a copy of run A** (`20261020-215732-corvallis-winter-weather`, copied with its
bundle into the scratchpad; the copy's `bundle_dir` points at the copied bundle). Offline: no
GitHub token or `FAASR_*`/`S3_*` variables in this session's environment, no `gh`, no `.env`.
- Deploy name `corvallis-winter-weather-comparison-cs`; targets
  `functions/corvallis-winter-weather-comparison-cs/{download_ghcn_station,process_weather_variable,plot_weather_comparison}.py`,
  `workflows/corvallis-winter-weather-comparison-cs.json`,
  `.github/workflows/corvallis-winter-weather-comparison-cs-{download-ghcn-station,process-weather-variable,plot-weather-comparison}.yml`.
- Secrets the YAML will import: `GH_PAT`, `S3_AccessKey`, `S3_SecretKey`.
- **Not ready:** run A's bundle still has emit placeholders in `ComputeServers.GH.UserName`,
  `.ActionRepoName`, `DataStores.S3.{Endpoint,Bucket,Region}` and `FunctionGitRepo.*` — it was
  emitted without the `FAASR_*` variables, so it could not be deployed by hand either. Plus no
  token / env here (GitHub checks skipped).
- Open decision for D1: fill exactly those `<FAASR_…>` placeholders from the environment in the
  deploy copy (non-secret values, shown in the preflight), or require re-emitting the bundle.

## 30. Preflight before D1: deploy-copy rule, S3 check, retrying a partial deploy

**Decisions (user):** the action repo is `laurakuo1006/CURATE-test`, shared with CURATE (its
state tagged `curate-baseline-weather` by the user); `FAASR_ACTION_REPO=CURATE-test` (CURATE
joins `FAASR_GH_USERNAME/FAASR_ACTION_REPO`). Run A deploys as
`corvallis-winter-weather-comparison-cs`; its `<FAASR_…>` placeholders are filled from the
environment in the deploy copy only, never in the approved bundle.

**Finding:** run A's functions write S3 outputs to the folder argument `corvallis_winter_weather`,
the same folder CURATE's run used; renaming only the workflow would have let the `-cs` run
overwrite CURATE's S3 outputs (FaaSr logs were already separate: `FaaSrLog/<WorkflowName>/…`).

**The deploy-copy rule** (enforced by `deploy.py preflight`, recorded in `scripts/VENDORED.md`):
- The deploy copy may differ from the Gate 4-approved bundle only in `WorkflowName` (+ suffix),
  each action's `Arguments.folder` (+ suffix), `FunctionGitRepo.*` (per-deployment folder
  `functions/<Name>-cs/`), and fields that held an emit `<FAASR_…>` placeholder. Any other
  difference is refused.
- The preflight summary shows a field-by-field table (field, approved value, deploy-copy value,
  reason) plus the locations that change only through the name: S3 log prefix, workflow JSON,
  function files and Actions files in the repo.
- Every path the deploy writes (repo and S3) must carry the suffix and must not exist yet;
  existing paths are listed (`refused_paths`). A workflow with external S3 inputs, or an action
  without a `folder` argument, is refused.
- Read-only S3 check: `head_bucket`, one-key listings of each prefix the run writes and of
  CURATE's own prefixes (reported, never touched).

**Retrying a partial deploy:** `--overwrite-own-deploy` (not the default) allows an existing path
only if this run's deploy log (`<run>/deploy/deploy-log.jsonl`, written by D1's steps) records it
for the same run id, deploy name and repo/branch (or bucket), and, in the repo, the file still
carries curate-skills' mark (commit tag / WorkflowName / YAML name). Everything else is still
refused; CURATE's paths are never targets.

Tests: 259 passed (without `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` set); `claude plugin validate
--strict` passes. Offline check on the run-A copy (`~/curate-tests/weather-deploy`, dummy values):
the table lists 12 changed fields (3 folders, 5 placeholders, 3 FunctionGitRepo, WorkflowName)
and 4 derived locations.

**Real preflight (user's `.env`, read-only):** not ready only because the secret check missed
`S3_AccessKey`/`S3_SecretKey`; the repo has them as `S3_ACCESSKEY`/`S3_SECRETKEY`. GitHub stores
secret names in upper case and resolves `${{ secrets.S3_AccessKey }}` without regard to case,
so the names were fine and the check was wrong; it now compares names case-insensitively (test
added; 260 passed). Everything else passed: token, push, default branch `main`, all 8 repo
paths and both S3 prefixes new, bucket `test-curate` reachable, diff within the allowed fields.
No CURATE files or S3 data exist under run A's names (CURATE's own run used its own names).

**Local S3 key spelling:** the local environment may now hold `S3_AccessKey` or GitHub's
`S3_ACCESSKEY` (same for `S3_SecretKey`); both set with different values is refused, naming the
two variables only. One reader (`deploy.env_key` / `s3_keys`) serves every S3 client deploy.py
builds and will serve curate-monitor's; the preflight summary says which spelling it read. The
leak checks (`leakcheck`, `emit.find_leaked_secrets`) and the stub-test env scrub now match secret
names without regard to case, so an upper-case spelling is never missed. Tests: camel only, upper
only, both equal, both different, plus leak/scrub of the upper-case name; 265 passed.

## 31. Real preflight: ready (run A copy → laurakuo1006/CURATE-test)

User's `.env`, read-only, run on the copy `~/curate-tests/weather-deploy` of run A
(`20261020-215732-corvallis-winter-weather`), at `d35bcf9`. Nothing was written to GitHub or S3.

- **Ready.** Deploys as `corvallis-winter-weather-comparison-cs` to `laurakuo1006/CURATE-test`,
  branch `main` (token: `GH_PAT`, accepted; push allowed; default branch `main`).
- All 8 repo paths new: `functions/corvallis-winter-weather-comparison-cs/{download_ghcn_station,
  plot_weather_comparison,process_weather_variable}.py`,
  `workflows/corvallis-winter-weather-comparison-cs.json`, three
  `.github/workflows/corvallis-winter-weather-comparison-cs-<action>.yml`.
- Both S3 prefixes new in bucket `test-curate` (us-east-1): `corvallis_winter_weather-cs/` and
  `FaaSrLog/corvallis-winter-weather-comparison-cs/`. S3 keys read from `S3_AccessKey`/`S3_SecretKey`.
- GitHub secrets `GH_PAT`, `S3_AccessKey`, `S3_SecretKey` present (the repo stores the S3 ones as
  `S3_ACCESSKEY`/`S3_SECRETKEY`; matched case-insensitively, #30).
- Deploy copy vs approved bundle: 12 fields, all allowed — 3 `folder` arguments (+`-cs`), 5
  placeholders filled (`ComputeServers.GH.{UserName,ActionRepoName}`,
  `DataStores.S3.{Bucket,Endpoint,Region}`), 3 `FunctionGitRepo` paths (per-deployment folder),
  `WorkflowName`; plus 4 derived locations (S3 log prefix, repo paths).
- Note: no bundle hash recorded at approval (run A predates deploy).

**Bundle hash recorded before D1** (in the copy's `state.json` only; the original run is not
touched). The Gate 4 snapshot `history/005-gate4-approve.json` holds no hash, so the bundle was
checked against what it does hold: `spec.json` equals the snapshot's spec; each function file
contains the approved code; every content file is dated 22:13:47, before the approval
(2026-10-20T22:14:11); the copy equals the original run's bundle. `bundle_hash` =
`57aae69c…4273`, with `bundle_hash_recorded` saying how. D1 refuses to deploy if it changes.
- Found while checking: a Finder `.DS_Store` (dated 00:10:37, after the approval) sits in the
  bundle folder, in the original too. `tree_hash` counted it, so browsing a bundle in Finder
  would have made a deploy refuse; it now ignores OS metadata (`.DS_Store`, `._*`, `Thumbs.db`,
  `desktop.ini`, `__pycache__/`) — part of D1's changes, with a test.

## 32. D1 build: curate-deploy (fakes only; nothing reached GitHub or S3)

**What was built.**
- **Gate 4** offers **Approve & deploy** / **Approve, no deploy** / **Stop here**
  (`spec.py approve --gate 4 --deploy yes|no`; default no = straight to finalize, as before).
  Approval records `bundle_hash` (sha256 of the bundle folder, OS metadata ignored) and
  `deploy_choice`; both are in the Gate 4 snapshot.
- **New stage `deploy`** (skill `curate-deploy`, stage file `references/stages/deploy.md`)
  between emit and finalize. Its requirements: Gate 4 approved (not superseded) with
  deploy = yes, a validated bundle for the current spec, a recorded bundle hash. Redoing emit or
  an earlier stage clears `bundle_hash`, `deploy_choice` and `deploy` (the deploy log stays).
- **`--start-at deploy --from-run <id>`**: a new run with that run's Gate 4-approved bundle. The
  hash comes from the snapshot, or for runs approved before hashes existed from the source's
  `state.json` (same bundle folder only, with its `bundle_hash_recorded` note); a bundle whose
  hash differs is refused. Only from a run (not a bundle or file).
- **`deploy.py publish|upload|register|invoke --run R [--confirmed] [--overwrite-own-deploy]`**
  and `deploy.py status`. In order; a finished step is skipped. Without `--confirmed` a step
  writes nothing and returns its plan (what, repo, branch, every path). Each step re-runs the
  preflight (bundle hash required; earlier steps' paths must still be this deploy's), checks all
  content for secrets before the first push, appends every written path to
  `<run>/deploy/deploy-log.jsonl`, and records itself in `state.json` under `state.lock`. Publish
  and upload use the contents API (ported from CURATE's PyGithub calls; messages tagged
  `(curate-skills)`); register and invoke call CURATE's vendored `main`s, with their log lines
  captured and redacted, keeping only the InvocationID/timestamp from the payload (which holds
  the token). A failed step keeps the run at `deploy` with a resume command; retrying it needs
  `--overwrite-own-deploy` (the user's choice). After invoke the run is **stopped** (stopped_from
  finalize) with "Check Actions and S3; resume with /curate-skills:curate --resume <id> to
  finalize." — saving to the catalog comes only after the user has checked the deployed run (until
  D2's monitor exists). The preflight also refuses when `GITHUB_REPOSITORY`/`GITHUB_REF_NAME` are set
  (CURATE's code would deploy there instead).
- **Guard** (`hooks/mga_guard.py`): curate-mga may never run `deploy.py`/`monitor.py`; the main
  session is denied Bash that would print a credential (`gh auth token`, `printenv`, bare
  `env`/`set`/`export`, `echo $<credential>` in any case, reading a `.env` file) — a heuristic.
- **Docs**: gates.md (Gate 4 options, deploy steps are the user's), emit.md, /curate SKILL.md
  (description unchanged, 1001 characters), state-contract.md, stage-io.md, VENDORED.md.

**Tests: 320 passed** (without `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` set);
`claude plugin validate --strict` passes. New: the four steps in order against a fake GitHub that
accepts writes (paths, commit tags, the uploaded deploy copy, deploy log, state, invocation id,
no secret in any output, state, log or pushed content); unconfirmed steps write nothing; order and
skipping; refusals before any write (changed bundle, no hash, wrong stage, no deploy choice,
`GITHUB_REPOSITORY` set); a secret in one function file stops publish before the first push; a
failed write leaves a resumable run whose retry is refused until `--overwrite-own-deploy`; spec
side (Gate 4 choice, start at deploy and its refusals, rewind clears); guard (16 denied, 11
allowed commands); the deploy stage file's rules.

**Checked without side effects:** CURATE's vendored register/invoke modules import under
`deploy.py`'s pinned deps (`FaaSr_py==0.1.13`, `PyGithub`, `requests`); the generated Actions file
references only `GH_PAT`, `S3_AccessKey`, `S3_SecretKey`. A dry run of
`spec.py start --at deploy --from-run 20261020-215732-corvallis-winter-weather` in the copy
project (`~/curate-tests/weather-deploy`) previews the new run and creates nothing; for that, the
copy's Gate 4 snapshot `bundle_dir` was repointed to the copied bundle (as its `state.json`
already was). The original run is untouched.

Diff: 15 files, +1010 / −62 (deploy.py +374, spec.py +108, guard +72, tests +367, docs).

**Review fixes before committing D1** (user's three points):
- The run-A copy's Gate 4 snapshot edit is recorded in the copy's own history (a `copy` /
  `repointed` entry: what was copied, both `bundle_dir` values, the snapshot's sha256 after).
  Root cause fixed: `--from-run` now takes the bundle folder from the source's `state.json` when
  the snapshot's path differs and a folder of the same name exists there (a copied or moved run);
  its content is still checked by hash, and the source is never changed. Tests: copied run with
  the hash in the snapshot or only in state.json; a changed copy is refused; a differently named
  folder is not taken.
- Publish only pushes `functions/<fn>.py` for the functions in `FunctionGitRepo`, never a folder
  listing, so OS metadata is never pushed; a test puts `.DS_Store`, `._*.py`, `__pycache__/` and
  `Thumbs.db` in the bundle after approval: the hash still matches and only the function files
  and the workflow JSON are pushed.
- After invoke the run stops (above); `spec.py next` then names no skill, so /curate stops too.
Tests: 325 passed (without `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`); plugin validates.
- Follow-up: a moved bundle is accepted only when it matches a recorded hash (snapshot's or
  state.json's); with neither, `--from-run` refuses (for deploy and finalize starts) and says how to
  go on — approve Gate 4 again, put the bundle back, or record a checked hash. Never by folder name
  alone. The guard now treats `.env*` (`.env.local`, `.envrc`, …) as well as `*.env` as credential
  files. Tests: 333 passed.

## 33. Fix: --start-at deploy --from-run failed the preflight (user's run 20261101-172236)

**Bug.** The user's real attempt (`/curate-skills:curate --start-at deploy --from-run
20261020-215732-corvallis-winter-weather`, run `20261101-172236-corvallis-winter-weather-comparison`)
stopped at the preflight: "Gate 4 was never approved in this run". `start` records the source's
approval as a `copied` gate-4 entry; `deploy.py`'s check only knew `approve`. The D1 tests set up
deploy runs by hand and never went through `start`. Nothing was written to GitHub or S3. That
run is marked abandoned in its history (it also lacks the hash the fix records).

**Also found:** `start --at deploy` set `deploy_choice: yes` when it created the run — before
/curate asks "Start this run?". The choice was recorded ahead of the user's answer.

**Fix.**
- `start --at deploy|finalize --from-run` records the carried-over approval with `from_run`,
  `snapshot` (+ sha256) and `bundle_hash`. `deploy.py` accepts a `copied` Gate 4 entry only with
  all three, and only while the bundle's hash matches it.
- Deploy choice: the user's Yes to "Start this run?" counts as the deploy choice, recorded as such.
  `start --at deploy` leaves `deploy_choice` empty (history `deploy: pending`, plus the source's
  own `source_deploy_choice`); /curate calls the new `spec.py confirm-start --run <new run>` after
  Yes, which sets it and records `deploy-confirmed`. Until then the deploy stage is refused
  ("the user has not confirmed starting this run"). For other start points confirm-start records
  nothing.
- Chosen over refusing and pointing to `--start-at emit` because it deploys exactly the bundle that
  was reviewed and approved at Gate 4 (hash-checked), where `--start-at emit` would re-emit a new
  bundle folder (new hash) and repeat Gate 4 only to add a choice the user makes explicitly here
  anyway; each of the four steps still asks.
- End-to-end test: a run taken through Gates 1-4 by the scripts → `start --at deploy --from-run`
  → `confirm-start` → `begin deploy` → `deploy.py preflight` (ok) → `publish` without and with
  `--confirmed` against the fake GitHub; then a bundle edit makes the next step refuse
  ("no longer matches the Gate 4 approval carried over"). Tests: 335 passed.

## 34. D2 build: curate-monitor (fakes only; nothing reached GitHub or S3)

Plan approved with three changes (fresh outputs; job logs in memory only; time-only matches give
"success, unconfirmed match" that the user accepts at Gate 5) plus: each run's job log is fetched
once and the match cached.

**What was built.**
- New stage `monitor` (skill `curate-monitor`, stage file `references/stages/monitor.md`) between
  deploy and finalize; after invoke the run goes there (D1's temporary stop is gone).
- `monitor.py poll --run R [--budget S] [--report-only] [--minutes M] [--interval S]`, `status`,
  `accept-unconfirmed --confirmed`. Read-only: Actions runs via GET (`/actions/workflows/<deploy
  name>-<action>.yml/runs?event=workflow_dispatch&created>=dispatched_at−2min`, run jobs, job
  logs); S3 via `head_object`/`get_object`. The job-log redirect is followed by the monitor itself
  **without** the Authorization header (checked against a local server: the opener stops at the
  302 and the token is sent only to the first host).
- Matching: a finished run counts as this invocation's when its job log contains the InvocationID
  (searched in memory; job-log text is never saved or printed; a failure excerpt from it is
  redacted). Each run's classification (`confirmed` / `time_only` / `other`) is cached in
  `state.monitor.matches`, so its log is fetched once. A finished run without a log is matched by
  time only.
- Per action rank (`download`, `process(1..3)`, `plot`): FaaSr log and `.done` marker in
  `FaaSrLog/<name>/<timestamp>/<InvocationID>/`, `[ERROR]` = failed.
- Outputs = each action's `output<N>` under its `folder` (CURATE's `build_artifact_list`),
  `{rank}` expanded; for the weather workflow the 5 files `USC00351862_daily.csv`,
  `winter_comparison_{1,2,3}.csv`, `corvallis_winter_comparison.png` under
  `corvallis_winter_weather-cs/`. A file counts only if `LastModified >= dispatched_at`; older is
  "stale" and fails the outputs check. Checked once every Actions run succeeded.
- Results, each reported separately (Actions / outputs / FaaSr logs) with the note that output
  correctness (the plot) is not judged: **success** → finalize; **success, unconfirmed match** →
  finalize, where Gate 5 asks "Accept the unconfirmed match" first (`accept-unconfirmed
  --confirmed` after the user's answer; `catalog.py save-*` refuses a deployed run until success or
  an accepted unconfirmed match); **failure** → stopped (`stopped_from: monitor`) with the failed
  action, why, its Actions URL and a redacted excerpt; **timeout** (`monitor_minutes`, default 10,
  pinned per run; each call ≤ 540 s) → stopped, resuming continues the same invocation with a
  fresh budget and never redeploys. Resume after success is a no-op.
- Report-only: a run not at stage `monitor` (e.g. `done`) is never recorded — no state, stage or
  file change; `--report-only` forces it.
- spec.py: `monitor` stage and requirements (finalize after a deploy needs monitor success);
  `start --at monitor` and `import --stage monitor` refused; the run summary (Gate 5) includes the
  monitor summary. Plugin userConfig `monitor_minutes`. The guard already denies curate-mga
  `monitor.py` (D1).
- Reuse (VENDORED.md): `build_artifact_list` as-is; log/done key formats, `[ERROR]` regex,
  invocation folder and rank parsing ported (the vendored classes import boto3/FaaSr_py at load);
  S3 client built as the vendored one; `WorkflowRunner` (threads, inactivity timer) not used —
  each poll is a bounded snapshot.

**Tests: 352 passed** (without `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`); plugin validates. New
(`tests/test_monitor.py`, fakes for GitHub with Actions endpoints and a redirecting blob host, and
S3): success; a failed action (redacted excerpt, fake token never in output, state or saved
logs); a missing and a stale output (outputs fail, Actions pass); all outputs stale from an
earlier invocation; timeout then resume (same invocation, fresh budget, GET only); resume after
success (no calls); job logs fetched once and another invocation's run ignored; a job log holding
the token and payload is never printed or saved; time-only match → success_unconfirmed → catalog
refuses → accept needs `--confirmed` → accepted; catalog refuses a deployed run before success;
report-only on a `done` run leaves every file byte-identical; finalize needs monitor success.

**Checked without side effects:** `monitor.py` loads under its pinned deps; on run
`20261101-172823` without a token it refuses (exit 2), and its `state.json` (sha256 `b14b985e…`)
and folder tree hash (`671fd39d…`) are unchanged.

## 35. First real monitor check: run 20261101-172823 (report-only)

User ran `monitor.py poll --run …/20261101-172823-corvallis-winter-weather-comparison --report-only`
with their `.env`, at `6f9798c`. Read-only; the run was at `done`.

- **Result: success** in 0.2 min (one poll). Invocation `2026-11-01-17-33-10`, dispatched
  2026-11-02T01:33:09Z, deployment `corvallis-winter-weather-comparison-cs` in
  `laurakuo1006/CURATE-test`.
- **Actions:** 5/5 runs, all `success`, **all matched by InvocationID** — the job logs do contain
  it, so the time-only fallback was not needed: download 1/1, process-weather-variable 3/3, plot
  1/1.
- **FaaSr logs:** every rank `completed`.
- **Outputs:** 5/5 present and fresh under `s3://test-curate/corvallis_winter_weather-cs/`:
  `USC00351862_daily.csv` (13,127,314 bytes), `winter_comparison_1.csv` (2,239),
  `winter_comparison_2.csv` (2,732), `winter_comparison_3.csv` (2,640),
  `corvallis_winter_comparison.png` (297,097). The plot itself was not judged.
- **Unchanged:** the run's `state.json` (sha256 `b14b985e…`) and folder tree hash (`671fd39d…`)
  are identical before and after; no `deploy/monitor/` folder was created.
