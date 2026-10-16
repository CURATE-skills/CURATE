# Revision classifier (Gates 2–4)

Adapted from CURATE-prototype `prompts/wca_prompts.py` `WCA_CHANGE_CLASSIFY_PROMPT` and
`agents/wca.py` `classify_structural_change` (MIT, Copyright (c) 2026 Nolan Cutler; see NOTICE).

Decide, yourself, whether a change request alters the workflow's STRUCTURE or only the
FUNCTIONS. Use the current DAG (nodes with ranks, and edges) from the gate display.

A user is revising an existing FaaSr workflow. Decide whether their change request alters
the workflow's STRUCTURE or only the FUNCTIONS themselves.

STRUCTURE (the DAG/topology) — answer STRUCTURE if the request would:
- add, remove, rename, or reorder steps (nodes)
- change how steps connect / their precedence / data dependencies (edges)
- change parallelism / fan-out (a step's rank N)

FUNCTION (behavior of existing steps) — answer FUNCTION if the request only:
- changes what an existing step computes, its algorithm, code, parameters, or styling
- changes an existing step's input/output filenames or formats
without adding/removing steps or rewiring the graph.

When genuinely unsure, answer FUNCTION.

**Mixed requests** (some structural, some functional parts): answer STRUCTURE. The skeleton
path re-runs resolution anyway, and every directive is re-applied there, so the functional
part is not lost.

Before recording it, tell the user in one line, e.g.
"Classified as STRUCTURE (adds a node) → back to the skeleton. Say 'function' to override."
Then run `spec.py directive --gate N --text "<request verbatim>" --structural true|false`.
