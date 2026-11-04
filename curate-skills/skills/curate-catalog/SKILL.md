---
name: curate-catalog
description: Inspect and manage the CURATE module catalog — where it is, list, search, initialise, import and export entries — e.g. to start an experiment from an empty or seeded catalog.
argument-hint: "where | list [--workflows] | search <query> | init | import <path>... | export <dir> [ids...]"
disable-model-invocation: true
allowed-tools: Read, Bash(uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/catalog.py *)
---

# curate-catalog

Thin wrapper over `uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/catalog.py`. Every command has
this shape — **the subcommand first**, then its own arguments, then the settings flag (the script
rejects flags placed before the subcommand):

`uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/catalog.py <subcommand> [its arguments] --config-catalog-dir '${user_config.catalog_dir}'`

For example: `uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/catalog.py where --config-catalog-dir '${user_config.catalog_dir}'`.
Keep `--config-catalog-dir` in single quotes exactly as written, so the plugin setting is honoured —
if the setting was not filled in, the literal text must reach the script, which treats it as unset.

Arguments: `$ARGUMENTS`

| User asks | Run |
|---|---|
| `where` (or nothing) | `catalog.py where` — folder, where the setting came from, entry counts |
| `list` / `list --workflows` | `catalog.py list [--workflows]` — show a compact table (name, rank, deps, usage, provenance) |
| `search <query>` | `catalog.py search --query "<query>" --k 5` — show score and the `line` field |
| `init` | `catalog.py init` |
| `import <path>...` | `catalog.py import <path>... --dedupe-code` — accepts a catalog folder, a single entry file, or the original CURATE prototype's `catalog/data/` folder |
| `export <dir> [ids]` | `catalog.py export --out <dir> [--ids ...]` |

Add `--catalog-dir <dir>` (also after the subcommand) when the user names a folder. Report what changed; never delete
entries (there is no delete command — tell the user to remove files under
`<catalog>/functions/` by hand if they want to).

## Which catalog is used

First match wins: `--catalog-dir` › the run's pinned folder › `CURATE_CATALOG_DIR` › the
plugin's `catalog_dir` setting › `~/.curate/catalog`. A /curate run pins its folder when it
starts, so changing these later does not affect a run in progress.

## Experiments

- Empty catalog: launch with `CURATE_CATALOG_DIR=$(mktemp -d)`, or `/curate --catalog <empty dir> …`.
- Seeded catalog: `/curate-skills:curate-catalog import <seed folder> ` into a fresh folder, then
  run /curate with `--catalog <that folder>`. The E2 MapReduce seed lives in the plugin at
  `tests/fixtures/catalog-e2-mapreduce/`.
- BM25 note (inherited from the original): a word that occurs in every catalog entry scores
  zero, so a one-entry catalog never matches. Seeds with several entries behave normally.
