# Stage: monitor (one deployed invocation, read-only)

Paths below are relative to `<plugin>`; `spec.py …`, `monitor.py …` are the script shorthand.

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

## 2. The result

The last entry of `lines` is the verdict (`✓ workflow succeeded` / `✗ workflow failed: …` /
`… not finished after N min`). Then:

| `result` | Next |
|---|---|
| `success` | the run moved to `finalize`; go on ("After the gate decision") |
| `success_unconfirmed` | the run moved to `finalize`; Gate 5 first asks the user to accept runs matched by time only (`stages/finalize.md`) |
| `failure` | the run is stopped. Tell the user, in one line each: `recheck_command` re-checks the same invocation (nothing is redeployed) and can replace the verdict; the details are in the `details` file. |
| `timeout` | the run is stopped. Tell the user `recheck_command` (add `--minutes N` for a longer watch): it continues watching the same invocation with a fresh budget. |

Only if the user asks why or for details: read the `details` file (`deploy/monitor-report.md`
in the run) and answer from it. Job-log text is never shown or saved; excerpts there are
redacted by `monitor.py`.

## 3. Done

Finish as `<plugin>/references/gates.md` "After the gate decision" says (after a failure or a
timeout the run is stopped, so /curate stops too).
