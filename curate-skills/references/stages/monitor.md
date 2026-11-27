# Stage: monitor (one deployed invocation, read-only)

Paths below are relative to `<plugin>`; `spec.py …`, `monitor.py …` are the script shorthand.

Run scripts in the plain form `uv run --quiet <plugin>/scripts/<script>.py …`: no variable assignments, no `&&` chains, no subshells.

`<run>` is the run's `<workflow-id>/<run-id>`: for a session that reached this stage, `spec.py status
--session <session>` shows it as `reference`; a run started with `--start-at deploy` is its own. Every
command below takes it as `--run <run>` (spec.py too: it acts on the session the run belongs to).

Precondition: `spec.py begin --run <run> --stage monitor` returned `ok` (the deploy stage started
the workflow and recorded its InvocationID; or the run stopped here and this is a re-check).

The monitor only reads: GitHub Actions runs (GET) and S3 (head/get). It never writes to GitHub or
S3 and never deploys again. Credentials are read by `monitor.py` itself, from the environment or
`~/.config/curate/.env`, as in deploy: never read, print, echo or ask for one, and never open a
`.env` file.

## 1. Poll, in chunks

`monitor.py poll --run <run> --config-minutes '<monitor_minutes>'`, with `<monitor_minutes>`
replaced by the value the skill gives, kept inside the single quotes exactly as written, plus
`--minutes N` if the arguments contain it. Each call polls for about a minute
(`--chunk-seconds`, default 60), saves its progress and returns. Run it in the foreground (never
in the background).

After **every** call, print each entry of `lines` on its own line, exactly as returned, and
nothing else about the poll: no tables, no output-file states, no poll counts. `lines` holds
only what changed since the previous call (it may be empty: then print nothing), e.g.

```
✓ download-ghcn-station succeeded
… process-weather-variable running (1/3)
```

While `result` is `pending`, call it again. Stop calling when `result` is anything else. The
total time is `monitor_minutes` (default 10, pinned for the run; an explicit `--minutes`, e.g. on
a re-check, replaces it).

How it judges (for answering questions; don't recite it):
- Nothing is concluded while any Actions run of this invocation is queued or in progress. A
  fan-in step may run once per predecessor; every run counts.
- A function has succeeded when all its runs concluded `success`, its FaaSr logs have no
  `[ERROR]`, and its declared output files were written after the dispatch. An output that is not
  there yet gets a 60-second grace period after the function's last run.
- Output correctness (e.g. the plot itself) is not judged.

**Pegasus runs** (`deploy_choice` `pegasus`): the same `monitor.py poll` command, the same `lines`
and `result` values (no unconfirmed match). It reads, here or over ssh, the Pegasus run folder that
`state.pegasus.run_dir` records (so it works after a logout or reboot): DAGMan's exit status and
each job's state. It never submits, removes or changes the workflow. When DAGMan has finished, it
copies the declared outputs into the run's Pegasus work folder (`deliver/`, plus `run-report.json`)
and, for an ssh host, fetches both into the run's `result/`; then into the run's one `outputs/`, its
results folder (with `FAILED.txt` when the run failed: partial outputs, and a `manifest.json`), which
the last line names. A re-check that collects again fills the same `outputs/` in, as FaaSr's
`download-outputs` does (below); a line starting `!` names any hand-edited file it left alone. Success needs DAGMan exit 0 and
every declared output delivered. A `timeout` means DAGMan is still running the workflow; the
re-check continues watching it. Its details file is `monitor-report.md` in the run's folder.
A Pegasus run started before phase 8 (an `ir.yaml` folder) can no longer be monitored or collected. A FaaSr run's outputs stay in S3 until `monitor.py download-outputs --run <run>` copies its
deliverables into the run's `outputs/` (its results folder; `--to <folder>` for an empty folder
elsewhere) at their `curate:deliver_as` paths, the same tree Pegasus delivers (e.g.
`results/word_count_1.json`), with a `manifest.json`; it takes them from the bundle's
`outputs.json`. `--all` also copies every intermediate object into `outputs/_all/`; offer it only
when the user asks for the intermediate files. Run it again after a
partial download: it fetches what is missing, keeps what is there, and never overwrites a file.

## 2. The result

The last entry of `lines` is the verdict (`✓ workflow succeeded` / `✗ workflow failed: …` /
`… not finished after N min`). Then:

| `result` | Next |
|---|---|
| `success` | the run moved to `finalize`; go on ("After the gate decision") |
| `success_unconfirmed` | the run moved to `finalize`; Gate 5 first asks the user to accept runs matched by time only (`stages/finalize.md`) |
| `failure` | the run is stopped. Print `stop_message` exactly (`/curate --resume` first, then `recheck_command`, which re-checks the same invocation and can replace the verdict), then one line: the details are in the `details` file. |
| `timeout` | the run is stopped. Print `stop_message` exactly (`/curate --resume` first, then `recheck_command`, which continues watching the same invocation with a fresh budget). |

Only if the user asks why or for details: read the `details` file the poll returned
(`monitor-report.md` in the run) and answer from it. Job-log text is never shown or saved; excerpts there are
redacted by `monitor.py`.

## 3. Done

Finish as `<plugin>/references/gates.md` "After the gate decision" says (after a failure or a
timeout the run is stopped, so /curate stops too).
