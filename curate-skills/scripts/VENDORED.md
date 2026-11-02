# Vendored and adapted code

Source: https://github.com/nolcut/CURATE-prototype
Commit: `3fd5c16092ddf97a28bc3b83088e23e7f1b0f140`
License: MIT, Copyright (c) 2026 Nolan Cutler (full text in `../NOTICE`).
Paths below are relative to `src/faasr_agents/` in that repo.

Every vendored script starts with the MIT notice and a `Derived from` line.
Status: `planned` until the file exists; then `vendored`.

## Vendored scripts

| Script | Source file(s) | Lines | Changes | Status |
|---|---|---|---|---|
| `curate_models.py` | `models.py` | 8-67, 96-135, 157-163 | Drop `UsageRecord`; reduce `UserModel`/`ContextFile` to `{name, path, description}`; v2: `origin_kind` also accepts `"import"` | vendored |
| `emit.py` | `faasr/emit.py`; `agents/wda.py` | 1-218; 15-53 | CLI wrapper; unset `FAASR_*` deployment vars emitted as `<FAASR_NAME>` placeholders (original: `username`, `faasr-functions`, `faasr-data`, AWS defaults); bundle writer adds `spec.json`, `external_inputs.json`, `README.md`, `.deps.txt` and refuses to write if a known secret value appears; structural fallback validator | vendored |
| `faasr_validate.py` | `faasr/validate.py` | 1-45 | Standalone PEP 723 script with its own `FaaSr_py==0.1.13` env (which pins `boto3==1.37.11`; the original's boto3 override was only for Bedrock); JSON in, JSON out | vendored |
| `catalog.py` | `catalog/store.py`; `catalog/workflow_store.py`; `orchestrator.py` | 20-84; 1-63; 173-226 | Configurable root (`functions/`, `workflows/` subdirs); CLI; `--only-verified`; import/export | vendored |
| `faasr_stubs.py` | `faasr/context_dir.py` (`_STUBS_CODE`) | 19-69 | Per-node layout: own `test_output` → own `test_data` → `$CURATE_UPSTREAM_DIRS`; warning on stderr when a file is missing (still created empty, as in the original) | vendored |
| `stubtest.py` | `agents/fga.py` (`_run_stub_test`); `faasr/context_dir.py` (`signature_and_test_call`) | 239-286; 234-248 | Two modes: `strict` (default: every rank, isolated `uv run --with-requirements` env, faasr-import and declared-output checks, `test_output/` cleared first) and `parity` (original: rank 1 only, no dep install, `ModuleNotFoundError` with declared deps passes); deployment/provider credentials removed from the env; writes `stubtest.log` | vendored |

## Vendored deploy and runtime code (`scripts/vendor/`, D0; used from D1 on)

Copied whole so they stay diffable against the original; `tests/test_deploy.py` checks that each file differs from its source only by the edits listed here. Each `.py` starts with a 3-line MIT/`Vendored from` header. Their packages (`pygithub`, `FaaSr_py==0.1.13`, `boto3`, `requests`) are declared by the scripts that import them, not by the test env. Platforms other than GitHub Actions stay in `register_workflow.py` but are unreachable, as in the original (emit always writes `GitHubActions`).

| Vendored file | Source file | Lines | Edits | Status |
|---|---|---|---|---|
| `vendor/curate_deploy/register_workflow.py` | `deploy/register_workflow.py` | 1-1022 | import `validate_faasr_json` from `faasr_validate` (the plugin's vendored `faasr/validate.py`); import-time `logging.basicConfig(..., stream=sys.stderr)` instead of stdout, so the scripts' stdout stays JSON | vendored |
| `vendor/curate_deploy/invoke_workflow.py` | `deploy/invoke_workflow.py` | 1-192 | logging to stderr (as above) | vendored |
| `vendor/curate_deploy/native_containers.txt` | `deploy/native_containers.txt` | 1-5 | none | vendored |
| `vendor/faasr_runtime/{enums,utils,s3_client,faasr_function,faasr_function_logger,workflow_runner}.py` | `faasr/runtime/*.py` | all (1,356) | `faasr_agents.faasr.runtime.` imports made relative; signal handlers and root-logger reset kept (monitor.py runs the runner in the main thread) | vendored |
| `vendor/faasr_runtime/artifacts.py` | `faasr/artifacts.py` | 1-91 | relative import of `s3_client` | vendored |

Not used: `tui/deploy_tracker.py` and `tui/output_review.py` (Rich live views and the Gate 5 REPL), replaced by stage files and AskUserQuestion.

## Ported and new code

`spec.py`, `scratch.py` and the hook are not wholesale copies, but parts of the first two are adapted copies of the original (MIT notice in their headers). Relation: **copy** = same logic, edited only for I/O (logging instead of `print`, plain dicts or names instead of LangGraph state); **adapted** = original logic with listed changes; **follows** = reimplemented to reproduce the original's behaviour; **new** = no counterpart.

### `scripts/spec.py` — run state, gate transitions and spec operations

Replaces the LangGraph graph, `AgentState` and the gate nodes. Claude now writes the JSON the original's LLM calls returned (skeleton, candidates, resolved spec); this script validates it and applies the original's deterministic post-processing.

| Part | Purpose | Original | Relation |
|---|---|---|---|
| `load_state`, `save_state`, `snapshot_history`, `cmd_init`, `cmd_runs`, `cmd_status` | Run directory and `state.json` (replaces the in-memory checkpointer); history copies; settings pinned per run | `state.py` 9-63 (`AgentState` fields); `cli.py` 820-840 (initial state) | new |
| `resolve_setting`, `_retries`, `_mode` | `mga_retries` / `stubtest_mode`: flag > env > plugin setting > default | — | new |
| `accumulated_feedback` | Standing change requests as bullets, plus Gate-3 code feedback | `agents/wca.py` 28-38 (`_accumulated_feedback`); 621-626 (code-feedback merge in `wca_fca_node`) | copy |
| `skeleton_outline`, `user_models_section`, `context_files_section` | Prompt sections | `agents/wca.py` 118-163 | copy |
| `candidates_text` | Candidate list for the resolve prompt | `agents/wca.py` 311-320 (`_fca_candidates_text`) | copy |
| `cmd_prompt_context` | Hands Claude the exact inputs of each prompt: skeleton, per-node MCA (query, BM25 top-5, prior implementation, user model), resolve | `agents/wca.py` 164-219 (`wca_compose_skeleton_node`), 611-735 (`wca_fca_node`); `agents/fca.py` 137-198 (query, catalog text, prior-impl section) | follows |
| `parse_workflow_spec` | JSON → `WorkflowSpec`; skeleton mode strips I/O; `data_store` forced to `S3` | `agents/wca.py` 72-115 (`_parse_workflow_spec`) | adapted (adds identifier, duplicate and unknown-edge errors) |
| `ensure_reachable`, `prune_transitive_edges` | Single entry, every node reachable; transitive reduction that never drops conditional edges | `agents/wca.py` 448-546, 549-610 | copy |
| `topo_order` | Kahn order for generation and display | `faasr/context_dir.py` 76-110 (`bfs_topological_sort`) | copy (returns names) |
| `ancestors` | Upstream nodes of a node | — | new |
| `cmd_skeleton` | Parse a skeleton; keep the last good draft on a bad revision | `agents/wca.py` 220-245 | follows |
| `_coerce_iospecs`, `_uniq` | LLM I/O coercion; secret-name dedupe | `agents/fca.py` 26-56 | copy |
| `decide_node` | Map one MCA decision (`cached`/`reuse`/`adapt`/`new`, user model) onto a node | `agents/fca.py` 120-135, 199-276 (`propose_candidate`) | adapted (no LLM call; usage not incremented here; unknown `catalog_id` → `new` with a note) |
| `apply_renames` | Verbatim catalog reuse takes the catalog name; edges follow; collisions downgrade to `adapt` | `agents/wca.py` 378-445 (`_apply_candidate_renames`) | copy |
| `merge_resolved` | Restore catalog/cached code and I/O; keep seeds for adapt/user nodes | `agents/wca.py` 323-375 (`_merge_resolved`) | copy (no JSON extraction) |
| `check_spec`, `cmd_check` | Filename/data-flow consistency, `{rank}` rules, single entry, cycles | — | new |
| `cmd_approve`, `cmd_directive` | Gate decisions and revision routing (structural → skeleton, functional → resolve), `prior_spec` / `spec_is_seed` rules, iteration cap | `agents/wca.py` 257-310 (Gate 1), 753-830 (Gate 2); `orchestrator.py` 17-108 (Gate 3), 109-170 (routing); `agents/wda.py` 196-262 (Gate 4) | follows (classification is done by Claude with `classify.md`) |
| `dag_lines`, `show_gate`, `cmd_show` | Markdown for each gate | `cli.py` 403-462 (`_display_interrupt`); `tui/` | new |

### `scripts/scratch.py` — per-node scratch folders, MGA lock, write checks, harvest

Replaces the FGA's single temporary context directory with one folder per node, so each `curate-mga` subagent can be limited to its own folder.

| Part | Purpose | Original | Relation |
|---|---|---|---|
| `_seed_real_input`, `seed_test_data` | Copy user files matching a node's inputs into its `test_data/`; never fabricate | `faasr/context_dir.py` 184-231 | adapted (also matches concrete shards of `{rank}` inputs) |
| `signature_and_test_call` | Signature and stub test call | `faasr/context_dir.py` 234-248 | copy |
| `shared_context_md`, `node_spec_md` | `CONTEXT.md` and per-node `spec.md` text | `faasr/context_dir.py` 251-357, 360-569 | adapted (per-node paths, write-scope section, `stubtest.py` command, `deps.txt` instead of `pip install`, parity report beside the code) |
| `cmd_build` | Rebuild every node folder from the spec; seed reused code; set MGA statuses | `faasr/context_dir.py` 117-177 (`setup_context_directory`); `agents/fga.py` 1007-1032 (fresh dir per generation) | follows |
| `cmd_lock`, `cmd_unlock` | Project-level lock naming the one folder the running MGA may write | — | new |
| `manifest`, `cmd_snapshot`, `cmd_verify_writes` | Before/after manifests (run dir and catalog by hash, project by size and mtime) to catch writes outside the node folder | — | new |
| `extract_secrets`, `read_reported_deps` | Secrets from `faasr_secret("X")`; deps from `<node>.deps.txt` | `agents/fga.py` 957-981 | copy |
| `cmd_harvest` | Put generated code, secrets and deps back into the spec | `agents/fga.py` 927-947 | follows |
| `cmd_record` | Record MGA status, attempts and the stub-test tail | — | new |

### `scripts/deploy.py` — curate-deploy (preflight, and from D1 the four steps)

| Part | Purpose | Original | Relation |
|---|---|---|---|
| `get_token` | `GH_PAT` (CURATE's variable), else `gh auth token` read inside the script; never printed or written | `agents/wda.py` 59-62 (`GH_PAT` only) | adapted |
| `required_secret_names` | GitHub secret names the generated Actions YAML imports | `deploy/register_workflow.py` 88-172 | follows (GitHub Actions only; a test compares with the vendored functions) |
| `deploy_copy`, `name_problems` | The workflow renamed `<Name>-cs` with `FunctionGitRepo` → `functions/<Name>-cs/<fn>.py`, and every path a deploy writes | `agents/wda.py` 56-132 (paths `functions/<fn>.py`, `workflows/<Name>.json`); `register_workflow.py` 300-345 (`.github/workflows/<Name>-<action>.yml`) | adapted (suffix and per-workflow folder) |
| `fill_placeholders` | Fills emit's `<FAASR_…>` placeholders (user, repo, S3 endpoint/bucket/region) from the environment, in the deploy copy only | `faasr/emit.py` 126-127 (CURATE reads the same variables at emit time) | new |
| `s3_check`, `boto3_s3` | Read-only S3: `head_bucket`, and one-key `list_objects_v2` on each prefix the run would write and on CURATE's own prefixes | client built as `faasr/runtime/s3_client.py` 64-83 | new |
| `field_diff`, `deploy_diff`, `derived_changes` | Field-by-field diff of the deploy copy against the approved bundle, each with its reason; refuses any change outside `ALLOWED_DIFF` | — | new |
| `read_deploy_log`, `own_paths`, `--overwrite-own-deploy` | Retrying a partial deploy: existing paths allowed only if this run's `deploy/deploy-log.jsonl` records them (same run, deploy name, repo/branch or bucket) and repo files still carry curate-skills' mark | — | new |
| `put_file`, publish/upload in `deploy_step` | Create or update a file in the action repo (contents API), with the commit message `Add/update <fn> (curate-skills)` / `Add/update workflow payload <Name> (curate-skills)` | `agents/wda.py` 56-132 (`_upload_functions_to_github`, `_upload_workflow_json_to_github`: PyGithub `get_contents` + `update_file`/`create_file`, messages `… (faasr-agents)`) | ported (same API calls; urllib instead of PyGithub; path, branch and tag differ) |
| `run_register`, `run_invoke` | Call the vendored `register_workflow.main(workflow_data=…)` and `invoke_workflow.main(workflow_path=…)` on the deploy copy; their log lines are captured and redacted; only `InvocationID`/`InvocationTimestamp` are kept from the payload (FaaSr adds the token to it) | `agents/wda.py` 135-157 (`_register_and_invoke`) | ported (split into two confirmed steps) |
| `reuse`, `reuse_source`, `verify_identical`, `expected_actions_files` | Invoke only: for a run started from a run that deployed the same bundle, GET each deployed file and compare it byte for byte with what publish/upload/register would write (Actions files regenerated twice with the vendored generator; differing output refuses); then the normal invoke step | `deploy/register_workflow.py` 178-246 (`generate_serverless_yaml`/`generate_vm_yaml`, called as `deploy_to_github` does) | new |
| `deploy_step`, `deploy_status`, `STEPS` | The order publish → upload → register → invoke; each step needs `--confirmed`, re-runs the preflight (bundle hash required; earlier steps' paths must still be this deploy's), checks every file for leaks before the first push, appends each written path to the deploy log, records the step in `state.json` (under `state.lock`); a finished step is skipped | `agents/wda.py` 270-278 (the step list: push code, then register & invoke) | adapted |
| `preflight`, `_owner_of`, `preflight_summary` | Read-only: run/Gate 4/bundle checks, placeholders, native containers, env, token scopes, repo push + default branch, secret names, existing targets and their owner, CURATE's own files | `register_workflow.py` 49-85 (`verify_containers`); `cli.py` 368-382 (secret hints) | new |

### `scripts/monitor.py` — curate-monitor (D2, read-only)

| Part | Purpose | Original | Relation |
|---|---|---|---|
| `declared_outputs` | Every `output<N>` of every action under its `folder`, `{rank}` expanded to the producer's (or its ranked successor's) rank count | `faasr/artifacts.py` `build_artifact_list` (vendored, **used as-is**) | uses vendored |
| `units` | Each action rank in workflow order, named `action` / `action(r)` | ranks as FaaSr_py `helpers/graph_functions.build_adjacency_graph`; names as vendored `WorkflowRunner._iter_ranks` | ported (FaaSr_py is not importable in the test env) |
| `invocation_folder`, `log_key`, `done_key`, `FAILED_RX` | Where FaaSr writes a rank's log and `.done` marker; the `[ERROR]` line that fails it | FaaSr_py `get_invocation_folder`; vendored `FaaSrFunctionLogger.logs_key`, `FaaSrFunction.done_key`, `FaaSrFunction.failed_regex` | ported (same formats; the vendored classes import boto3 at load) |
| `S3Reader`, `s3_reader` | `head_object` / `get_object` only, on a client built as the vendored `s3_client` builds it (via `deploy.boto3_s3`), keys from `deploy.s3_keys` (either spelling) | `faasr/runtime/s3_client.py` 64-83, 150-200 | adapted (adds `LastModified` for the freshness check) |
| `list_runs`, `job_log_matches`, `_NoRedirect` | Actions runs of each `-cs` workflow file since the dispatch (GET); a run is this invocation's when its job log contains the InvocationID — searched in memory, the raw text never kept; the log redirect is followed without the token | — (CURATE has no Actions check; it watches S3 only) | new |
| `download_outputs` | Copy the declared outputs from S3 (get only) to an empty folder outside the run, with a manifest (size, LastModified, sha256); files holding a secret value are skipped | `tui/output_review._download_artifact` | adapted |
| `poll_once`, `poll`, `summary_md`, `accept_unconfirmed` | Snapshot per poll; bounded, resumable polling; success / success_unconfirmed / failure / timeout; state under `state.lock`; report-only for runs not at `monitor` | `agents/wda.py` 280-300 (`_build_runner`, `WorkflowRunner(timeout=600, check_interval=10)`) | new; the vendored `WorkflowRunner` (threads, inactivity timer, signal handlers) is **not used** |

### `scripts/leakcheck.py`

`find_leaks` / `redact`: secret env values by name (as `emit.find_leaked_secrets`), extra values such as a `gh auth token`, and token/key patterns (`ghp_`…, `github_pat_`…, `AKIA`/`ASIA`…, `aws_secret_access_key=`). New.

### `scripts/runstate.py` (v2)

Run lock (`fcntl.flock` on `<run>/state.lock`), atomic JSON writes, and the input hashes stages record (`spec_hash`, `node_basis`). New; no counterpart in the original.

### `hooks/mga_guard.py`

PreToolUse write-scope guard for `curate-mga` subagents; from D1 it also denies `curate-mga` any `deploy.py`/`monitor.py` call, and denies the main session Bash commands that would print a credential (`gh auth token`, `printenv`, a bare `env`/`set`/`export`, `echo $<credential>`, reading a `.env` file) — a heuristic. New; no counterpart in the original.

## Adapted prompt text

| Plugin file | Source |
|---|---|
| `references/prompts/wca-system.md` | `prompts/wca_prompts.py` 1-45 (`WCA_SYSTEM`) |
| `references/prompts/classify.md` | `prompts/wca_prompts.py` 47-66 (`WCA_CHANGE_CLASSIFY_PROMPT`) |
| `references/prompts/wca-skeleton.md` | `prompts/wca_prompts.py` 68-115 (`WCA_SKELETON_PROMPT`) |
| `references/prompts/wca-resolve.md` | `prompts/wca_prompts.py` 117-208 (`WCA_RESOLVE_PROMPT`) |
| `references/prompts/mca-candidates.md` | `prompts/fca_prompts.py` 1-108 (`FCA_SYSTEM`, `FCA_CANDIDATE_PROMPT`) |
| `references/prompts/mca-user-model.md` | `prompts/fca_prompts.py` 111-147 (`FCA_USER_MODEL_PROMPT`) |
| `references/prompts/mga-spawn.md` | `agents/fga.py` 700-763 (`_turn_prompt`) |
| `agents/curate-mga.md` | `agents/fga.py` 71-157 (`_SHARED_RULES`) |
| `skills/faasr-reference/SKILL.md` | `faasr/context_dir.py` 251-357 (`_shared_context_md`), 403-436 (rank rules) |
| `scratch.py` templates (`CONTEXT.md`, `spec.md`) | `faasr/context_dir.py` 251-569 |

## Deliberate behaviour changes

- Unset `FAASR_*` deployment variables become `<FAASR_NAME>` placeholders instead of guessed defaults; secret values are never written (bundle aborts if one is found).
- Stub-test mode is switchable (`stubtest_mode`: flag, `CURATE_STUBTEST_MODE`, plugin setting; pinned per run). Default `strict` runs every rank in an isolated `uv` env with the node's `deps.txt`, rejects faasr imports and requires every declared output to be uploaded. `parity` reproduces the original: rank 1 only, no dep install, `ModuleNotFoundError` with declared deps counts as a pass.
- Stub runs never see `GH_PAT`, S3 keys or LLM provider keys; secrets a function declares are passed through.
- The orchestrator always re-runs the stub test; retries are configurable (`mga_retries`, default 3; `0` = original Claude-SDK behaviour).
- Catalog `usage_count` is incremented at Gate 4 approval (original: during candidate resolution, `fca.py:238`).
- Saving to the catalog (`wda-finalize`) shows a dry-run preview first and saves only verified functions (stub test passed, or reused); the original's Gate 5 `save` stored any node with code.
- Reused catalog/cached nodes are stub-run so downstream nodes get real upstream test outputs.
- The MCA decides all nodes in one Claude turn, each node judged on its own fields (original: one LLM call per node, `agents/wca.py` 664-689). The user-model verbatim/adapt choice is made in the same turn (original: a separate call, `agents/fca.py` 59).
- The resolve step may not rename nodes: `merge-resolved` requires the candidate names (original: the resolve LLM could rename a node whose meaning changed). A rename is handled as a structural change at Gate 2.
- The revision classifier is applied by Claude in the main session using `classify.md`; mixed requests count as STRUCTURE (original: separate LLM call, ambiguous answers default to FUNCTION, no mixed-request rule).
- Deployments are named `<WorkflowName><suffix>` (`deploy_name_suffix`, default `-cs`, never empty) with function files in `functions/<Name>-cs/`, so a curate-skills deployment never writes CURATE's `functions/<fn>.py`, `workflows/<Name>.json` or `.github/workflows/<Name>-<action>.yml`; each action's S3 `folder` argument becomes `<folder>-cs` in the deploy copy (FaaSr's logs go to `FaaSrLog/<Name>-cs/` by the name alone), so the run never writes CURATE's S3 outputs or logs. The preflight refuses any path (repo or S3) that lacks the suffix or already exists — including the deployment's own earlier files — and a workflow with external S3 inputs, which the renamed folder would not hold. Emit's `<FAASR_…>` placeholders are filled from the environment in the deploy copy. The approved bundle itself is unchanged.
- **Deploy-copy rule.** The deploy copy may differ from the Gate 4-approved bundle only in `WorkflowName` (suffix), `ActionList.<action>.Arguments.folder` (suffix), `FunctionGitRepo.<fn>` (per-deployment folder) and fields that held an emit `<FAASR_…>` placeholder; any other difference is refused. The preflight shows every difference field by field (approved value, deploy value, reason), plus the locations that change only through the name (S3 log prefix, repo paths). CURATE deploys its bundle unchanged.
- **Deploying asks before each step.** CURATE deploys in one go after Gate 4 approval (push code, upload, register, invoke). curate-skills asks at Gate 4 whether to deploy at all (**Approve & deploy** / **Approve, no deploy**), then asks the user before each of the four steps; `deploy.py <step>` writes nothing without `--confirmed`. A failed step keeps the run at `deploy` with a resume command (CURATE ends the run). After invoke the run goes to curate-monitor. Gate 4 approval records the bundle's hash; every step refuses a bundle that changed since. `--start-at deploy --from-run` carries the source's Gate 4 approval over with its hash, and the user's Yes to "Start this run?" is recorded as the deploy choice (the source may predate the deploy option). The function files pushed are the approved bundle's files (CURATE pushes each node's code; the bundle file adds emit's 3-line comment header).
- **Monitoring is read-only, bounded and checks more than CURATE.** CURATE watches S3 (FaaSr logs, `.done` markers) with an inactivity timeout of 600 s and no overall limit. curate-monitor also matches each action's GitHub Actions run(s) to the InvocationID (job log, searched in memory) and requires every declared output to exist **and be written after the dispatch** (older files are "stale"). Success needs all Actions runs `success` and all outputs fresh; runs matched by time only give "success, unconfirmed match", which the user must accept at Gate 5 before anything is saved. Polling stops after `monitor_minutes` (default 10) with a resume command that continues monitoring the same invocation. A failed or timed-out run stops (CURATE goes on to its output review); D3 adds the Gate 5 review of failures. Output correctness is not judged.
- **Invoke only.** CURATE always pushes and registers before invoking. curate-skills can start an existing deployment again from a new run without writing to GitHub, only after verifying that every deployed file is byte-identical to what the run would deploy; the user sees the deploy-copy diff and confirms the invoke. The new invocation rewrites that deployment's outputs in S3.
- **Retrying a partial deploy** (`--overwrite-own-deploy`, off by default): an existing path is allowed only if this run's own deploy log records it for the same run, deploy name and repo/branch or bucket, and (repo) it still carries curate-skills' mark. CURATE overwrites its files on every deploy.
