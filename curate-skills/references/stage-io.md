# Stage inputs, imports and preconditions

Every stage skill accepts `--run <run_id>` (an existing run) or `--from <file>` (start a new run
from your own file, via `spec.py import`). Before doing anything, `spec.py begin` checks the
stage's inputs (`spec.py require`) and, if something is missing, says what and which skill
produces it.

## What each stage needs

| Stage | Needs | Missing → run |
|---|---|---|
| skeleton | a sketch (or a spec to revise) | `/curate-skills:curate-skeleton --from <sketch>` |
| reuse | an approved skeleton (or a full spec) | curate-skeleton |
| resolve | reuse decisions made for the current spec | curate-reuse |
| generate | a resolved spec: inputs/outputs on every node, no `check` errors, a description for every function to generate, resolved from the current reuse decisions; plus the foreground setting when anything is generated | curate-resolve |
| emit | code for every function, generated for the current spec | curate-generate |
| deploy | Gate 4 approved with Approve & deploy, a validated bundle for the current spec, its recorded hash | curate-emit |
| finalize | a validated bundle built from the current spec | curate-emit |

## Start points (`/curate --start-at <stage>`)

`spec.py start --at <stage>` creates a **new** run from exactly one source; the source is never
written (it is read without the run lock). `/curate` then shows the returned `summary` and asks
"Start this run? Yes / Stop"; Stop marks the new run stopped and deletes nothing.

| Source | Allowed stages | What is used |
|---|---|---|
| `--from-run <run_id or folder>` | reuse, resolve, generate, emit, deploy, finalize | the latest approval snapshot `history/NNN-gateN-approve.json` of: Gate 1 (reuse), Gate 2 (resolve, generate), Gate 3 (emit), Gate 4 (deploy, finalize). Deploy also needs the bundle's hash (from the snapshot, or for runs approved before hashes existed, `bundle_hash` in the source's state.json for the same bundle) and refuses a bundle that changed |
| `--from-bundle <folder>` | generate (needs `--existing-code`), emit | the bundle's `spec.json` with code |
| `--from-file <file>` | as `--from` below | the file |
| `--sketch-file <file>` | skeleton | the sketch |

From a run, the new run gets the approved spec plus, by stage: the reuse decisions (resolve),
the stub-test results (emit), the validation and bundle path (finalize); also the sketch,
attachments and standing change requests. It does **not** get the source's old code
(`prior_spec`) or its unapproved current state. History records `copied`, the source run, the
snapshot file and its sha256.

Refusals (nothing is created): no exactly-one source; a gate never approved; an approval
superseded by a later change request or rewind that sent the run back before that gate (a
structural change voids every gate; a functional one or the emit retry voids Gates 2–4; a
rewind voids the gates from its target on); a run id that is not exact (close matches are
listed) or unknown; a v1 run; a bundle without `spec.json` or without code; a source that does
not fit the stage; `--existing-code` off generate. `--dry-run` reports the same summary without
creating anything.

**Generate from a run whose Gate 3 was approved** needs `--existing-code` (otherwise
`needs_choice: "existing_code"`, nothing created). `keep` / `rewrite` put the code of the latest
Gate 3 approval snapshot onto the Gate 2 spec by function name (`cached` / `adapt`, labelled
"kept: your code" / "rewritten from your code"); `ignore` generates every function from scratch.
History adds `code_snapshot`, `code_sha256` and `existing_code`. `keep` or `rewrite` without an
approved, unsuperseded Gate 3 after the last Gate 2 approval is refused. The summary's
"Generate:" line says which functions are kept and which get a subagent.

## Importing (`--from`)

An imported file counts as approved (the history records `imported` with the file's sha256).
`--review` stops at the gate before the stage instead (reuse → Gate 1, generate → Gate 2,
emit → Gate 3). Every `init` flag works (`--catalog`, `--mga-retries`, `--stubtest-mode`,
`--attach`, `--context`). Without a sketch, the request text is built from the spec's name and
description; `--request-file` overrides it.

| Start at | `--from` | Extra |
|---|---|---|
| skeleton | sketch `.txt`/`.md` | — |
| reuse | `skeleton.json`, or a full WorkflowSpec (kept as a seed, I/O not stripped) | `--review` |
| resolve | `skeleton.json` | `--candidates candidates.json` (required; catalog ids must exist in the run's catalog) |
| generate | complete WorkflowSpec (or a bundle's `spec.json`) | `--functions DIR`; `--existing-code keep\|rewrite\|ignore` (required whenever there is code); `--review` |
| emit | WorkflowSpec with code for every node | `--functions DIR`, `--review` |
| finalize | not supported (a bundle has no stub-test results) | use `--run` |

At generate, any code you bring — a `--functions` folder, or code already inside the spec
(e.g. a bundle's `spec.json`) — needs an explicit `--existing-code`. Without one, the import creates nothing and returns
`needs_choice: "existing_code"`; the skill then asks you (Keep / Rewrite / Ignore) and reruns, or, in a
headless run where it cannot ask, stops with the three options:

| `--existing-code` | What happens | Gate 3 shows |
|---|---|---|
| `keep` | kept as is; no subagents; every function is stub-tested (how existing code becomes "verified") | `kept: your code` |
| `rewrite` | each function regenerated by a subagent, starting from your code | `rewritten from your code` |
| `ignore` | the code is dropped; functions generated from scratch (not allowed with `--functions`) | `[new]` |

Files in `--functions` override code in the spec. Bundle headers (`# Generated by
curate-skills`) are stripped. `--existing-code` applies only at generate. The old spelling
`--functions-as reuse|seed` still works and means `keep|rewrite`.

## Not supported: a FaaSr `workflow.json`

It has no descriptions, so it cannot start generate, and starting emit from it would only
rebuild almost the same file (`emit.py validate --workflow` validates one directly). Converting
it back would lose descriptions, input/output types, data-flow formats, per-function secrets,
where each function came from, and any argument not named `inputN`/`outputN`. Bringing an
existing FaaSr workflow in for revision is future work.
