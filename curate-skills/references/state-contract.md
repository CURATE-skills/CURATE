# Run state and work files

A run lives in `<project>/.curate/runs/<run_id>/`. Only the scripts write `state.json`.

## state.json (schema_version 2; v1 runs are refused)

| Field | Meaning |
|---|---|
| `run_id`, `run_dir`, `project_dir`, `created` | identity |
| `catalog_dir`, `mga_max_retries`, `stubtest_mode` | settings pinned at `init` |
| `user_request`, `user_models`, `context_files` | the sketch and attachments |
| `stage`, `iteration`, `max_iterations` | where the run is (`skeleton`, `gate1`, `reuse`, `resolve`, `gate2`, `generate`, `gate3`, `emit`, `gate4`, `deploy`, `monitor`, `finalize`, `done`, `stopped`) and how many revision rounds were used |
| `begun`, `stopped_from` | which stage was started (for resume); where a stopped run stopped |
| `bundle_hash`, `deploy_choice` | sha256 of the bundle folder (OS metadata ignored) and `yes`/`no`, both recorded when Gate 4 is approved; curate-deploy refuses a bundle whose hash changed. A run started with `--start-at deploy` carries the source's approval over (history `copied` at gate 4, with `from_run`, `snapshot`, `bundle_hash`; it counts only while the bundle matches that hash), and its `deploy_choice` stays empty until the user answers Yes to "Start this run?" (`spec.py confirm-start`, history `deploy-confirmed`) |
| `deploy` | written by `deploy.py`: `deploy_name`, `suffix`, `repo`, `branch`, `bundle_hash`, `steps.{publish,upload,register,invoke}` (`ok`, `ts`, `error`), and after invoke `invocation_id`, `invocation_timestamp`, `dispatched_at`, `workflow_name`. Every path a step wrote is also in `<run>/deploy/deploy-log.jsonl` |
| `monitor` | written by `monitor.py` for a run at stage `monitor` only: `invocation_id`, `settings` (`minutes`, `interval`, pinned), `seconds_used`, `polls`, `matches` (each Actions run's classification — `confirmed`, `time_only`, `other` — cached so its job log is fetched once), `result` (`success`, `success_unconfirmed`, `failure`, `timeout`, `pending`), `accepted` (the user accepted an unconfirmed match at Gate 5), `failed`, `snapshot`, `summary`, `logs_dir` (`<run>/deploy/monitor/`, FaaSr's S3 logs, redacted; job logs are never saved). Finalize after a deploy needs `success`, or `success_unconfirmed` (catalog saves also need `accepted`) |
| `revision_directives`, `feedback`, `code_feedback`, `structural_change` | change requests; directives persist across rounds |
| `skeleton_draft` | Gate 1 draft: `spec`, `last_good`, `parse_error`, `refinement` |
| `spec`, `prior_spec`, `spec_is_seed` | approved WorkflowSpec; prior code-bearing spec for `[cached]` |
| `enriched_spec`, `candidates`, `repairs` | MCA results and automatic repairs |
| `mga`, `validation`, `bundle_dir`, `history` | generation, emit and decision history |
| `enriched_basis`, `spec_basis`, `mga[n].basis`, `validation.spec_hash` | input hashes: what each result was built from, so a later stage can tell it is out of date |

## Files you write in `work/`

`skeleton.json`
: `{name, description, nodes: [{name, description, rank}], edges: [{from_node, to_node, condition?}]}`.
  Node names are Python identifiers, unique.

`candidates.json`
: array, one object per skeleton node: `{node, decision: cached|reuse|adapt|new, adapt_from,
  catalog_id, adaptation_notes, refined_description, updated_inputs, updated_outputs,
  suggested_dependencies, required_secrets, user_model_mode?}`.

`resolved.json`
: a full WorkflowSpec: `{name, description, entry, folder, nodes: [{name, description, rank,
  inputs, outputs, dependencies}], edges, data_flow: [{from_node, to_node, file, format, folder}]}`.
  Node names must equal the candidate names exactly.

Inputs/outputs are `{name, type, description}`; names are basenames; `{rank}` is the only
placeholder.
