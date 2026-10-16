# Run state and work files

A run lives in `<project>/.curate/runs/<run_id>/`. Only the scripts write `state.json`.

## state.json (schema_version 1)

| Field | Meaning |
|---|---|
| `run_id`, `run_dir`, `project_dir`, `created` | identity |
| `catalog_dir`, `mga_max_retries`, `stubtest_mode` | settings pinned at `init` |
| `user_request`, `user_models`, `context_files` | the sketch and attachments |
| `stage`, `iteration`, `max_iterations` | where the loop is |
| `revision_directives`, `feedback`, `code_feedback`, `structural_change` | change requests; directives persist across rounds |
| `skeleton_draft` | Gate 1 draft: `spec`, `last_good`, `parse_error`, `refinement` |
| `spec`, `prior_spec`, `spec_is_seed` | approved WorkflowSpec; prior code-bearing spec for `[cached]` |
| `enriched_spec`, `candidates`, `repairs` | MCA results and automatic repairs |
| `mga`, `validation`, `bundle_dir`, `history` | generation, emit and decision history |

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
