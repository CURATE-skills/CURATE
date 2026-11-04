# Stage: deploy (FaaSr on GitHub Actions, four confirmed steps)

Paths below are relative to `<plugin>`; `spec.py …`, `deploy.py …` are the script shorthand.

Precondition: `spec.py begin --run <run> --stage deploy` returned `ok` (Gate 4 was approved with
**Approve & deploy**, and the bundle's hash was recorded then).

Deploying writes to the user's GitHub repo and starts a workflow on their account. **Each of the
four steps is the user's decision, asked right before it runs.** A yes to one step is never a yes
to the next, and nothing said earlier ("deploy it all", "don't ask me") replaces asking.

## Credentials

`deploy.py` reads them itself, from the environment or else from `~/.config/curate/.env` (CURATE's
names: `GH_PAT` or `gh auth token`, `FAASR_GH_USERNAME`, `FAASR_ACTION_REPO`, `FAASR_ACTION_BRANCH`,
`FAASR_S3_*`, `S3_AccessKey` / `S3_SecretKey` or their upper-case spellings). Never read, print,
echo or ask for a credential, never open a `.env` file, and never put a value on a command line.
If the preflight says one is missing, tell the user which name is missing and that it goes in
`~/.config/curate/.env` (copied from the plugin's `.env.template`; `/curate-skills:curate-doctor
--deploy` checks it). deploy.py reads the file on every call, so no restart is needed; then stop
(step 4).

## 0. Invoke only (reuse an identical existing deployment)

`deploy.py status --run <run>`. If `reuse_possible` names a run (this run was started with
`--start-at deploy --from-run` from a run that deployed the same bundle), the deployment may
already be in the repo:
1. `deploy.py reuse --run <run>` **without** `--confirmed`. It writes nothing. It checks that
   every deployed file is byte-identical to what this run would deploy (function files, the
   deploy copy of workflow.json, and the Actions files regenerated with CURATE's generator).
2. Print its `preflight_summary` exactly — it includes the field-by-field table of the deploy copy
   vs the approved bundle — then tell the user the `plan`: the reused run, the identical files,
   and that the new invocation rewrites the deployment's earlier outputs in the S3 paths listed.
   If it returned `problems` instead, show them exactly and stop (step 4); nothing is reused.
3. Ask with AskUserQuestion — question `Invoke only: start <deploy_name> again (nothing is
   written to GitHub)?`, header `Invoke`, options **Yes, invoke** and **Stop here**.
4. Only after **Yes, invoke** in the user's answer just now: `deploy.py reuse --run <run>
   --confirmed`. It records publish, upload and register as reused (byte-identical) and runs the
   invoke step. Then continue at 2.5 (the result) below.

Otherwise go on with the preflight.

## 1. Preflight (read-only)

`deploy.py preflight --run <run>`. **Print its `summary` exactly as a message** (the user does not
see tool results; never re-format it). It lists every path the deploy writes, all with the
`-cs` deploy name, and a table of how the deploy copy differs from the approved bundle.
If `ok` is false: say the deploy cannot start, list the `problems` exactly, and stop (step 4).

## 2. The four steps

`deploy.py status --run <run>` → `next_step` (publish, upload, register, invoke; finished steps
are skipped). For that step:

1. `deploy.py <step> --run <run>` **without** `--confirmed`. It writes nothing and returns
   `needs_confirmation` with a `plan` (`what`, `repo`, `branch`, `paths`). If it returns
   `problems` instead, show them exactly and stop (step 4).
2. Tell the user, in your message, step N of 4: the plan's `what`, the repo and branch, and every
   path in `paths`.
3. Ask with AskUserQuestion — question `Deploy step N/4: <what>?`, header `Deploy N/4`, options
   **Yes, <step>** and **Stop here**. Without AskUserQuestion (a headless run), ask in chat and
   end your turn; only the user's next message can be the answer.
4. Only if the user chose **Yes** for this step in their answer just now:
   `deploy.py <step> --run <run> --confirmed`. Anything else is not a yes: **Stop here** → step 4
   below; a question → answer it, then ask again (3).
5. Show the result in one line, then go back to `deploy.py status` for the next step.
   After invoke, print the returned `message` (the deploy name and `invocation_id`); the run
   moves on to `monitor`, which checks the Actions runs, FaaSr logs and outputs read-only.

Never pass `--confirmed` without the user's yes to that step in their latest answer, and never
pass `--overwrite-own-deploy` unless the user chose it (below).

## 3. When a step fails

The run stays at `deploy`. Show the `error` exactly. What the step already wrote is in the run's
deploy log; running that step again is refused while those files exist, unless the user agrees to
overwrite this run's own files. Ask with AskUserQuestion — **Retry, overwriting this run's own
files** / **Stop here** — and only after the first, repeat the step (from 2.1) with
`--overwrite-own-deploy` added to both calls. It never allows anything else: other deployments,
CURATE's files and paths not in this run's deploy log stay refused.

## 4. Stopping

`spec.py set-stage --run <run> --stage stopped`; tell the user its `resume_command` and that
`/curate-skills:curate-deploy --run <run_id>` continues with the next unfinished step (the
finished ones are recorded and skipped). Nothing is undone.

## 5. Done

Finish as `<plugin>/references/gates.md` "After the gate decision" says; `spec.py next` names
`curate-monitor`.
