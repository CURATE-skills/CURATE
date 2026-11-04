# curate-skills

A Claude Code plugin that turns a plain-language description of a scientific workflow into a
[FaaSr](https://faasr.io) serverless workflow — Python functions plus a `workflow.json` that runs
on GitHub Actions with S3 storage. It reimplements **CURATE** (*Composition, User-in-the-loop,
Reuse, and Automated Task Execution*; Cutler, Kuo, Velugoti, Newhart, Figueiredo; arXiv
2608.04270) as Claude Code skills, one subagent and a few scripts.

**What it is.** A pipeline of stages — skeleton → reuse → resolve → generate → emit → (deploy →
monitor) → finalize — that stops at **human approval gates** after each stage. Functions are
generated one at a time by a `curate-mga` subagent and tested locally against FaaSr stand-ins
before you see them. Verified functions go into a catalog that later runs reuse.

**What it isn't.** Not an unattended code generator: every gate waits for you. Not a FaaSr
runtime: it emits a bundle and, if you choose, deploys it to *your own* GitHub repo and S3
bucket, step by step with your confirmation. It does not judge whether your workflow's outputs
are scientifically right.

## Requirements

- macOS or Linux (tested on macOS).
- [Claude Code](https://code.claude.com) (tested on 2.1.318).
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) on `PATH` (tested 0.13.1). The
  scripts declare their own dependencies; on first use `uv` downloads Python 3.13 and pinned
  packages (pydantic 2.11.7, rank-bm25 0.2.2, FaaSr_py 0.1.13). Needs network access once.
- `python3` 3.9 or newer on `PATH`, for the write-guard hook and doctor. macOS ships one
  (`xcode-select --install` if it is missing). **If the hook can't run, Claude Code blocks every
  Write, Edit and Bash call while the plugin is enabled.**
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
catalog), stopping at each gate for your review (see [Gates](#gates)).

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
| 4 | emit | the validated bundle; **approve & deploy**, **approve, no deploy**, or **stop** |
| 5 | finalize | the summary; which verified functions to save to the catalog |

A change at Gates 2–4 sends the run back to the right stage (structural changes to the skeleton,
functional ones to reuse; working functions come back as `[cached]`). Each change request is one
revision round; after 5, only approve or stop.

## Start at a stage, resume, rewind

- **Resume** a stopped run, in any later session: `/curate-skills:curate --resume <run_id>`. It
  continues where the run stopped (a pending gate is shown again).
- **Start a new run from an existing one** (the source is never changed):
  `/curate-skills:curate --start-at <stage> --from-run <run_id>` copies that run's **gate-approved**
  results. You see "Start this run?" with what is copied and redone before anything happens.
  - Change one function of a finished run: `/curate-skills:curate --start-at generate --from-run
    <run_id> --existing-code keep`, then ask for the change at Gate 3; only that function is redone.
  - Regenerate all the code: `--existing-code ignore` instead.
  - Other sources: `--from-bundle <faasr_output folder>` (generate or emit), `--from-file <file>`,
    `--sketch "<text>"` / `--sketch-file <file>` (skeleton). Formats: `references/stage-io.md`.
- **Rewind** the same run: run a stage skill on it, e.g. `/curate-skills:curate-generate --run
  <run_id>`. If the run is already past that stage, it asks first and says the cost: one revision
  round, and that stage and every later one are redone.
- **One stage at a time**: `/curate-skills:curate-skeleton`, `curate-reuse`, `curate-resolve`,
  `curate-generate`, `curate-emit`, `curate-deploy`, `curate-monitor`, `wda-finalize`, each with
  `--run <run_id>`. Each stops after its gate and prints the next command.
- **Catalog**: `/curate-skills:curate-catalog where | list | search <q> | init | import <path> |
  export <dir>`.

## Deploy

Deploying is optional. It writes the bundle to **your own** GitHub repo and starts it there.

**1. Prerequisites**
- **Your own action repo** on GitHub, with a first commit (create it at
  https://github.com/new with "Add a README" ticked). FaaSr runs each function as a GitHub
  Actions workflow in it.
- **A classic GitHub token** with scopes **repo** and **workflow**: GitHub → Settings → Developer
  settings → Personal access tokens → Tokens (classic) → Generate new token (classic).
  (Fine-grained tokens aren't recommended: their write access can't be checked.)
- **An S3 bucket** (AWS, or S3-compatible such as MinIO) and an access key pair that can read and
  write it.
- **Three Actions secrets on the action repo**: `GH_PAT`, `S3_AccessKey`, `S3_SecretKey` (the same
  values): repo → Settings → Secrets and variables → Actions → New repository secret.

**2. The settings file.** From the clone's `curate-skills/` folder:

```bash
mkdir -p ~/.config/curate && cp .env.template ~/.config/curate/.env && chmod 600 ~/.config/curate/.env
```

Fill in `~/.config/curate/.env` with an editor (e.g. `open -e ~/.config/curate/.env`). Each line
has a comment saying what it is, where to find it and an example format; `SECRET` marks the token
and keys.

**Never paste a secret into a Claude Code session** (or any chat). They go only into that file and
the repo's Actions secrets. If you did paste one, revoke it and make a new one.

**3. Check before the first deploy.** In a `curate` session:

```
/curate-skills:curate-doctor --deploy --online
```

It checks the settings file (and that it is mode 600), which variables came from the file and
which from the environment (names only), the token (classic: repo + workflow), that the repo and
branch are readable and pushable, and that the bucket answers. It only reads; values are never
shown.

deploy, monitor and doctor read `~/.config/curate/.env` themselves, on every call; nothing needs
to be loaded into your shell and no other alias is needed. A variable set in the environment
overrides the file.

**4. Deploy.** Answer Gate 4 with **approve & deploy** (or run
`/curate-skills:curate-deploy --run <run_id>` on a run approved that way). Deploy:
- runs a read-only **preflight** and shows every path it would write — all under a `-cs` name
  (`<Workflow>-cs`), so it never overwrites another deployment — and refuses if any exists;
- then asks **four separate confirmations**, each right before it runs: **publish** (function
  files), **upload** (the deploy copy of `workflow.json`), **register** (the GitHub Actions
  files), **invoke** (start the workflow). A yes to one is never a yes to the next.

**5. Monitor.** After invoke, curate-monitor watches the run read-only — the Actions runs, FaaSr's
logs, and that every declared output was freshly written to S3 — for up to 10 minutes. Results:
`success`, `success_unconfirmed` (a run matched by time only; you're asked at Gate 5), `failure`,
or `timeout`. After a timeout it stops with a resume command; continue with a new budget:

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
| Deploy name suffix | — | `CURATE_DEPLOY_NAME_SUFFIX` | `-cs` |

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

## Troubleshooting

| Symptom | Fix |
|---|---|
| Anything odd at the start | `/curate-skills:curate-doctor`; fix the `FAIL` lines. |
| Generation stops with "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" | Quit and start with the `curate` alias. |
| Every Write/Edit/Bash is blocked | The hook can't run: `python3` is missing or older than 3.9. Install it, or disable the plugin. |
| "uv is not on PATH" | Install uv and open a new terminal. |
| Claude Code asks for permission during a run? | That's expected. Choose **Yes** (or **Yes, and don't ask again** for this project). Choosing **No** stops that step. |
| Skills appear twice / doctor says "loaded twice" | The plugin is also installed via `/plugin`; uninstall that copy or drop `--plugin-dir`. |
| Emit says the FaaSr_py validator could not run | uv couldn't fetch FaaSr_py (offline?); emit used its built-in checks instead. Rerun when online. |
| Deploy preflight lists missing variables | Fill them in `~/.config/curate/.env`, then check with `/curate-skills:curate-doctor --deploy`. |
| Deploy refuses: a path already exists | Another deployment uses that name. Set `CURATE_DEPLOY_NAME_SUFFIX` (e.g. `-cs-yourname`) in the `.env`. |
| Monitor ended with `timeout` | Resume with `/curate-skills:curate-monitor --run <run_id> --minutes N`. |
| A run stopped | `/curate-skills:curate --resume <run_id>`. |

## Known limitations

- **Gates and the rewind confirmation are instructions, not locks.** The skills tell Claude to wait
  for your answer and to ask before a rewind, and the scripts refuse a rewind or a deploy step
  without a `--confirmed` flag — but nothing technically stops a model from passing it.
- **The credential guard is a heuristic.** In any folder that has a `.curate/` folder, the hook
  denies main-session Bash commands that would print a credential (`printenv`, `cat .env`,
  `echo $GH_PAT`, …), and Read or Grep aimed at a `.env` file or `~/.config/curate/` ("curate
  never reads your .env; scripts load it themselves"; `.env.template` and the like are allowed).
  It checks names and commands, so it can be bypassed (e.g. a Grep over a parent folder), and it
  applies to every session in such a folder, not only curate's.
- **The write guard is a heuristic too.** A `curate-mga` subagent may write only inside its
  function's folder; Bash commands are checked by parsing, so after every subagent the run's
  files are also compared with a snapshot. For a hard boundary, enable Claude Code's sandbox.
- **Reuse is untested on a large catalog.** Catalog search (BM25) has been tried with a handful of
  functions only.
- Fine-grained GitHub tokens: doctor can't verify their write access (use a classic token).

## Tests

From `curate-skills/`:

```bash
uv run --no-project --python 3.13 --with pytest --with pydantic==2.11.7 --with rank-bm25==0.2.2 pytest tests -q
```

Ten tests that compare the vendored code with the prototype are skipped unless the prototype is
checked out at `../ref/curate`.

## Reproducing the paper's experiments

Sketches are in `experiments/prompts/` (E2 is reconstructed from SeBS-Flow §5; E4 is verbatim). The
paper used Claude Opus 4.8: add `--model claude-opus-4-8` to the alias; `curate-mga` uses the
session's model. E4 starts from the E2 catalog in `curate-skills/tests/fixtures/catalog-e2-mapreduce/`.

## How this differs from the prototype

`scripts/VENDORED.md`: which code is copied, adapted or new (with source lines and the prototype's
commit `3fd5c16`), and every deliberate behaviour change.

## License

MIT (`LICENSE`). Parts are derived from the CURATE prototype, MIT, Copyright (c) 2026 Nolan
Cutler — see `NOTICE`.
