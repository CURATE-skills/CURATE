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
| `curate_models.py` | `models.py` | 8-67, 96-135, 157-163 | Drop `UsageRecord`; reduce `UserModel`/`ContextFile` to `{name, path, description}` | vendored |
| `emit.py` | `faasr/emit.py`; `agents/wda.py` | 1-218; 15-53 | CLI wrapper; unset `FAASR_*` deployment vars emitted as `<FAASR_NAME>` placeholders (original: `username`, `faasr-functions`, `faasr-data`, AWS defaults); bundle writer adds `spec.json`, `external_inputs.json`, `README.md`, `.deps.txt` and refuses to write if a known secret value appears; structural fallback validator | vendored |
| `faasr_validate.py` | `faasr/validate.py` | 1-45 | Standalone PEP 723 script with its own `FaaSr_py==0.1.13` env (which pins `boto3==1.37.11`; the original's boto3 override was only for Bedrock); JSON in, JSON out | vendored |
| `catalog.py` | `catalog/store.py`; `catalog/workflow_store.py`; `orchestrator.py` | 20-84; 1-63; 173-226 | Configurable root (`functions/`, `workflows/` subdirs); CLI; `--only-verified`; import/export | vendored |
| `faasr_stubs.py` | `faasr/context_dir.py` (`_STUBS_CODE`) | 19-69 | Per-node layout: own `test_output` → own `test_data` → `$CURATE_UPSTREAM_DIRS` | planned |
| `stubtest.py` | `agents/fga.py` (`_run_stub_test`); `faasr/context_dir.py` (`signature_and_test_call`) | 239-286; 234-248 | Runs every rank; isolated `uv run --with-requirements` env; writes `stubtest.log` | planned |

## New code (design reference only, not vendored)

| Script | Behaviour reproduced from |
|---|---|
| `spec.py` | `agents/wca.py` 72-115 (skeleton parse), 323-375 (merge), 378-445 (renames), 448 (`_ensure_reachable`), 549 (`_prune_transitive_edges`); `agents/fca.py` 199-276 (decision mapping); `faasr/context_dir.py` 76-110 (Kahn sort) |
| `scratch.py` | `faasr/context_dir.py` 117-231 (scratch dir, test data seeding); `agents/fga.py` 937-981 (deps / secrets harvest) |
| `hooks/mga_guard.py` | none (plugin-specific write-scope guard) |

## Adapted prompt text

| Plugin file | Source |
|---|---|
| `skills/curate/references/wca-system.md` | `prompts/wca_prompts.py` 1-45 (`WCA_SYSTEM`) |
| `skills/curate/references/classify.md` | `prompts/wca_prompts.py` 47-66 (`WCA_CHANGE_CLASSIFY_PROMPT`) |
| `skills/curate/references/wca-skeleton.md` | `prompts/wca_prompts.py` 68-115 (`WCA_SKELETON_PROMPT`) |
| `skills/curate/references/wca-resolve.md` | `prompts/wca_prompts.py` 117-208 (`WCA_RESOLVE_PROMPT`) |
| `skills/curate/references/mca-candidates.md` | `prompts/fca_prompts.py` 1-108 (`FCA_SYSTEM`, `FCA_CANDIDATE_PROMPT`) |
| `skills/curate/references/mca-user-model.md` | `prompts/fca_prompts.py` 111-147 (`FCA_USER_MODEL_PROMPT`) |
| `skills/curate/references/mga-spawn.md` | `agents/fga.py` 700-763 (`_turn_prompt`) |
| `agents/curate-mga.md` | `agents/fga.py` 71-157 (`_SHARED_RULES`) |
| `skills/faasr-reference/SKILL.md` | `faasr/context_dir.py` 251-357 (`_shared_context_md`), 403-436 (rank rules) |
| `scratch.py` templates (`CONTEXT.md`, `spec.md`) | `faasr/context_dir.py` 251-569 |

## Deliberate behaviour changes

- Unset `FAASR_*` deployment variables become `<FAASR_NAME>` placeholders instead of guessed defaults; secret values are never written (bundle aborts if one is found).
- Stub tests run every rank (original: rank 1 only).
- Stub tests run in an isolated `uv` env with the node's `deps.txt` (original: system interpreter; `ModuleNotFoundError` counted as pass).
- The orchestrator always re-runs the stub test; retries are configurable (`mga_retries`, default 3; `0` = original Claude-SDK behaviour).
- Catalog `usage_count` is incremented at Gate 4 approval (original: during candidate resolution, `fca.py:238`).
- Reused catalog/cached nodes are stub-run so downstream nodes get real upstream test outputs.
