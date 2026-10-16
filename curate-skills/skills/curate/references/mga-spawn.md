# Generate stage: spawning curate-mga (Gate 3 input)

Adapted from CURATE-prototype `agents/fga.py` (`_turn_prompt` 700-763, `_implement_all`
816-954, the OpenCode retry loop 640-690) (MIT, Copyright (c) 2026 Nolan Cutler; see NOTICE).

## Rules

- **One subagent at a time.** Never spawn the next `curate-mga` until the current one has
  finished and you have run every "after" step for it.
- **Foreground only.** `spec.py preflight` must pass before the first spawn. If a spawn ever
  comes back saying the subagent is running in the background, do nothing else until its
  completion notification arrives, then continue — and tell the user the session is not
  running with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`.
- **Unlock only after the subagent has finished.** In the foreground the Agent call returns
  only metadata; the subagent's report (ending in `MGA_RESULT`) arrives as a separate message
  right after it. Wait for both. In the background, wait for the completion notification —
  the report message alone is not enough.
- **Your stub test counts, not the subagent's.** Always re-run `stubtest.py` yourself.
- **Never paste generated code into chat** unless the user asks to see it.

## Per node (order from `scratch.py build`)

**Reused node** (`generate: false` in the build output — catalog or cached code):
`stubtest.py run --run <run> --node N`, so downstream nodes get its real outputs. Then
`scratch.py record --run <run> --node N --status reused` if it passed, or `--status
reused_failed` if not (report that at Gate 3; do not regenerate it).

**Node to generate:** attempts = 1 + the run's `mga_max_retries` (from `spec.py status`;
0 retries means exactly one attempt, as in the original CURATE). For attempt k:

1. `scratch.py snapshot --run <run> --node N`
2. `scratch.py lock --run <run> --node N`
3. Spawn: Agent tool, `subagent_type: "curate-skills:curate-mga"`, description
   `"MGA: N (attempt k)"`, prompt = the template below.
4. Wait for the `MGA_RESULT` report (see Rules).
5. `scratch.py unlock --run <run>`
6. `scratch.py verify-writes --run <run> --node N` — any violation fails this attempt; the
   retry feedback must list the paths.
7. `stubtest.py run --run <run> --node N` — this result decides.
8. If `<run>/functions/N/N.py` exists: `scratch.py harvest --run <run> --node N`.
9. Decide:
   - step 6 clean and step 7 `ok` → `scratch.py record ... --status passed --attempts k`; next node.
   - step 7 failed, the report says `EXTERNAL_ONLY`, and step 7 has
     `network_error_suspected: true` → `record --status external_only --attempts k`
     (warn at Gate 3); next node.
   - otherwise, if attempts remain → attempt k+1 with retry feedback.
   - otherwise → `record --status failed --attempts k`; continue with the next node (its
     inputs may be missing; the user decides at Gate 3).

Report one line per node as you go, e.g. `square_part ×2 — passed (attempt 1)`.

## Spawn prompt template

```
Implement the FaaSr function `<N>` for CURATE run <run_id>.

Run directory: <run>
Your folder (the only place you may write): <run>/functions/<N>
Read <run>/CONTEXT.md first, then <run>/functions/<N>/spec.md.

- Signature must be exactly: `<signature from spec.md>`
<rank rule>
- Write the implementation to <run>/functions/<N>/<N>.py and the PyPI packages it imports to
  <run>/functions/<N>/<N>.deps.txt (empty file if none) before testing.
- Validate it through the stubs with exactly:
    <stub test command from spec.md>
- When that test passes, stop immediately and end with the MGA_RESULT block.
<user-model block, if any>
<retry block, if attempt > 1>
```

**Rank rule** — pick by the node's rank:
- rank N > 1: `- THIS FUNCTION IS RANKED: it runs as N parallel instances. Call r = faasr_rank()
  (it reports THIS instance's own index) and use r['rank'] (1..N) to process ONLY this
  instance's shard. The input/output names in the spec contain a {rank} placeholder — substitute
  r['rank'] (e.g. input1.format(rank=r['rank'])). Implement ONE function, never copies.`
- rank 1: `- faasr_rank() reports THIS function's OWN rank and is only valid when this function
  is itself ranked — which it is NOT. Do NOT call faasr_rank(). If you feed a ranked successor,
  follow the spec's fan-out section exactly (do not derive counts from faasr_rank()).`

**User-model block** (source `user_provided`):
- verbatim: `[USER PROVIDED MODEL — VERBATIM] original_<N>.py in your folder is the user's
  ORIGINAL Python script/function and its code must NOT change. Copy it UNCHANGED into <N>.py,
  add a FaaSr-compliant entry function <N> that produces its results ONLY by CALLING the
  preserved model, prove the entry function invokes the model, verify output parity, and write
  parity_report.txt in your folder. Full steps: spec.md → "[User Provided Model] — Wrap Verbatim".`
- adapt: `[USER PROVIDED MODEL] original_<N>.py in your folder is the user's ORIGINAL Python
  script/function. Do NOT write <N> from scratch — REFACTOR the original's logic into <N>.py,
  apply exactly the changes the spec/feedback requires, VERIFY PARITY against the original, and
  write parity_report.txt in your folder. Full steps: spec.md → "[User Provided Model] — Refactor
  & Verify Parity".`

**Retry block** (attempt > 1):
```
CURATE independently re-ran the required stub test after your last attempt and it failed.
Inspect the current files in your folder, fix the function, and run the test again.
Failure output:
<last 6000 characters of the step-7 tail, or the verify-writes violations>
```
