# Permission rules for curate-skills (optional)

During a run, Claude Code asks for permission before some steps; answering **Yes** is all a run
needs. These rules are for people who'd rather not be asked. They are optional.

Only the first turn of a curate command runs with the skill's own permissions; your gate
answers are later turns, where Claude Code asks before it runs the plugin's scripts, reads the
plugin's files, or writes the run's files under `.curate/` (the subagents too). Add these three rules once to your **user settings**, `~/.claude/settings.json`
(merge them into an existing `"permissions"` → `"allow"` list if you have one), and they apply in
every project:

```json
{
  "permissions": {
    "allow": [
      "Bash(uv run --quiet /absolute/path/to/CURATE/curate-skills/scripts/*)",
      "Read(//absolute/path/to/CURATE/curate-skills/**)",
      "Edit(./.curate/**)"
    ]
  }
}
```

- `Bash(…)` allows only commands that start with `uv run --quiet` and this plugin's `scripts/`
  folder — not `uv run` in general. Use the same absolute path as in the alias, without quotes.
- `Read(//…)`: the plugin's own files. `//` marks an absolute path; a single `/` would be read
  relative to the settings file's folder.
- `Edit(./.curate/**)`: writes under the `.curate/` folder of whichever project you're in (`./`
  is the session's folder). Covers Write too. Nothing outside `.curate/` is pre-approved.

Without them, answer the prompts ("Yes, and don't ask again" stores a rule in that project's
`.claude/settings.local.json`). To limit the rules to one project, put the same block in that
project's `.claude/settings.local.json` instead.

## How these were checked

With exactly these three rules (passed via `claude --settings <file>`, no other permissions), a
one-function workflow went from sketch to Gate 3 with no permission prompts, including the
`curate-mga` subagent's writes and its stub-test runs (2026-11-03). Putting them in
`~/.claude/settings.json` itself was not tested; Claude Code applies user settings to every
project.

Caveats:
- A prefix rule such as `Bash(uv run --quiet …/scripts/*)` also matches a path that leaves that
  folder (`…/scripts/../other.py`). The skills' own permissions have the same limit.
- A command Claude writes in another form (e.g. `R=… && uv run …`) doesn't match the rule and is
  asked about (or denied in a headless run); Claude then usually retries in the plain form.

## Auto mode

Auto mode keeps narrow allow rules like these and drops broad ones (`Bash(*)`, `Bash(uv run *)`).
Checked 2026-11-04 with a headless auto-mode session and a placeholder `deploy.py`: without a rule,
an approved publish command was blocked ("Create Public Surface", 2 of 2 runs); with
`Bash(uv run --quiet …/scripts/deploy.py *)`, the same rule written with `:*`, or the `scripts/*`
rule above, it ran (4 of 4). `/curate-skills:curate-doctor --deploy` warns when no rule covers
`deploy.py` and `monitor.py`.
