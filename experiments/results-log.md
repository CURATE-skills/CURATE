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
