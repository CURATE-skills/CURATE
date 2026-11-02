# Stage: monitor (one deployed invocation, read-only)

Paths below are relative to `<plugin>`; `spec.py …`, `monitor.py …` are the script shorthand.

Precondition: `spec.py begin --run <run> --stage monitor` returned `ok` (the deploy stage started
the workflow and recorded its InvocationID).

The monitor only reads: GitHub Actions runs (GET) and S3 (head/get). It never writes to GitHub or
S3 and never deploys again. Credentials are read by `monitor.py` itself from the environment, as in
deploy: never read, print, echo or ask for one, and never open a `.env` file.

## 1. Poll

`monitor.py poll --run <run> --config-minutes '${user_config.monitor_minutes}'` (keep the quotes
exactly), plus `--minutes N` if the arguments contain it. One call polls for at most 9 minutes;
the total is `monitor_minutes` (default 10, pinned for the run; an explicit `--minutes`, e.g. on
resume, replaces it). After each call tell the user one line: the `result`, `minutes_used`, and
the last entry of `interim` (runs concluded, outputs present / stale / missing). While `result`
is `pending`, call it again. Before every run has succeeded, files from an earlier invocation
show as stale; that is progress, not a failure.

What it checks, per action in workflow order (e.g. download → process 1–3 → plot):
- **Actions:** the action's GitHub Actions run(s) for this InvocationID, all concluded `success`.
  A run counts as this invocation's when its job log contains the InvocationID; a finished run
  whose job log is not available is matched **by time only** (unconfirmed).
- **Outputs:** every declared output file exists under the run's `-cs` S3 folder **and was written
  after the dispatch**; an older file is **stale** and fails the check.
- **FaaSr logs:** status per rank; an `[ERROR]` line fails the action.
- Output correctness (e.g. the plot itself) is not judged; the summary says so.

## 2. The result

Print the returned `summary` exactly as a message (never re-formatted), then:

| `result` | Meaning | Next |
|---|---|---|
| `success` | every Actions run succeeded (matched by InvocationID) and every output is present and fresh | the run moved to `finalize`; go on ("After the gate decision") |
| `success_unconfirmed` | as success, but some runs were matched by time only | the run moved to `finalize`; Gate 5 first asks the user to accept the unconfirmed match (`stages/finalize.md`) |
| `failure` | the summary names the failed action, why (Actions conclusion, `[ERROR]` in its FaaSr log, or a missing or stale output) and a redacted excerpt | the run is stopped; tell the user the `resume_command`. Resuming shows the same result (nothing is redeployed). |
| `timeout` | not finished within `monitor_minutes` | the run is stopped; tell the user the `resume_command` — resuming continues monitoring the same invocation with a fresh budget, never redeploys |

Job-log text is never shown or saved; excerpts are redacted by `monitor.py` before you see them.

## 3. Done

Finish as `<plugin>/references/gates.md` "After the gate decision" says (after a failure or a
timeout the run is stopped, so /curate stops too).
