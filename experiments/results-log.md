# curate-skills results log

One entry per test run. Record cost and turns per stage, and every write-guard denial with
whether it was legitimate (a real attempt to write outside the function's folder) or a false
positive (the target was inside the folder but the guard could not prove it).

- **Headless runs** (`claude -p --output-format json`): cost (`total_cost_usd`) and turns
  (`num_turns`) come from the CLI's JSON result.
- **Interactive runs**: no cost is printed, so record token usage with
  `python3 experiments/tools/transcript_usage.py <session-id>`, which sums the session
  transcript and its subagent transcripts. Run `/cost` before quitting if you want a dollar figure.
- **Denials**: from `.curate/mga_guard.log` (entries without `"debug"`).

Model for every run so far: `claude-opus-5-5`. Claude Code 2.1.299.

## Summary

| # | Date | Run | Mode | Stages | Cost (USD) | Turns | Guard denials (legit / false +) |
|---|---|---|---|---|---|---|---|
| 1 | 2026-10-15 | M3 guard smoke test | headless | — | not captured | not captured | 2 (2 / 0) |
| 2 | 2026-10-15 | E2 MapReduce, through Gate 2 | headless, 2 turns | skeleton + Gate 1; resolve + Gate 2 | 0.309 + 0.664 = 0.973 | 10 + 9 = 19 | 0 |
| 3 | 2026-10-15 | M5 test 1: sequential spawning | interactive | generate (3 functions) | not captured (tokens below) | 17 main + 19 subagent messages | 3 (0 / 3) |

## 1. M3 guard smoke test

- Throwaway project; one locked function `hello`; one `curate-mga` subagent asked to probe the guard.
- Output was plain text, so cost and turns were not captured. Later runs use `--output-format json`.
- Denials:
  1. Write to `<project>/probe_outside.txt` — **legitimate** (intentional probe).
  2. Bash `echo bad > <project>/probe_bash.txt` — **legitimate** (intentional probe).
- Confirmed: `agent_type` is `curate-skills:curate-mga`; `skills: [faasr-reference]` preloads with the bare name.

## 2. E2 MapReduce word count, through Gate 2

- Sketch: paraphrase of the SeBS-Flow MapReduce workload plus the paper's two bullets verbatim
  (the SeBS-Flow §5 prompt itself is not in the paper or in `ref/curate`).
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
