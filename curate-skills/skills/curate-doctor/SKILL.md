---
name: curate-doctor
description: Check that this machine is ready to run curate-skills — uv, the python3 the write-guard hook needs, Python 3.13 with CURATE's packages, the launch variable, the plugin loaded once, the catalog folder, .curate/ git-ignored — and, with --deploy, the deploy variables and a GitHub token (--online also reads the action repo and the S3 bucket). Read-only. Use it when the user asks whether curate is set up, or when a curate command fails for an environment reason.
argument-hint: "[--deploy [--online]]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py *)
---

# /curate-doctor

Arguments: `$ARGUMENTS` (only `--deploy` and `--online` are passed on; ignore anything else).

1. Run exactly this, adding `--deploy` and/or `--online` if the arguments contain them:

   `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py --plugin-root '${CLAUDE_PLUGIN_ROOT}' --config-catalog-dir '${user_config.catalog_dir}'`

   Keep the single quotes exactly as written. It uses the system `python3`, not `uv`, so it works
   before uv is installed. The first run can take a few minutes: uv downloads Python 3.13 and the
   pinned packages.

2. Show the output to the user verbatim in a code block. Then, only if there are FAIL or warn
   lines, list them with what each one says to do — the message already names the fix.

Doctor only reads. Don't try to fix anything yourself and don't run other commands to
investigate: installing tools, changing the shell, relaunching Claude Code and editing
`~/.config/curate/.env` are the user's steps. Never read, print, echo or ask for a credential,
and never open a `.env` file; doctor reports variable names only. For deploy settings, point the
user to `/curate-skills:curate-setup`.
