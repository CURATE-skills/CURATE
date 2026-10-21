# curate-skills

A Claude Code plugin that reimplements **CURATE** — *Composition, User-in-the-loop, Reuse, and
Automated Task Execution* (Cutler, Kuo, Velugoti, Newhart, Figueiredo; arXiv 2608.04270) — as
skills, one subagent and a few small scripts, instead of the prototype's LangGraph code.

You describe a scientific workflow in plain language. Claude Code turns it into a FaaSr
serverless workflow in six stages, stopping at human approval gates. Run them all with
`/curate`, or run any stage on its own with its stage skill (v2):

| Stage skill | Does | Who | Ends at |
|---|---|---|---|
| `curate-skeleton` | the steps and their order | Claude (as CURATE's WCA) | Gate **1** |
| `curate-reuse` | catalog search; reuse / adapt / cached / new per function | Claude (as the MCA) | shows decisions |
| `curate-resolve` | concrete files, data flow, packages | Claude (as the WCA) | Gate **2** |
| `curate-generate` | one function at a time, tested locally against FaaSr stubs | a `curate-mga` subagent per function | Gate **3** |
| `curate-emit` | FaaSr `workflow.json` + functions, validated with FaaSr_py | scripts | Gate **4** |
| `wda-finalize` | summary, then save verified functions to the catalog | Claude | Gate **5** |

**It does not deploy.** It stops at a validated bundle in `faasr_output/<workflow>-<run_id>/`; deploying
to GitHub Actions and monitoring the run are not implemented.

**Versions.** v1 (git tag `v1`) has one `/curate` skill. v2 (branch `v2-files`) splits it into
the stage skills above; the stage procedures live once in `references/stages/` and both
`/curate` and the stage skills follow them. v2 cannot continue a run started with v1 (use the
v1 plugin for those).

## Requirements

- Claude Code **2.1.295 or later** (tested on 2.1.299).
- [`uv`](https://docs.astral.sh/uv/) on `PATH`. The scripts declare their dependencies inline
  and `uv` fetches Python 3.13 and pinned packages (pydantic 2.11.7, rank-bm25 0.2.2,
  FaaSr_py 0.1.13) on first use.
- `python3` (3.9+) on `PATH` for the write guard hook. macOS ships one.

## Launch

Generation spawns one `curate-mga` subagent per function and must wait for each. In
interactive sessions Claude Code runs subagents in the background, and the session does not
resume by itself when one finishes, so `/curate` would stall after every function. Start
Claude Code with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, for example with a shell alias:

```bash
alias curate='CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 claude --plugin-dir /absolute/path/to/curate-skills'
```

Use an absolute path so the alias works from any folder. The generate stage checks for the
variable before spawning anything and stops with instructions if it is missing (not needed
when every function is reused).

That variable also turns off, for the whole session: background Bash commands
(`run_in_background`), automatic backgrounding of long commands, and Ctrl+B. Use a separate
session for work that needs those.

Installing from the local marketplace in this repository (`/plugin marketplace add <repo root>`,
then `/plugin install curate-skills@curate-local`) should also work but is not yet tested; see
Open items.

## Use

```
/curate-skills:curate <workflow sketch> [--catalog DIR] [--attach model.py] [--context FILE]
                      [--mga-retries N] [--stubtest-mode strict|parity]
/curate-skills:curate --resume <run_id>
/curate-skills:curate --start-at <stage> --from-run <run_id> | --from-bundle <folder>
                      | --from-file <file> | --sketch "<text>" | --sketch-file <file>   [...]
/curate-skills:curate --from <file> --stage <stage> [...]

/curate-skills:curate-skeleton --from <sketch> | <sketch text> | --run <run_id>
/curate-skills:curate-reuse    --from <skeleton.json> | --run <run_id>
/curate-skills:curate-resolve  --from <skeleton.json> --candidates <candidates.json> | --run <run_id>
/curate-skills:curate-generate --from <spec.json> [--functions DIR] [--existing-code keep|rewrite|ignore] | --run <run_id>
/curate-skills:curate-emit     --from <spec.json> [--functions DIR] | --run <run_id>
/curate-skills:wda-finalize    [--run <run_id>]

/curate-skills:curate-catalog where | list | search <q> | init | import <path> | export <dir>
```

At each gate, approve, stop, or type the changes you want. Changes at Gates 2–4 are classified
as structural (back to the skeleton) or functional (back to reuse; working functions come
back as `[cached]`). Each change request counts as one revision round; at the cap (5) nothing
is changed until you approve as-is, stop, or raise the cap.

### Start points

`/curate-skills:curate --start-at <stage>` starts a **new run** from exactly one source. The
source is never changed.

| Source | What it gives the new run | Can start at |
|---|---|---|
| `--from-run <run_id>` | that run's **gate-approved** results: the latest approval of the gate before the stage (Gate 1 for reuse; Gate 2 for resolve and generate; Gate 3 for emit; Gate 4 for finalize), read from the run's approval snapshots in `history/` — never its current, possibly unapproved state, and never its old code | reuse, resolve, generate, emit, finalize |
| `--from-bundle <folder>` | a `faasr_output/…` bundle's `spec.json` with its code | generate (with `--existing-code`), emit |
| `--from-file <file>` | the import formats in `references/stage-io.md` | any but finalize |
| `--sketch "<text>"` / `--sketch-file <file>` | a new sketch | skeleton |

Before any stage work, /curate shows what will be used — source and snapshot or file, the
workflow, which functions come with code and their stub-test results, what is copied and what
is redone, and the settings — and asks **Start this run? Yes / Stop**. Stop leaves the new run in
place, marked stopped, and deletes nothing. It refuses rather than guess: a gate that was never
approved, an approval undone by a later change request or rewind, a partial or unknown run id,
a v1 run, a bundle without code, or a source that does not fit the stage. The new run's
history records where it came from (run, snapshot file and its sha256). Your sketch, attached
files and standing change requests come along; settings come from your flags or defaults.

### Redoing a stage

To redo a stage of a run — e.g. regenerate its code — you have two options:

- **New run (recommended):** `/curate-skills:curate --start-at generate --from-run <run_id>`.
  It copies the approved results up to Gate 2 into a new run and generates the code again. The
  original run stays as it is, and no revision round is used.
- **Same run:** `/curate-skills:curate-generate --run <run_id>`. If the run is already past
  generate, it goes back (a *rewind*): that costs one revision round, and generate and every
  stage after it are redone. It asks you first and refuses without your yes. Functions marked
  new start from an empty folder; your old code stays available only for `[cached]` decisions.
  To redo just some functions, use a change request at Gate 3 instead.

For another stage, replace `generate` with its name (skeleton, reuse, resolve, generate, emit,
finalize). If you ask in plain words ("re-generate the code for run X"), Claude is told not to
run CURATE's scripts or rewind on its own, but to give you these two commands — the new run
first, then the rewind and its cost — and wait.

**Running stages separately.** A stage skill stops after its gate and prints the next one to
run, e.g. `Next: /curate-skills:curate-reuse --run <run_id>`; `/curate` instead carries on by
itself. Every stage first checks what it needs and, if something is missing or out of date,
says which earlier stage produces it — including "approve Gate N first" when the run is still
waiting at an earlier gate. Going back to an earlier stage is a rewind (see "Redoing a stage").
A stopped run tells you how to continue: `/curate-skills:curate --resume <run_id>`, or the stage
command for where it stopped.

**Starting from your own files** (`--from`): an imported file counts as approved; add
`--review` to see the gate before the stage first. Accepted formats per stage, and why a FaaSr
`workflow.json` is not enough to start from, are in `references/stage-io.md`. Code you bring
counts as verified only after it has been stub-tested at generate. When you start generate
with code — a `--functions` folder, or a spec that already contains code such as a bundle's
`spec.json` — say what to do with it with `--existing-code`. If you don't, the skill asks you
(Keep / Rewrite / Ignore); in a headless run it stops and lists the options. It never guesses:

| `--existing-code` | What happens | Gate 3 shows |
|---|---|---|
| `keep` | your code is kept as is and only stub-tested (no subagents) | `kept: your code` |
| `rewrite` | each function is regenerated, starting from your code | `rewritten from your code` |
| `ignore` | your code is dropped; functions are generated from scratch | `[new]` |

- `--attach model.py`: your own Python script or function becomes a workflow step, wrapped
  verbatim (or adapted, if you ask for changes to it) and checked for output parity.
- `--context FILE`: reference material (paper, sample data). Files named like a step's input
  become that step's real test data.

## Settings

| Setting | Flag | Env var | Plugin setting | Default |
|---|---|---|---|---|
| Catalog folder | `--catalog` | `CURATE_CATALOG_DIR` | `catalog_dir` | `~/.curate/catalog` |
| Extra MGA attempts after a failed stub test | `--mga-retries` | `CURATE_MGA_RETRIES` | `mga_retries` | `3` (`0` = original CURATE) |
| Stub-test mode | `--stubtest-mode` | `CURATE_STUBTEST_MODE` | `stubtest_mode` | `strict` |

The first one set wins, left to right. A run pins all three when it starts.

- `strict`: every rank of a parallel step, the function's dependencies installed in an isolated
  environment, no `faasr` imports, every declared output must be uploaded.
- `parity`: the original CURATE behaviour — rank 1 only, no dependency install, and a
  `ModuleNotFoundError` counts as a pass when dependencies are declared.

Plugin settings in skill text are not filled in when the plugin is loaded with `--plugin-dir`;
use the flag or the environment variable there.

## Open items

- **Rewind confirmation is instruction-level.** `spec.py begin --rewind` refuses without
  `--confirmed`, and the skills say to ask you first, but nothing technically stops a model from
  passing the flag. Plain requests in fresh sessions have been handled correctly since the
  description change (results log #20, #22); a session that already has /curate loaded has not
  been tested. If that shows a problem, the planned fix is a typed confirmation phrase checked by
  a `UserPromptSubmit` hook (results log #24).

- **Marketplace install not tested.** Untested: installing from the local marketplace
  (`/plugin install curate-skills@curate-local`), and whether plugin settings (`catalog_dir`,
  `mga_retries`, `stubtest_mode`) are then filled in. Until it is checked, use the flags or
  environment variables. If you test it, uninstall afterwards or drop `--plugin-dir` from the
  `curate` alias, so the plugin does not load twice under the same name.

## Files it creates

- `.curate/runs/<run_id>/` — run state (`state.json`), one folder per function with its spec,
  code, test data and stub-test log, and a history of gate decisions. Add `.curate/` to your
  `.gitignore`.
- `.curate/mga.lock.json` while a `curate-mga` subagent runs; `.curate/mga_guard.log` for denied
  writes (and every subagent call when `CURATE_GUARD_DEBUG=1`).
- `faasr_output/<workflow>-<run_id>/` — the bundle, one folder per run (a numbered `-2`… if a run
  emits again; an existing bundle is never overwritten), path recorded in `state.json`: `workflow.json`, `functions/`, `spec.json`,
  `external_inputs.json`, `README.md`. Unset `FAASR_*` deployment variables appear as
  `<FAASR_NAME>` placeholders; only secret **names** are ever written, and the bundle is not
  written if a known secret value would appear in it.
- The catalog folder: `functions/<uuid>.json` and `workflows/<uuid>.json`, in the original
  prototype's format (its catalogs can be imported).

## Safety

- Each `curate-mga` subagent may write only inside its own function folder. A PreToolUse hook
  (`hooks/mga_guard.py`) denies Write/Edit outside it and write-like Bash commands
  (redirections, `cp`, `mv`, `rm`, `sed -i`, `git`, `pip install`, …) aimed elsewhere. The Bash
  check is a heuristic; after every subagent `/curate` also compares file snapshots of the run,
  the catalog and the project, and fails the attempt on any write outside the folder.
- The hook runs on every Write/Edit/Bash call in sessions where the plugin is enabled (about
  30 ms each; other agents pass straight through). It fails closed: if it cannot run, the call
  is blocked.
- Stub tests run with deployment and model-provider credentials removed from the environment.
- For a hard boundary, also enable Claude Code's sandbox with filesystem write restrictions.

## Reproducing the paper's experiments

Prompts are in `../experiments/prompts/` (E2 is reconstructed from SeBS-Flow §5; E4 is verbatim).
The paper used Claude Opus 4.8: launch with `--model claude-opus-4-8`; `curate-mga` inherits the
session's model. E4 starts from the E2 catalog in `tests/fixtures/catalog-e2-mapreduce/`.
Results are logged in `../experiments/results-log.md`.

## Tests

```bash
uv run --no-project --python 3.13 --with pytest --with pydantic==2.11.7 --with rank-bm25==0.2.2 \
  pytest tests -q
```

## How this differs from the prototype

See `scripts/VENDORED.md`: which code is copied, adapted or new (with source lines and commit
`3fd5c16`), and every deliberate behaviour change.

## License

MIT (`LICENSE`). Parts are derived from the CURATE prototype, MIT, Copyright (c) 2026 Nolan
Cutler — see `NOTICE`.
