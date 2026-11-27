# Session and run state, and work files

Two kinds of state, two words:

- A **session** is one `/curate` session, from the sketch to Gate 5:
  `<project>/.curate/sessions/<session-id>/` (its `state.json`, `draft/`, `history/`, `review/`,
  `work/`). Session commands (`spec.py`, `scratch.py`, `emit.py`, `stubtest.py`, `catalog.py`)
  take `--session <id or folder>`; `--run` is the old name, still accepted.
- A **run** is one execution of a published version:
  `<project>/workflows/<workflow-id>/runs/<run-id>/`, named `<workflow-id>/<run-id>`. Run commands
  (`deploy.py`, `pegasus_deploy.py`, `monitor.py`) take `--run <workflow-id>/<run-id>` (a run id or
  its folder too); given a session id they use its current run and say so. A bare id that names
  both a session and a run is refused: use `--session <id>` or `--run <workflow-id>/<run-id>`.

IDs: `YYYYMMDD-HHMMSS-<4 hex>` for both (older sessions: `YYYYMMDD-HHMMSS-<words>`); a version is
`<name>-v<N>`. Only the scripts write `state.json`.

```
<project>/
  workflows/                       CURATE's (marked by .curate-workflows; an unmarked folder is refused)
    .gitignore                     written once: keeps versions and each run's outputs/ (not outputs/_all/) and monitor-report.md
    <workflow-id>/                 a published version, never edited: workflow.yml, inputs.yml, functions/
      approved.json                what Gate 4 approved (the spec); outside the version hash, checked on read
      runs/<run-id>/               a run: state.json, manifest.json, bundle/, deploy/, deploy-log.jsonl,
                                   logs/, monitor-report.md, outputs/ (its results folder)
  .curate/
    sessions/<session-id>/         a /curate session
    project-id                     a random id a FaaSr deployment's marker records (never a path)
```

Projects made before this layout keep versions in `.curate/workflows/` and sessions in
`.curate/runs/`; both are still read (a version there gets its new runs in its own `runs/`), and
nothing is moved.

**A session and its run.** When Gate 4 (or "Start this run?") opens a run, the run gets its own
`state.json` (`kind: "execution"`, `session: {session_id, dir}`); the execution records `deploy`,
`pegasus` and `monitor` live only there. Reading the session merges them in, so the session's
state shows its current run's progress; writing splits them back (both are locked, session first).
A session made before runs had their own state keeps everything in its own file (and its
execution files in `<session>/deploy/` and `<session>/pegasus/`). A run started with
`--start-at deploy --workflow <wid>` has no session (`session: null`): it carries its own `stage`,
`reference`, `spec` (from the version's `approved.json`), `spec_from`, `from_run`, and a history that starts with
a Gate 4 `copied` record; it ends at Gate 5 with nothing to save.

## state.json (schema_version 2; v1 runs are refused)

| Field | Meaning |
|---|---|
| `run_id`, `run_dir`, `project_dir`, `created` | identity (a session's id and folder; a run's own state has `kind` `execution` and `session`) |
| `catalog_dir`, `mga_max_retries`, `stubtest_mode` | settings pinned at `init` (`stubtest_mode` is `strict` unless a developer sets `CURATE_STUBTEST_MODE=parity`; not a user setting) |
| `user_request`, `user_models`, `context_files` | the sketch and attachments |
| `stage`, `iteration`, `max_iterations` | where the run is (`skeleton`, `gate1`, `reuse`, `resolve`, `gate2`, `generate`, `gate3`, `emit`, `gate4`, `deploy`, `monitor`, `finalize`, `done`, `stopped`) and how many revision rounds were used |
| `begun`, `stopped_from` | which stage was started (for resume); where a stopped run stopped |
| `workflow_name`, `workflow_lineage` | the workflow's name (its ID's `<name>`), asked at the sketch and recorded by `spec.py set-name` (checked against the global catalog and this project); the lineage that makes a taken name a new version of it: `{new_version_of}` (the user confirmed), `{version_of}` (a reused catalog workflow), or `{from_run, workflow_id}` (a run started with `--from-run`, which inherits the name). A lineage counts only for the name it is about |
| `draft_dir`, `draft_problems`, `draft_spec_hash` | written by emit: the workflow draft `<session>/draft/` (`workflow.yml`, `inputs.yml`, `functions/`), why the spec could not become one, and the hash of the spec it was built from (Gate 4 refuses a draft older than the spec) |
| `workflow_id`, `workflow_version`, `draft_hash`, `version_dir`, `version_hash`, `version_reused`, `workflow_content_hash` | written when Gate 4 is approved: the version it published (`<name>-v<N>`, its number), the approved draft's tree hash, the version folder and its own hash (deploy refuses a version that changed), whether an identical existing version was used instead (`--on-duplicate drop`), and the content hash (it only detects duplicates). Cleared, like everything Gate 4 recorded, when the run goes back before Gate 4; the published folder stays |
| `execution` | the execution run Gate 4 (or "Start this run?") opened for the chosen target: `run_id`, `reference` (`<workflow-id>/<run-id>`), `dir`, `target` (`faasr` / `pegasus`), `bundle_dir`, and once its bundle is built `bundle_hash` and `curate_version` (from its `manifest.json`). FaaSr builds the bundle at approval; Pegasus at prepare |
| `deploy_choice` | `yes` (FaaSr) / `pegasus` / `no`, recorded when Gate 4 is approved. The bundle the execution builds is checked at deploy by its manifest. A run started with `--start-at deploy` carries the source's approval over (history `copied` at gate 4, with `from_run` and `snapshot`; the source's version must be unchanged, and a source approved before workflow versions is refused at deploy and finalize — start it at emit instead), and its `deploy_choice` stays empty until the user answers Yes to "Start this run?" (`spec.py confirm-start`, history `deploy-confirmed`; `--deploy pegasus` for "Yes, run on Pegasus") |
| `deploy` | written by `deploy.py` (in the run's state): `scheme` `version` (one deployment per version), `deploy_name` (the version's workflow id + `CURATE_DEPLOY_NAME_SUFFIX`), `suffix`, `repo`, `branch`, `payload` (`workflows/<deploy_name>/<run-id>.json` in the action repo), `deployment` (`made`: this run wrote the version's deployment; `reused`: it was already there, byte-identical), `marker` (the deployment marker's `projects` and `deployed_at` as the publish or register step last saw or wrote it, with `seen`; `deploy.py list` shows them without reading the repo), `bundle_hash`, `steps.{publish,upload,register,invoke}` (`ok`, `ts`, `error`; `reused` for publish/register that wrote nothing), and after invoke `invocation_id`, `invocation_timestamp`, `dispatched_at`, `workflow_name`; after a teardown `teardown` (this run's payload and S3 files) and, after `--workflow <wid> --all`, `deployment_teardown`. Every path a step wrote is in the run's `deploy-log.jsonl`. Runs deployed before one deployment per version have no `scheme` (a per-run `deploy_name` such as `<workflow>-<MMDD-HHMM>`, maybe `reused_from`): readable and can be torn down, not continued |
| `pegasus` | only for `deploy_choice` `pegasus` (absent in FaaSr and no-deploy runs; cleared on rewinds). Set at Gate 4 / confirm-start: empty, or `{image_choice, image_source}` when the user chose a different image for this run only (`--image`; checked by `images.check_image`; the settings file is never changed). Then written by `pegasus_deploy.py` — `name` (the version's workflow id), `host` (null = this machine), `runtime`, `image` and `image_source` (pinned by prepare: the run's own image, else `CURATE_PEGASUS_IMAGE`, else the built-in default; every step's image in the workflow's `curate.transformations` metadata), `workflow_dir` (the Pegasus workflow folder, the run's `bundle/`; runs prepared before step 5 recorded it as `ir_dir`, still read), `jobs`, `inputs_from`, `remote_dir` (after copy: `~/curate-runs/<workflow-id>/<run-id>/`), `steps.{prepare,copy,launch}` (`ok`, `ts`, `error`), and after launch `work_dir`, `run_dir` (the Pegasus run folder on the host, so the monitor finds it after a logout or reboot), `submitted_at`, `dagman_cluster`. Every path a step wrote is in the run's `deploy-log.jsonl`. Monitor needs `steps.launch.ok`; finalize and catalog saves need `monitor.result` `success` |
| `monitor` | written by `monitor.py` for a run at stage `monitor` only: `invocation_id`, `settings` (`minutes`, `interval`, pinned), `seconds_used`, `polls`, `matches` (each Actions run's classification — `confirmed`, `time_only`, `other` — cached so its job log is fetched once), `result` (`success`, `success_unconfirmed`, `failure`, `timeout`, `pending`), `finished`, `accepted` (the user accepted an unconfirmed match at Gate 5), `failed`, `snapshot`, `summary`, `logs_dir` (the run's `logs/`: FaaSr's S3 logs, redacted; job logs are never saved). Finalize after a deploy needs `success`, or `success_unconfirmed` (catalog saves also need `accepted`). A FaaSr run's outputs stay in S3 until `monitor.py download-outputs --run <run>` copies the deliverables into the run's `outputs/` at their `curate:deliver_as` paths, as Pegasus delivers them (e.g. `results/word_count_1.json`), with `manifest.json`; `--all` also copies every object the run wrote into `outputs/_all/` (without the `<deploy name>/<run-id>/` prefix; ignored by `workflows/.gitignore`); a bundle without deliverables (older) saves every object under its S3 name; a later download fills it in and never overwrites. For a Pegasus run (`pegasus_monitor.py`): `target` `pegasus`, `run_dir`, `settings`, `seconds_used`, `polls`, `result` (`success`, `failure`, `timeout`, `pending`), `jobs` (each job copy's state), `failed` (steps), `dagman_exit`, `summary`, `shown`, `report` (the run's `monitor-report.md`), and once collected `deliver_dir`, `run_report`, `delivered`, `missing`, `output_dir` (the run's `outputs/`: the delivered files, `run-report.json`, `manifest.json`, and `FAILED.txt` while the run has failed; a re-check fills it in), `output_skipped` (hand-edited files a re-check left alone) |
| `revision_directives`, `feedback`, `code_feedback`, `structural_change` | change requests; directives persist across rounds |
| `skeleton_draft` | Gate 1 draft: `spec`, `last_good`, `parse_error`, `refinement` |
| `spec`, `prior_spec`, `spec_is_seed` | approved WorkflowSpec; prior code-bearing spec for `[cached]` |
| `enriched_spec`, `candidates`, `repairs` | MCA results and automatic repairs |
| `mga`, `validation`, `history` | generation, emit and decision history. `validation` comes from the draft for a versioned run (`source` `draft`: the CWL validator, then each target's builder in memory, `faasr` and `pegasus`, each `{ok, error, problems}`; a refusing target does not fail validation, and Gate 4's "where it can run" uses these results). Runs from before phase 8 may still hold `bundle_dir` / `bundle_hash` (the old emit's bundle); nothing reads them, and a new stage entry clears them |
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
