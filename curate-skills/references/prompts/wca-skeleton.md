# Skeleton prompt (Gate 1)

Adapted from CURATE-prototype `prompts/wca_prompts.py` `WCA_SKELETON_PROMPT` and
`agents/wca.py` `wca_compose_skeleton_node` (MIT, Copyright (c) 2026 Nolan Cutler; see NOTICE).

## Inputs

Run `spec.py prompt-context --run <run> --stage skeleton`. It returns `user_request`,
`feedback_section`, `current_workflow_section`, `user_models_section`,
`context_files_section`, and — on a Gate 1 refinement — `refinement` and `previous_draft`.
Fill them into the prompt below in this order (empty sections are simply absent).

**Refinement round** (`refinement` present): you already produced `previous_draft`. Apply
"Please revise the workflow: <refinement>" to it and write the complete revised object.

## Prompt

User request:
{user_request}

{feedback_section}
{current_workflow_section}
{user_models_section}
{context_files_section}
Analyze the request and produce a HIGH-LEVEL workflow skeleton as a JSON object matching this schema:
```json
{
  "name": "snake_case_workflow_name",
  "description": "one-line description",
  "nodes": [
    {"name": "snake_case_fn_name", "description": "plain-language summary of what this step does", "rank": 1}
  ],
  "edges": [
    {"from_node": "fn_a", "to_node": "fn_b"}
  ]
}
```

Rules:
- This is an OUTLINE only — describe each step in natural language.
- nodes are functions; each does ONE logical thing. Give each a snake_case name and a clear description.
- If the request states explicit implementation details for a step, copy them VERBATIM into
  that step's description. "OUTLINE only" means don't invent data-flow plumbing — it is not a
  license to summarize away specifics the user provided.
- If a "User-provided functions" block is shown, each listed function MUST appear as a node
  using its EXACT snake_case name — these are guaranteed steps the user attached. Place and
  order them sensibly given their descriptions and the request; wire their edges like any node.
- If a "User-provided reference files" block is shown, those are background material (papers,
  I/O examples, datasets) the MGA can consult — they are NOT nodes. Do NOT add a node for them;
  just let them inform how you scope and describe the steps.
- edges represent precedence/triggering (fn_a must complete before fn_b starts).
- EXACTLY ONE entry node (the only node with no incoming edge). Every other node MUST have at
  least one incoming edge and be reachable from the entry. If a step only produces data for a
  later step and nothing triggers it, add an edge from the entry (or a natural predecessor) so
  it still runs — do NOT leave a second node with no incoming edge.
- PARALLELISM: if a step runs as N independent parallel instances, model it as ONE node with
  "rank": N — do NOT emit node_1/node_2/node_3 duplicates. Its single successor automatically
  runs once after all N finish (fan-in is automatic; no fan-in node).
  Use "rank": 1 (or omit) for ordinary single-instance steps.
- Do NOT specify inputs, outputs, file names, data formats, or dependencies — those are
  resolved in the next stage (Gate 2), where they are made concrete and aligned with any
  catalog functions that get pulled in.
- Do not fill in entry, folder, or data_flow yet.
- If a "Current workflow being revised" block is shown, START from it and apply ONLY the requested
  change (e.g. changing a node's rank/fan-out N), preserving all other nodes, names, ranks, and edges.

## Output

Write ONLY the JSON object to `<run>/work/skeleton.json` — no prose, no markdown fence.
