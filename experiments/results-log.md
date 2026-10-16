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
