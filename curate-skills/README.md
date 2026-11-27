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
| `/curate-skills:curate-skeleton` | Turns the sketch into an outline: steps, order, ranked steps (Gate 1) |
| `/curate-skills:curate-reuse` | Searches the catalog; decides reuse, adapt or new for each step |
| `/curate-skills:curate-resolve` | Adds the details: input/output files, data flow, packages, and the date ranges dated data uses, read against the day it ran (Gate 2) |
| `/curate-skills:curate-generate` | Writes and tests each function (Gate 3) |
| `/curate-skills:curate-emit` | Builds and validates the FaaSr workflow file and bundle (Gate 4) |
| `/curate-skills:curate-deploy` | Deploys the bundle to your repo and bucket, or runs it on Pegasus/HTCondor, asking before each step (optional) |
| `/curate-skills:curate-monitor` | Watches a deployed or Pegasus run until it succeeds, fails or times out |
| `/curate-skills:wda-finalize` | Summarizes the run and saves verified functions to the catalog (Gate 5) |
| `/curate-skills:curate-catalog` | Inspects and manages the catalog: where, list, search, import, export |
| `/curate-skills:curate-doctor` | Checks your setup (deploy settings with `--deploy`, the Pegasus target with `--pegasus`) |

Internal, not run directly: the `curate-mga` subagent writes each function, with the
`function-reference` skill preloaded. Every function is a plain `main(inputs, outputs, params)`
on local paths, so the same code runs on FaaSr and on Pegasus.

## Requirements

- macOS or Linux (tested on macOS).
- [Claude Code](https://code.claude.com) (tested on 2.1.318).
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) (tested 0.13.1). 
- `python3` 3.9 or newer
- `git`, to clone this repository.
- Only for deploying: a GitHub account, an S3 bucket — see [Deploy](#deploy).

## Install

```bash
git clone https://github.com/CURATE-skills/CURATE.git
```

The plugin is the `curate-skills/` folder inside the clone. Add an alias to `~/.zshrc` (or
`~/.bashrc`), with the **absolute** path to that folder, then open a new terminal:

```bash
alias curate='CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 claude --plugin-dir /absolute/path/to/CURATE/curate-skills'
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

Each workflow is published in `workflows/<name>-v<N>/` in the folder you started in, and each run
of it (an execution) gets `runs/<run-id>/` there, with its results in `outputs/`. The /curate
session itself lives in `.curate/sessions/` (see [Where files live](#where-files-live)).

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

Before Gate 1 you are asked for the **workflow's name** (lower-case words joined by dashes). It
becomes the workflow's ID, `<name>-v<N>`: if the name is already in your catalog, you choose whether
this is a new version of that workflow or pick another name. The version number is assigned when
you approve Gate 4.

| Gate | After | You review |
|---|---|---|
| 1 | skeleton | the steps and their order |
| 2 | reuse + resolve | per function: new, from the catalog, adapted, or cached; files and data flow; packages |
| 3 | generate | the code and each function's stub-test result (ask to see any function) |
| 4 | emit | the validated workflow draft and a short summary per target (no file lists; those are in the run's `review/gate4.md`); **approve & deploy to FaaSr (cloud)**, **approve & run on Pegasus (this machine or your host · image)**, **… with a different image…**, **approve, no deploy**, or type **stop** (see below). Approving publishes the draft as the next version; if it is identical to an existing version, you choose to keep both or use that one |
| 5 | finalize | the summary; which verified functions to save to the catalog |

A change at Gates 2–4 sends the run back to the right stage (structural changes to the skeleton,
functional ones to reuse; working functions come back as `[cached]`). Each change request is one
revision round; after 5, only approve or stop.

**After Gate 4**, `/curate-skills:curate` carries on in the same session:
- **approve & deploy to FaaSr (cloud)** → deploy → after invoke, monitor watches the run → on
  success, finalize (Gate 5). It stops early only if you choose **Stop here** at a deploy step, or
  monitor ends with `failure` or `timeout`; each time it prints the command to continue.
- **approve & run on Pegasus** → the Pegasus steps (prepare, [copy,] launch, each asked) → monitor
  → finalize, with the same stops ([Run on Pegasus](#run-on-pegasus-optional)).
- **approve, no deploy** → straight to finalize (Gate 5).

A stage skill started on its own (`/curate-skills:curate-deploy`, `curate-monitor`, …) stops
after its stage and prints the next command instead.

## Start at a stage, resume, rewind

| To… | Command |
|---|---|
| Resume a stopped session | `/curate-skills:curate --resume <session_id>` |
| Redo a session from a stage, without changing the original | `/curate-skills:curate --start-at <stage> --from-run <session_id>` |
| Change one function of a finished session | `/curate-skills:curate --start-at generate --from-run <session_id> --existing-code keep`, then ask for the change at Gate 3. Only that function is rewritten. |
| Rewrite all functions of a finished session | `/curate-skills:curate --start-at generate --from-run <session_id> --existing-code ignore` |
| Run an approved version again | `/curate-skills:curate --start-at deploy --workflow <workflow-id>` (a new run of it; ends at Gate 5 with nothing to save) |
| Start from other sources | `--from-bundle <folder>` (generate, emit) · `--from-file <file>` · `--sketch "<text>"` / `--sketch-file <file>` (skeleton) |
| Rewind the same session | `/curate-skills:curate-<stage> --session <session_id>` |
| Run one stage only | `/curate-skills:curate-<stage> --session <session_id>`. Stages: `skeleton`, `reuse`, `resolve`, `generate`, `emit`; finalize is `wda-finalize`. Deploy and monitor act on a run: `--run <workflow-id>/<run-id>` |

A **session** is one `/curate` session (sketch to Gate 5); a **run** is one execution of a published
version, named `<workflow-id>/<run-id>`. Session commands take `--session` (`--run`, the old name,
still works); deploy and monitor take `--run`, and given a session id use its current run. An id
that names both is refused: say `--session <id>` or `--run <workflow-id>/<run-id>`.

- **New sessions and runs** copy only gate-approved results, never change the source, and ask "Start this run?" first.
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

Read-only check of the file, token, repo, branch and bucket. It also warns when two lines of the
file run together (a missing newline) or a secret has an implausible length (cut short or run on).
Values are never shown.

**Auto mode** blocks deploy after the first step. Use manual mode (Shift+Tab) for deploy (it asks
before each command), or add these to the `allow` list in `~/.claude/settings.json` — deploy offers
to, and `doctor --deploy` prints them with your path; in Claude Code,
`! uv run --quiet /absolute/path/to/CURATE/curate-skills/scripts/allow_deploy.py` adds them
(`--project` for this project only; it backs the file up and shows the change):
```
Bash(uv run --quiet /absolute/path/to/CURATE/curate-skills/scripts/deploy.py *)
Bash(uv run --quiet /absolute/path/to/CURATE/curate-skills/scripts/monitor.py *)
```

### 4. Deploy
At Gate 4, choose **approve & deploy to FaaSr (cloud)** (or run `/curate-skills:curate-deploy --run <workflow-id>/<run-id>`).
After a read-only preflight, deploy asks before each of four steps:
1. **Publish:** function files to the repo
2. **Upload:** `workflow.json` to the repo
3. **Register:** GitHub Actions files
4. **Invoke:** start the workflow

A workflow version is deployed once, named after its workflow id (`<workflow-id>`): its function
files, Actions files and a marker (`workflows/<workflow-id>/deployment.json`) recording which version
it is. Each run of that version adds only its own payload (`workflows/<workflow-id>/<run-id>.json`)
and S3 folder (`<workflow-id>/<run-id>/`), so two runs never share either; a later run reuses the
deployment when it is byte-identical. A deployment of another version under the same name is
refused (tear it down, or set `CURATE_DEPLOY_NAME_SUFFIX`); one of this version whose files differ
is refused until `deploy.py update-deployment --workflow <workflow-id>` re-pushes them.
`/curate-skills:curate-deploy --list` shows this project's deployments, each version's with its
runs underneath (active, finished, not invoked or torn down) and the projects its marker lists; `--teardown --run
<workflow-id>/<run-id>` deletes that run's payload, outputs and logs, and `--teardown --workflow
<workflow-id> --all` the version's whole deployment and every run's files, each after asking once
whether to keep the outputs (the default), delete them too, or download them first. Local run
folders are never deleted.

### 5. Monitor
Under `/curate`, monitoring starts after invoke and watches the run for up to 10 minutes,
with one line per function as it changes (`… running`, `✓ succeeded`, `✗ failed: <reason>`).
On success, finalize follows. On failure or timeout, the run stops; to re-check the same
invocation (nothing is redeployed; the new result replaces the old one):
```
/curate-skills:curate-monitor --run <workflow-id>/<run-id> --minutes 20
```

## Run on Pegasus (optional)

Runs the approved workflow with [Pegasus](https://pegasus.isi.edu) on an HTCondor pool, on this
machine or on a host you reach with ssh, instead of FaaSr on GitHub Actions. Nothing goes to
GitHub or S3. The functions run unchanged, each in a container, through CURATE's workflow format
(a CWL `workflow.yml`, described in the repository's `docs/CWL_IR.md`). CURATE builds Pegasus's
own `workflow.yml` from it on this machine; the Pegasus machine only plans and runs it.

### 1. Prerequisites
On the Pegasus machine: Pegasus 5.1+ (the generated `workflow.yml` needs `pegasus-plan` 5.0.4 or
later; check with `pegasus-version`), HTCondor, Docker (or Apptainer), and a `python3` that can
import `Pegasus.api` and `yaml` with `PYTHONPATH=$(pegasus-config --python)`. For another host:
ssh login that doesn't ask for a password (`ssh <host> true` works).

### 2. Settings
In `~/.config/curate/.env` (see `.env.template`; none are secrets):

| Variable | Default | Meaning |
|---|---|---|
| `CURATE_PEGASUS_HOST` | this machine | an ssh host to run on instead |
| `CURATE_PEGASUS_RUNTIME` | `docker` | `docker` or `singularity` (Apptainer) |
| `CURATE_PEGASUS_IMAGE` | `docker:///python:3.12-slim` | the image every function runs in; it must already have their packages |

```
/curate-skills:curate-doctor --pegasus
```
checks them: Pegasus, its Python API, HTCondor and the container runtime, here or over ssh.

**Auto mode** blocks the Pegasus run after its first step, as for deploy; the run offers to add
two rules (`pegasus_deploy.py`, `monitor.py`), or in Claude Code:
`! uv run --quiet /absolute/path/to/CURATE/curate-skills/scripts/allow_deploy.py --pegasus`.

### 3. Run
At Gate 4, choose **approve & run on Pegasus** (the label names the machine and the image). Gate 4
shows a short summary (run name, jobs, image, deliverables), or why the workflow can't run on
Pegasus (e.g. a conditional edge). To use another image for this run only, choose **approve & run
on Pegasus with a different image…** and give its address; it is checked, the summary is shown
again with it (with package warnings), and you confirm. Your settings file is not changed.
Accepted addresses: `docker:///<repo>:<tag>` (or `docker://…`), or a `file:///…` `docker save` tar;
with `CURATE_PEGASUS_RUNTIME=singularity` also `library://`, `shub://`, `oras://` and `file:///….sif`.
After a read-only preflight, it asks before each step:
1. **Prepare:** builds the workflow folder from the approved version into
   `workflows/<workflow-id>/runs/<run-id>/bundle/` (validated, with `manifest.json`); input
   files, if the workflow needs any, are copied from a folder you name
2. **Copy** (ssh host only): to `<host>:~/curate-runs/<workflow-id>/<run-id>/`
3. **Launch:** plans and submits to HTCondor, and returns at once; HTCondor runs the workflow

The monitor then follows it (as for FaaSr; it reads the Pegasus run folder recorded at launch, so
it also works after a logout or reboot). When the workflow has finished, the results are in the
run's `outputs/` (`results/`, `run-report.json`, and `FAILED.txt` if it failed: then the outputs are
partial), then Gate 5. A re-check fills `outputs/` in and never overwrites a file; the Pegasus
workflow is named after the version, and each run has its own folders. A run started with
`--start-at deploy --from-run <session_id>` (or `--workflow <workflow-id>`) asks whether to deploy
to FaaSr or run on Pegasus (with the set image or a different one).

## Settings

| Setting | Flag | Environment variable | Default |
|---|---|---|---|
| Catalog folder | `--catalog DIR` | `CURATE_CATALOG_DIR` | `~/.curate/catalog` |
| Extra subagent attempts after a failed stub test | `--mga-retries N` | `CURATE_MGA_RETRIES` | `3` (`0` = original CURATE) |
| Stub-test mode | `--stubtest-mode strict\|parity` | `CURATE_STUBTEST_MODE` | `strict` |
| Monitor time | `--minutes N` (curate-monitor) | `CURATE_MONITOR_MINUTES` | `10` |
| Deploy name suffix | `--name-suffix` (deploy.py) | `CURATE_DEPLOY_NAME_SUFFIX` | none: the deployment is named `<workflow-id>` |

A flag beats the environment variable, which beats the default. `CURATE_DEPLOY_NAME_SUFFIX` and
`CURATE_MONITOR_MINUTES` can also go in `~/.config/curate/.env` (the environment overrides it). A run pins the first three when
it starts and the monitor time when monitoring starts. `strict` tests every rank of a ranked
step, installs the function's packages in an isolated environment and requires every declared
output; `parity` is the original CURATE behaviour (rank 1 only, no installs).

The plugin also declares Claude Code plugin settings (`catalog_dir`, `mga_retries`,
`stubtest_mode`, `monitor_minutes`), but **they don't apply when the plugin is loaded with
`--plugin-dir`**, as above. Use the flags or environment variables.

## Where files live

In the folder you started in (the project):

- `workflows/`: CURATE's published workflows, marked by `workflows/.curate-workflows` (CURATE
  refuses to publish into an existing `workflows/` folder without that marker).
  - `workflows/<name>-v<N>/`: a version, published when Gate 4 is approved and never edited
    afterwards (`workflow.yml` in CWL with `s:identifier`, `s:version` and `curate:content_hash`;
    `inputs.yml`; `functions/`), and `approved.json` (the spec Gate 4 approved, checked against the
    version whenever it is read, so `--start-at deploy --workflow` can run it again).
  - `workflows/<name>-v<N>/runs/<run-id>/`: one run of it, `<name>-v<N>/<run-id>`: its own
    `state.json`, the target's `bundle/` (built from the version once the target is chosen),
    `manifest.json` (the version's hash, the CURATE version, the target, the bundle's hash),
    `deploy/` (FaaSr's deploy copy), `deploy-log.jsonl` (every path a deploy step wrote, here, on
    GitHub, in S3 or on a Pegasus host), `logs/`, `monitor-report.md`, Pegasus's `work/` and
    `result/`, and **`outputs/`, the run's results folder**: Pegasus copies the delivered files
    there when the workflow finishes; for FaaSr, `monitor.py download-outputs --run <run>` copies
    the same files from S3 to the same paths (with `manifest.json`; a later download fills in what
    was missing and never overwrites). `--all` also copies every intermediate file into
    `outputs/_all/`, which `workflows/.gitignore` ignores.
  - `workflows/.gitignore`, written the first time CURATE publishes: it keeps every version and,
    of each run, `outputs/` (but not `outputs/_all/`) and `monitor-report.md`, and ignores the rest of each run (bundle,
    work folders, logs, deploy copies, state files), so you can commit `workflows/` and move it
    between machines. CURATE never rewrites it once it exists; edit it freely.
- `.curate/sessions/<session_id>/`: a /curate session's state (`state.json`), one folder per
  function (spec, code, test data, stub-test log), gate history, and `draft/` (the workflow draft
  emit writes and Gate 4 approves: `workflow.yml`, `inputs.yml`, `functions/`). Add `.curate/` to
  `.gitignore`. Session and run IDs look like `20261118-012903-a3f9`.
- `.curate/project-id`: a random id; a FaaSr deployment's marker records which projects use it
  by this id (never by a folder path). `.curate/mga_guard.log`: writes the guard denied.
- Projects made before this layout keep versions in `.curate/workflows/` and sessions in
  `.curate/runs/`. Both are still read, and nothing is moved: a version there gets its new runs in
  its own `runs/`, and a session there keeps its old run files (`deploy/`, `pegasus/`).
- The FaaSr bundle (`workflow.json`, `functions/`, `external_inputs.json`, `outputs.json`) is the
  run's `bundle/`; only secret **names** are written. The Pegasus bundle there is the workflow
  folder: Pegasus's `workflow.yml` (no host paths), `functions/`, `inputs/`; the host's catalogs
  and `pegasus.properties` go in `work/`. Sessions approved before workflow versions (whose bundle
  was in `faasr_output/` or `.curate/runs/<id>/pegasus/ir/`) can no longer be deployed, run or
  monitored: start again from their spec (`--start-at emit --from-run <session_id>`).

Elsewhere:

- Your action repo (FaaSr): `functions/<workflow-id>/`, `.github/workflows/<workflow-id>-<action>.yml`
  and `workflows/<workflow-id>/deployment.json` (one deployment per version), and
  `workflows/<workflow-id>/<run-id>.json` per run. Your bucket: `<workflow-id>/<run-id>/` per run,
  and FaaSr's logs under `FaaSrLog/<workflow-id>/`.
- A Pegasus host over ssh: `~/curate-runs/<workflow-id>/<run-id>/`.
- The catalog folder (`~/.curate/catalog` by default): `functions/<uuid>.json`; workflows as
  `workflows/<name>-v<N>.json` (each save is a new version; older entries keep `<uuid>.json`) and
  their version folders in `versions/<name>-v<N>/`; `reservations.json` records the version number
  each older uuid entry holds (so new versions never take it).
- `~/.config/curate/.env`: your deploy and Pegasus settings (mode 600), read by deploy, monitor
  and doctor; an environment variable of the same name overrides it.

## License

MIT (`LICENSE`). Parts are derived from the CURATE prototype, MIT, Copyright (c) 2026 Nolan
Cutler — see `NOTICE`.
