# User-model mode (MCA, for [User Provided Model] nodes)

Adapted from CURATE-prototype `prompts/fca_prompts.py` `FCA_USER_MODEL_PROMPT`
(MIT, Copyright (c) 2026 Nolan Cutler; see NOTICE).

For a node whose `user_model` is set (from `prompt-context --stage mca`), read the model file
at `user_model.path` (first ~200 lines are enough) and decide. Record the answer in that
node's `candidates.json` entry as `"decision": "new", "user_model_mode": "verbatim"|"adapt"`.

## Prompt

The user attached their own model (a Python script or function) for the workflow step below.
Decide whether the model can be used VERBATIM or must be ADAPTED.

Definitions:
- verbatim: the model's own code does not change. The generation agent copies it unchanged and
  wraps it with FaaSr I/O and a compliant entry function that calls the model.
- adapt: the model's own computation must change (different algorithm, formula, parameters,
  units, or output semantics) to satisfy what the user asked for.

Rules, in priority order:
1. If the user explicitly asked that their model be used verbatim / unchanged / as-is (in the
   original request or ANY revision below), answer verbatim. This is mandatory and overrides
   every other consideration.
2. FaaSr-compatibility issues NEVER justify adapt: wrong signature, script form (argparse,
   __main__, hardcoded paths), local file I/O instead of S3, missing faasr_* calls, input/output
   format conversion — the wrapper absorbs all of that.
3. Answer adapt ONLY when the request or feedback explicitly indicates the model's own
   computation must change.
4. Default is verbatim. Bias strongly toward verbatim: build the rest of the workflow around
   the user's model rather than changing the model to fit the workflow.

Workflow step: {name} — {description}
Inputs: {inputs}
Outputs: {outputs}

Overall workflow intent:
{workflow_context}

{feedback}

User model code: the file at {user_model.path}.

Answer: {"mode": "verbatim" | "adapt", "reason": "one sentence"} — tell the user the reason
in one line when you show Gate 2.
