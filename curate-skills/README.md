# curate-skills

A Claude Code plugin that turns a plain-language description of a scientific workflow into a
[FaaSr](https://faasr.io) serverless workflow.

**What it is.** A pipeline of stages — skeleton → reuse → resolve → generate → emit → (deploy →
monitor) → finalize — that stops at **human approval gates** after each stage. Functions are
generated one at a time by a subagent and tested locally against FaaSr stand-ins
before you see them. Verified functions go into a catalog that later runs reuse.

### Skills

| Skill | What it does |
|---|---|
| `/curate-skills:curate` | Runs the full pipeline, stopping at each gate |
| `/curate-skills:curate-skeleton` | Turns the sketch into an outline: steps, order, parallel copies (Gate 1) |
| `/curate-skills:curate-reuse` | Searches the catalog; decides reuse, adapt or new for each step |
| `/curate-skills:curate-resolve` | Adds the details: input/output files, data flow, packages (Gate 2) |
| `/curate-skills:curate-generate` | Writes and tests each function (Gate 3) |
| `/curate-skills:curate-emit` | Builds and validates the FaaSr workflow file and bundle (Gate 4) |
| `/curate-skills:curate-deploy` | Deploys the bundle to your repo and bucket, asking before each step (optional) |
| `/curate-skills:curate-monitor` | Watches a deployed run until it succeeds, fails or times out |
| `/curate-skills:wda-finalize` | Summarizes the run and saves verified functions to the catalog (Gate 5) |
| `/curate-skills:curate-catalog` | Inspects and manages the catalog: where, list, search, import, export |
| `/curate-skills:curate-doctor` | Checks your setup (and deploy settings with `--deploy`) |

Internal, not run directly: the `curate-mga` subagent writes each function, with the
`faasr-reference` skill preloaded.

## Requirements

- macOS or Linux (tested on macOS).
- [Claude Code](https://code.claude.com) (tested on 2.1.318).
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (tested 0.13.1). 
- `python3` 3.9 or newer
- `git`, to clone this repository.
- Only for deploying: a GitHub account, an S3 bucket — see [Deploy](#deploy).

## Install

```bash
git clone https://github.com/laurakuo1006/skill-curate.git
```

The plugin is the `curate-skills/` folder inside the clone. Add an alias to `~/.zshrc` (or
`~/.bashrc`), with the **absolute** path to that folder, then open a new terminal:

```bash
alias curate='CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 claude --plugin-dir /absolute/path/to/skill-curate/curate-skills'
```

`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` is required: generation spawns one subagent per function
and must wait for each; with background tasks on, the session stalls after every function. The
variable also turns off background Bash commands and Ctrl+B for that session.

Start Claude Code with `curate` in the folder where you want the run's files, then check the
setup:

```
/curate-skills:curate-doctor
```

Every line should say `ok` (a `warn` about `.curate/` in `.gitignore` is advice). Each `FAIL`
line says what to fix. On the first run `uv` downloads Python 3.13 and the packages.

## Quick start

A **sketch** is a plain-language description of the workflow you want. Start `curate` in an empty
folder, then:

### Run your own workflow

This command runs the entire pipeline (sketch → outline → reuse → design → code + tests → bundle →
[deploy → monitor, if you choose it at Gate 4] → catalog), stopping at each gate for your review
(see [Gates](#gates)).

```text
/curate-skills:curate <your sketch>
```

The bundle ends up in `faasr_output/<workflow>-<run_id>/` in the folder you started in.

### Not sure what to try? Use the weather example

A FaaSr weather visualization workflow on real NOAA data (Corvallis, Oregon: this winter against
the past 10 years):

```text
/curate-skills:curate Build me a FaaSr workflow that compares this winter's weather in Corvallis, Oregon with the past 10 years. Download the NOAA GHCN-Daily CSV for station USC00351862. Then, in parallel, process precipitation (PRCP), minimum temperature (TMIN) and maximum temperature (TMAX): for each, take the values from 2025-01-01 to 2025-03-01, and the daily average over the same dates in each of the previous 10 years (extend that window by 30 days). Finally, plot a figure with three panels (precipitation, max temperature, min temperature) showing this year as a solid line and the 10-year average as a dashed line. Temperatures in the data are in tenths of a degree Celsius.
```

### Reproduce E2 from the CURATE paper

The word-count sketch is in [`experiments/prompts/e2-mapreduce.md`](../experiments/prompts/e2-mapreduce.md);
run it the same way. The sketch is reconstructed, not the paper's verbatim prompt.

## Gates

Each gate shows a table and waits. Type **approve**, **stop**, or the change you want in your own
words.

| Gate | After | You review |
|---|---|---|
| 1 | skeleton | the steps and their order |
| 2 | reuse + resolve | per function: new, from the catalog, adapted, or cached; files and data flow; packages |
| 3 | generate | the code and each function's stub-test result (ask to see any function) |
| 4 | emit | the validated bundle; **approve & deploy**, **approve, no deploy**, or **stop** (see below) |
| 5 | finalize | the summary; which verified functions to save to the catalog |

A change at Gates 2–4 sends the run back to the right stage (structural changes to the skeleton,
functional ones to reuse; working functions come back as `[cached]`). Each change request is one
revision round; after 5, only approve or stop.

**After Gate 4**, `/curate-skills:curate` carries on in the same session:
- **approve & deploy** → deploy → after invoke, monitor watches the run → on success, finalize (Gate 5).
  It stops early only if you choose **Stop here** at a deploy step, or monitor ends with
  `failure` or `timeout`; each time it prints the command to continue.
- **approve, no deploy** → straight to finalize (Gate 5).

A stage skill started on its own (`/curate-skills:curate-deploy`, `curate-monitor`, …) stops
after its stage and prints the next command instead.

## Start at a stage, resume, rewind

| To… | Command |
|---|---|
| Resume a stopped run | `/curate-skills:curate --resume <run_id>` |
| Redo a run from a stage, without changing the original | `/curate-skills:curate --start-at <stage> --from-run <run_id>` |
| Change one function of a finished run | `/curate-skills:curate --start-at generate --from-run <run_id> --existing-code keep`, then ask for the change at Gate 3. Only that function is rewritten. |
| Rewrite all functions of a finished run | `/curate-skills:curate --start-at generate --from-run <run_id> --existing-code ignore` |
| Start from other sources | `--from-bundle <folder>` (generate, emit) · `--from-file <file>` · `--sketch "<text>"` / `--sketch-file <file>` (skeleton) |
| Rewind the same run | `/curate-skills:curate-<stage> --run <run_id>` |
| Run one stage only | `/curate-skills:curate-<stage> --run <run_id>`. Stages: `skeleton`, `reuse`, `resolve`, `generate`, `emit`, `deploy`, `monitor`; finalize is `wda-finalize` |

- **New runs** copy only gate-approved results, never change the source, and ask "Start this run?" first.
- **Rewinding** asks first. It costs one revision round and redoes that stage and every later one.
- **Each stage** stops after its gate and prints the next command.
- Input formats: `references/stage-io.md`.

## Catalog

`/curate-skills:curate-catalog where | list | search <q> | init | import <path> | export <dir>`

## Deploy (optional)

Deploys the bundle to **your own** GitHub repo and S3 bucket, then runs it.

### 1. Prerequisites
- **Action repo** with a first commit (https://github.com/new, tick "Add a README").
- **Classic GitHub token** with `repo` + `workflow`: Settings → Developer settings →
  Personal access tokens → Tokens (classic). Fine-grained tokens aren't recommended.
- **S3 bucket** (AWS or S3-compatible) and a key pair that can read and write it.
- **Actions secrets** on the repo: `GH_PAT`, `S3_AccessKey`, `S3_SecretKey`
  (repo → Settings → Secrets and variables → Actions).

### 2. Settings file
From the clone's `curate-skills/` folder:
```bash
mkdir -p ~/.config/curate && cp .env.template ~/.config/curate/.env && chmod 600 ~/.config/curate/.env
```
Fill it in with an editor; each line's comment says what goes there. Deploy, monitor
and doctor read this file themselves; environment variables override it.

> **Never paste a secret into Claude Code.** If you did, revoke it and make a new one.

### 3. Check

```
/curate-skills:curate-doctor --deploy --online
```

Read-only check of the file, token, repo, branch and bucket. Values are never shown.

**Auto mode** blocks deploy after the first step. Use default mode (Shift+Tab) for deploy, or add
these to the `allow` list in `~/.claude/settings.json` (`doctor --deploy` prints them with your
path when they're missing):
```
Bash(uv run --quiet /absolute/path/to/skill-curate/curate-skills/scripts/deploy.py *)
Bash(uv run --quiet /absolute/path/to/skill-curate/curate-skills/scripts/monitor.py *)
```

### 4. Deploy
At Gate 4, choose **approve & deploy** (or run `/curate-skills:curate-deploy --run <run_id>`).
After a read-only preflight, deploy asks before each of four steps:
1. **Publish:** function files to the repo
2. **Upload:** `workflow.json` to S3
3. **Register:** GitHub Actions files
4. **Invoke:** start the workflow

Each deployment is named `<Workflow>-<MMDD-HHMM>` after the run that approved Gate 4, so two runs
of the same sketch never clash; deploy refuses to overwrite any existing deployment.
`/curate-skills:curate-deploy --list` shows this project's deployments; `--teardown --run <run_id>`
deletes exactly what that run deployed (repo files and S3 objects), after asking once.

### 5. Monitor
Under `/curate`, monitoring starts after invoke and watches the run for up to 10 minutes,
with one line per function as it changes (`… running`, `✓ succeeded`, `✗ failed: <reason>`).
On success, finalize follows. On failure or timeout, the run stops; to re-check the same
invocation (nothing is redeployed; the new result replaces the old one):
```
/curate-skills:curate-monitor --run <run_id> --minutes 20
```

## Settings

| Setting | Flag | Environment variable | Default |
|---|---|---|---|
| Catalog folder | `--catalog DIR` | `CURATE_CATALOG_DIR` | `~/.curate/catalog` |
| Extra subagent attempts after a failed stub test | `--mga-retries N` | `CURATE_MGA_RETRIES` | `3` (`0` = original CURATE) |
| Stub-test mode | `--stubtest-mode strict\|parity` | `CURATE_STUBTEST_MODE` | `strict` |
| Monitor time | `--minutes N` (curate-monitor) | `CURATE_MONITOR_MINUTES` | `10` |
| Deploy name suffix | — | `CURATE_DEPLOY_NAME_SUFFIX` | `-<MMDD-HHMM>` of the run that approved Gate 4 |

A flag beats the environment variable, which beats the default. `CURATE_DEPLOY_NAME_SUFFIX` and
`CURATE_MONITOR_MINUTES` can also go in `~/.config/curate/.env` (the environment overrides it). A run pins the first three when
it starts and the monitor time when monitoring starts. `strict` tests every rank of a parallel
step, installs the function's packages in an isolated environment and requires every declared
output; `parity` is the original CURATE behaviour (rank 1 only, no installs).

The plugin also declares Claude Code plugin settings (`catalog_dir`, `mga_retries`,
`stubtest_mode`, `monitor_minutes`), but **they don't apply when the plugin is loaded with
`--plugin-dir`**, as above. Use the flags or environment variables.

## Where files live

- `.curate/runs/<run_id>/` in the folder you started in: the run's state (`state.json`), one
  folder per function (spec, code, test data, stub-test log), gate history, `deploy/` (the deploy
  log). Add `.curate/` to `.gitignore`.
- `faasr_output/<workflow>-<run_id>/`: the bundle — `workflow.json`, `functions/`, `spec.json`,
  `external_inputs.json`, `README.md`. Unset deploy values appear as `<FAASR_…>` placeholders;
  only secret **names** are written.
- The catalog folder (`~/.curate/catalog` by default): `functions/<uuid>.json`,
  `workflows/<uuid>.json`, in the original prototype's format.
- `~/.config/curate/.env`: your deploy settings (mode 600), read by deploy, monitor and doctor; an
  environment variable of the same name overrides it.
- `.curate/mga_guard.log`: writes the guard denied.

## License

MIT (`LICENSE`). Parts are derived from the CURATE prototype, MIT, Copyright (c) 2026 Nolan
Cutler — see `NOTICE`.
