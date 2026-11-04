---
name: curate-doctor
description: Check that this machine is ready to run curate-skills — uv, the python3 the write-guard hook needs, Python 3.13 with CURATE's packages, the launch variable, the plugin loaded once, the catalog folder, .curate/ git-ignored — and, with --deploy, the deploy variables and a GitHub token. Read-only. Use it on your own when the user asks whether curate is set up, or when a curate command fails for an environment reason (core checks, and --deploy for deploy problems). --online contacts GitHub and S3 with the user's credentials: only when the user asks for it.
argument-hint: "[--deploy [--online]]"
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/doctor.py *)
---

# /curate-doctor

Arguments: `$ARGUMENTS` (only `--deploy` and `--online` are passed on; ignore anything else).

**When to add which flag.** You may run doctor on your own initiative — the core checks, and
`--deploy` when the problem is about deploying (`--deploy` only looks at variable names and asks
`gh` for a token; it contacts nothing). Add `--online` **only** when the user asked for it in
their own words (e.g. "check my repo and bucket", `/curate-skills:curate-doctor --online`): it
uses the user's GitHub token and S3 keys to read their repo and bucket. Never add `--online` because it seems useful.

1. Run exactly this, adding `--deploy` and/or `--online` as allowed above:

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
user to the README's Deploy section and `.env.template` in the plugin folder.
