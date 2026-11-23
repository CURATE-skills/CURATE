# Run state and work files

A generation run lives in `<project>/.curate/runs/<run_id>/` (run IDs: `YYYYMMDD-HHMMSS-<4 hex>`;
older runs: `YYYYMMDD-HHMMSS-<words>`). Only the scripts write `state.json`. A workflow version Gate 4
published lives in `<project>/.curate/workflows/<name>-v<N>/`, and each execution of it in
`<project>/.curate/workflows/<name>-v<N>/runs/<run_id>/` (docs/CWL_IR.md, "Layout").

## state.json (schema_version 2; v1 runs are refused)

| Field | Meaning |
|---|---|
| `run_id`, `run_dir`, `project_dir`, `created` | identity |
| `catalog_dir`, `mga_max_retries`, `stubtest_mode` | settings pinned at `init` |
| `user_request`, `user_models`, `context_files` | the sketch and attachments |
| `stage`, `iteration`, `max_iterations` | where the run is (`skeleton`, `gate1`, `reuse`, `resolve`, `gate2`, `generate`, `gate3`, `emit`, `gate4`, `deploy`, `monitor`, `finalize`, `done`, `stopped`) and how many revision rounds were used |
| `begun`, `stopped_from` | which stage was started (for resume); where a stopped run stopped |
| `workflow_name`, `workflow_lineage` | the workflow's name (its ID's `<name>`), asked at the sketch and recorded by `spec.py set-name` (checked against the global catalog and this project); the lineage that makes a taken name a new version of it: `{new_version_of}` (the user confirmed), `{version_of}` (a reused catalog workflow), or `{from_run, workflow_id}` (a run started with `--from-run`, which inherits the name). A lineage counts only for the name it is about |
| `draft_dir`, `draft_problems`, `draft_spec_hash` | written by emit: the workflow draft `<run>/draft/` (`workflow.yml`, `inputs.yml`, `functions/`), why the spec could not become one, and the hash of the spec it was built from (Gate 4 refuses a draft older than the spec) |
| `workflow_id`, `workflow_version`, `draft_hash`, `version_dir`, `version_hash`, `version_reused`, `workflow_content_hash` | written when Gate 4 is approved: the version it published (`<name>-v<N>`, its number), the approved draft's tree hash, the version folder and its own hash (deploy refuses a version that changed), whether an identical existing version was used instead (`--on-duplicate drop`), and the content hash (it only detects duplicates). Cleared, like everything Gate 4 recorded, when the run goes back before Gate 4; the published folder stays |
| `execution` | the execution run Gate 4 (or "Start this run?") opened for the chosen target: `run_id`, `reference` (`<workflow-id>/<run-id>`), `dir`, `target` (`faasr` / `pegasus`), `bundle_dir`, and once its bundle is built `bundle_hash` and `curate_version` (from its `manifest.json`). FaaSr builds the bundle at approval; Pegasus at prepare |
| `bundle_hash`, `deploy_choice` | `yes` (FaaSr) / `pegasus` / `no`, recorded when Gate 4 is approved. For a run approved before workflow versions, also the sha256 of emit's bundle folder (OS metadata ignored); curate-deploy refuses such a bundle whose hash changed. A versioned run records no `bundle_hash`: its bundle is checked by the manifest. A run started with `--start-at deploy` carries the source's approval over (history `copied` at gate 4, with `from_run`, `snapshot`, and for a source approved before versions `bundle_hash`, counting only while the bundle matches it; a versioned source's version must be unchanged), and its `deploy_choice` stays empty until the user answers Yes to "Start this run?" (`spec.py confirm-start`, history `deploy-confirmed`; `--deploy pegasus` for "Yes, run on Pegasus") |
| `deploy` | written by `deploy.py`: `deploy_name`, `suffix`, `repo`, `branch`, `bundle_hash`, `steps.{publish,upload,register,invoke}` (`ok`, `ts`, `error`), and after invoke `invocation_id`, `invocation_timestamp`, `dispatched_at`, `workflow_name`. Every path a step wrote is also in `<run>/deploy/deploy-log.jsonl` |
| `pegasus` | only for `deploy_choice` `pegasus` (absent in FaaSr and no-deploy runs; cleared on rewinds). Set at Gate 4 / confirm-start: empty, or `{image_choice, image_source}` when the user chose a different image for this run only (`--image`; checked by `images.check_image`; the settings file is never changed). Then written by `pegasus_deploy.py` — `name`, `host` (null = this machine), `runtime`, `image` and `image_source` (pinned by prepare: the run's own image, else `CURATE_PEGASUS_IMAGE`, else the built-in default; also `ir.yaml` `defaults.image`), `ir_dir`, `jobs`, `inputs_from`, `remote_dir` (after copy), `steps.{prepare,copy,launch}` (`ok`, `ts`, `error`), and after launch `work_dir`, `run_dir` (the Pegasus run folder on the host, so the monitor finds it after a logout or reboot), `submitted_at`, `dagman_cluster`. Every path a step wrote is also in `<run>/pegasus/deploy-log.jsonl`. Monitor needs `steps.launch.ok`; finalize and catalog saves need `monitor.result` `success` |
| `monitor` | written by `monitor.py` for a run at stage `monitor` only: `invocation_id`, `settings` (`minutes`, `interval`, pinned), `seconds_used`, `polls`, `matches` (each Actions run's classification — `confirmed`, `time_only`, `other` — cached so its job log is fetched once), `result` (`success`, `success_unconfirmed`, `failure`, `timeout`, `pending`), `accepted` (the user accepted an unconfirmed match at Gate 5), `failed`, `snapshot`, `summary`, `logs_dir` (`<run>/deploy/monitor/`, FaaSr's S3 logs, redacted; job logs are never saved). Finalize after a deploy needs `success`, or `success_unconfirmed` (catalog saves also need `accepted`). For a Pegasus run (`pegasus_monitor.py`): `target` `pegasus`, `run_dir`, `settings`, `seconds_used`, `polls`, `result` (`success`, `failure`, `timeout`, `pending`), `jobs` (each job copy's state), `failed` (steps), `dagman_exit`, `summary`, `shown`, `report` (`<run>/pegasus/monitor-report.md`), and once collected `deliver_dir`, `run_report`, `delivered`, `missing`, `output_dir` (`<project>/pegasus_output/<workflow>-<run_id>/`: the delivered files and run-report.json, plus `FAILED.txt` when the run failed; copied once per launch) |
| `revision_directives`, `feedback`, `code_feedback`, `structural_change` | change requests; directives persist across rounds |
| `skeleton_draft` | Gate 1 draft: `spec`, `last_good`, `parse_error`, `refinement` |
| `spec`, `prior_spec`, `spec_is_seed` | approved WorkflowSpec; prior code-bearing spec for `[cached]` |
| `enriched_spec`, `candidates`, `repairs` | MCA results and automatic repairs |
| `mga`, `validation`, `bundle_dir`, `history` | generation, emit and decision history. `validation` comes from the draft for a versioned run (`source` `draft`: the CWL validator, then `faasr` = the FaaSr workflow built from it in memory); `bundle_dir` is emit's older FaaSr bundle, which only runs approved before versions deploy |
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
