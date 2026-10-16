# Resolve prompt (Gate 2)

Adapted from CURATE-prototype `prompts/wca_prompts.py` `WCA_RESOLVE_PROMPT` and
`agents/wca.py` `wca_fca_node` (MIT, Copyright (c) 2026 Nolan Cutler; see NOTICE).

## Inputs

After `apply-candidates`, run `spec.py prompt-context --run <run> --stage resolve`. It returns
`workflow_json` (the skeleton with candidates applied — catalog renames done, code omitted),
`fca_candidates`, `user_models_section`, `context_files_section` and `feedback_section`.

## Prompt

The workflow skeleton has been reviewed. Now resolve the concrete data dependencies.

Current skeleton:
{workflow_json}

Candidate functions proposed by the MCA:
{fca_candidates}
{user_models_section}
{context_files_section}
{feedback_section}

The skeleton is a high-level outline with no I/O yet. Make it concrete now: define each
function's inputs and outputs, aligned with the catalog candidates above.

Produce a complete WorkflowSpec JSON:
```json
{
  "name": "...",
  "description": "...",
  "entry": "name_of_first_function",
  "folder": "workflow_folder_name",
  "compute_target": "GH",
  "data_store": "S3",
  "nodes": [
    {"name": "fn_a", "description": "...", "rank": 1, "inputs": [{"name": "input.csv", "type": "csv", "description": ""}], "outputs": [{"name": "output.csv", "type": "csv", "description": ""}], "dependencies": ["pypi_package"]}
  ],
  "edges": [...],
  "data_flow": [
    {"from_node": "fn_a", "to_node": "fn_b", "file": "concrete_filename.csv", "format": "csv", "folder": "workflow_folder_name"}
  ]
}
```

Rules:
- Every node MUST have concrete inputs and outputs (S3 file names + formats) — no empty I/O.
- REUSE/catalog candidates run verbatim catalog code: keep their input/output COUNT and
  FORMATS, and keep their existing file names UNLESS an edge to/from an ADAPT node forces a
  rename for consistency (see below).
- ADAPT candidates will be REGENERATED. UPDATE their input/output file names to reflect the
  change described in that function's description / adaptation notes (e.g. a new date, region,
  dataset, or filename). Do NOT leave a stale name that contradicts the description (e.g. keep
  a stale date or label in a filename when the description specifies a different one).
- CONSISTENCY OVERRIDES EVERYTHING: for every edge, the producer's output name, the consumer's
  input name, and the data_flow "file" MUST be identical. If an ADAPT rename changes a file that
  a downstream catalog function consumes, rename that consumer's input to match too — this is
  safe because file names are passed as runtime arguments, not hardcoded in the function.
- CATALOG (verbatim reuse) functions have FIXED filenames in their code. On any edge touching a
  catalog node, set the shared file to the CATALOG function's actual input/output name, and make
  the new/adapt neighbor on the other end use that exact name. Catalog I/O names are authoritative;
  the generation agent reconciles neighbors against the real catalog code.
- User-provided nodes (shown in the "User-provided functions" block) keep their EXACT name and
  stay in the workflow — the MGA turns the attached code into a FaaSr function. Give them
  concrete inputs/outputs like any node so data flows correctly.
- A node's NAME must describe what it does NOW. Verbatim-reuse (catalog) nodes keep their exact
  name. *(curate-skills: `merge-resolved` requires the node names to match the candidates
  exactly, so do not rename nodes here; a rename is a structural change for Gate 2.)*
- FOLDER: if a change request names the S3 folder (e.g. "use X as the folder", "don't use the
  default folder, use X"), SET the top-level "folder" field to that name AND use it for every
  data_flow entry's "folder" — this is the actual S3 prefix for external inputs and all data. Do NOT
  merely mention the folder in descriptions while leaving "folder" unchanged.
- dependencies lists ONLY third-party PyPI packages each function needs. NEVER list `faasr` (it is
  the runtime, always available) or standard-library modules (re, json, os, sys, collections, …).
  Functions run in minimal headless Linux containers with no display server or GUI/system
  libraries — when a package offers a headless or slim variant, pick it over the full build.
- PARALLELISM (rank): keep parallel steps as ONE node with "rank": N — never duplicate nodes, and
  do NOT add a separate fan-in node (the successor runs once automatically after all N instances).
  For a ranked node, its per-instance input/output filenames MUST contain a "{rank}" placeholder
  (e.g. "ranked_func{rank}.json") — each instance fills in its own rank at runtime via
  faasr_rank(). The single producer feeding a ranked node writes the whole family
  (base_1 … base_N), and the single successor after it reads all N. Edges stay simple:
  producer → ranked → successor (one edge each), never producer → ranked_1, producer → ranked_2.
  "{rank}" is the ONLY placeholder ever allowed in a filename — NEVER invent any other
  placeholder. When each instance of a ranked node handles a distinct category (e.g. one
  category per instance), number the files by rank ("result_{rank}.json", rank 1 = first
  category, …) and map rank → category inside the function code.
- EXACTLY ONE entry node: `entry` names the single node with no incoming edge. Every OTHER node
  MUST be reachable from `entry` via `edges`. A source/entry node MAY have inputs that no upstream
  node produces — these are EXTERNAL inputs the user places in S3 before the run; they need no
  data_flow edge. Keep external inputs intentional and minimal.
- REACHABILITY via trigger edges: invocation `edges` are what make a node run; they are SEPARATE
  from `data_flow`. A node that only produces data consumed downstream (no upstream node triggers
  it) is still unreachable unless something invokes it — add an incoming `edge` from the entry, or
  from the node that produces its input, EVEN IF no file is passed on that edge. Two nodes with no
  incoming edge = two entries = invalid.
- folder is a short, descriptive S3 prefix (no spaces).
- every `data_flow` entry MUST have a matching `edge` (same from_node/to_node), but an `edge` MAY
  exist with NO `data_flow` — a trigger-only edge that exists purely to make a node reachable. A
  data-flow edge's "file" must match the producing node's output name and the consuming node's
  input name.
- file names should be descriptive (e.g. "raw_input.csv" not "file1.csv").

## Output

Write ONLY the JSON object to `<run>/work/resolved.json`, then run
`spec.py merge-resolved --run <run> --json <run>/work/resolved.json`. The script restores
catalog/cached code and I/O, adds missing trigger edges, drops redundant ones and reports
`problems`. If any problem has severity `error`, fix `resolved.json` once and re-run
`merge-resolved`; if errors remain, show them at Gate 2.
