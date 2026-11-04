---
name: curate-setup
description: Set up deploying with curate-skills — show the prerequisites (your own action repo, a GitHub token, an S3 bucket, the repo's Actions secrets), ask for the non-secret deploy settings one at a time, write ~/.config/curate/.env (mode 600) with empty lines for the secrets, print the deploy launch alias, and finish with doctor --deploy --online. Only for users who want to deploy; run it by name.
argument-hint: "[--check]"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup_env.py *), AskUserQuestion
---

# /curate-setup

Helper: `setup.py …` below means `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup_env.py …`. Each call
prints one JSON object; `ok: false` comes with an `error` that says what to do.

Arguments: `$ARGUMENTS`. With `--check`, go straight to step 6.

## Secrets: never in this chat

GH_PAT, S3_AccessKey and S3_SecretKey are **never** asked for, typed, shown or passed to a command
here. The user pastes them into the file in their own editor. If the user pastes anything that
looks like a token or key anyway (e.g. `ghp_…`, `github_pat_…`, `gho_…`, `AKIA…`, or a long random
string where a name was expected), at any step:
- do **not** repeat it, quote it, or pass it to any command (not even `setup.py validate`);
- tell them: "That looks like a secret, so I didn't use it. It is now in this conversation's
  history: revoke it and create a new one (GitHub: Settings → Developer settings → Personal access
  tokens; AWS: IAM → your user → Security credentials), then paste the **new** one into the file
  yourself.";
- then continue with the step you were on.
Never open, read, `cat` or `source` the `.env` file yourself; `setup.py status` reports what is in
it (secrets only as set / empty / missing).

## 1. Prerequisites — show these first, before asking anything

Show this list, then ask with AskUserQuestion "Do you have these, or want to continue anyway?"
— **Continue** / **Stop** (Stop ends here).

1. **Your own action repo on GitHub.** FaaSr runs each function as a GitHub Actions workflow in
   it; deploy commits the workflow files and function code there. Create one at
   https://github.com/new — any name (e.g. `faasr-actions`), public or private, with **Add a
   README** ticked (it needs one commit and a default branch). Or in a terminal:
   `gh repo create faasr-actions --private --add-readme`. Use your own repo, not a shared one.
2. **A GitHub token.** Classic (GitHub → Settings → Developer settings → Personal access tokens →
   Tokens (classic)) with scopes **repo** and **workflow**; or fine-grained, limited to the action
   repo, with **Contents, Workflows and Actions: read and write** and **Secrets: read**. (Doctor can
   verify a classic token fully; for a fine-grained one only the read side.)
3. **An S3 bucket** (AWS S3, or an S3-compatible service such as MinIO) and an access key pair
   that can read and write it.
4. **Actions secrets in the action repo**, used by the workflows at run time: `GH_PAT`,
   `S3_AccessKey`, `S3_SecretKey` (same values as above). Repo → Settings → Secrets and variables
   → Actions → New repository secret; or in a terminal `gh secret set GH_PAT -R <owner>/<repo>`
   (it prompts for the value), likewise for the other two. Deploy's preflight checks the names.

## 2. Existing file?

`setup.py status`. If `exists` is false → step 3 with mode `--new`, asking for every key.

If it exists, show: the file, its non-secret `values`, each secret's state (set / empty /
missing), `missing` keys, `problems`, and whether `mode_ok` (600). Then AskUserQuestion "What
should happen to <file>?":
- **Add missing keys only (Recommended)** — only if `missing` is not empty; existing lines are not
  touched → step 3 with mode `--add-missing`, asking only for the missing non-secret keys;
- **Replace it** — a new file; the old one is kept next to it as `.env.bak-<time>` (mode 600) →
  step 3 with mode `--replace --confirmed`, asking for every key (offer the current values as the
  defaults);
- **Leave it as is** → step 5.
Never replace the file without that answer.

## 3. Ask for the non-secret settings, one at a time

`setup.py keys` gives the six keys in order, each with a `hint` and maybe a `default`:
FAASR_GH_USERNAME, FAASR_ACTION_REPO, FAASR_ACTION_BRANCH, FAASR_S3_ENDPOINT, FAASR_S3_BUCKET,
FAASR_S3_REGION. For each key you need, in that order:
- Ask in one short message: the key, its hint, and the default if there is one ("reply `default`
  to use it"). One key per message; wait for the answer.
- If the answer looks like a secret, follow "Secrets" above and ask again.
- If it contains a quote, backtick, `$`, `;`, `&`, `|`, `<`, `>` or a newline, don't run anything:
  no valid value has those; say so and ask again.
- Otherwise `setup.py validate <KEY> '<answer>'` (single quotes). If `ok` is false, show the
  `error` and ask again. Use the returned `value`.

## 4. Write the file

`setup.py write <mode> --set KEY='value' …` with every value from step 3 (`--new`,
`--add-missing`, or `--replace --confirmed`). Show `action`, the file path and `mode` 600 (and
`backup` after a replace). The three secrets are written as empty lines under `# paste here`.

Then tell the user, as their next step (outside this chat):
- open the file in an editor — e.g. `open -e ~/.config/curate/.env` (macOS TextEdit) or
  `nano ~/.config/curate/.env` — and paste each secret right after its `=` (no quotes, no spaces);
- if not done yet, add the same three as Actions secrets in the action repo (step 1, item 4).
AskUserQuestion "Tell me when the secrets are in the file." — **Done, check now** / **Later**
(Later: skip to step 5, then end with "Run `/curate-skills:curate-setup --check` when they are in.").

## 5. The deploy launch alias

`setup.py alias --plugin-root '${CLAUDE_PLUGIN_ROOT}'`. Show the `alias` line in a code block with
its `explain` and `where`. Say: start deploy sessions with `curate-deploy` (not `source` the file
in their shell); the plain `curate` alias stays for sessions that don't deploy.

## 6. Check

`setup.py doctor --plugin-root '${CLAUDE_PLUGIN_ROOT}'` (it reads the file itself and runs doctor
with `--deploy --online`; nothing is sourced in a shell). Show `report` verbatim in a code block.
- `empty_secrets` not empty → the secrets aren't in the file yet: say which, and to rerun
  `/curate-skills:curate-setup --check` after pasting them.
- A FAIL on `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` is about *this* session; the `curate-deploy`
  alias sets it.
- Other FAIL or warn lines: list each with the fix its message names. Don't fix anything yourself.
End with: once doctor shows the deploy lines ok, deploy from a session started with
`curate-deploy` (Gate 4 → **Approve & deploy**, or `/curate-skills:curate-deploy --run <run_id>`).
