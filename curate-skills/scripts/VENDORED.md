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

### `scripts/runstate.py` (v2)

Run lock (`fcntl.flock` on `<run>/state.lock`), atomic JSON writes, and the input hashes stages record (`spec_hash`, `node_basis`). New; no counterpart in the original.

### `hooks/mga_guard.py`

PreToolUse write-scope guard for `curate-mga` subagents. New; no counterpart in the original.

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
