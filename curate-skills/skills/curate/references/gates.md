# Gates, routing and resume

How CURATE's 9 LangGraph nodes map onto this skill (from CURATE-prototype
`orchestrator.py` 324-396):

| Original node | Here | Run `stage` while waiting |
|---|---|---|
| `compose_skeleton` | SKILL.md step 1 (you, WCA) | `skeleton` |
| `gate1_skeleton` | Gate 1 | `gate1` |
| `fca_resolve` | step 2 (you, MCA + WCA) | `resolve` |
| `gate2_candidates` | Gate 2 | `gate2` |
| `fga_generate` | step 3 (`curate-mga` per node) | `generate` |
| `gate3_code_review` | Gate 3 | `gate3` |
| `gate4_deploy` (validate + approve) | step 4 (`emit.py`) + Gate 4 | `emit`, `gate4` |
| `deploy_execute` | not in v1 (no deploy) | — |
| `gate5_output_review` | `wda-finalize` (local summary + catalog save) | `finalize` |

## Approval and routing

| Gate | Approve | Change request |
|---|---|---|
| 1 | `approve --gate 1` → resolve | `directive --gate 1` → skeleton (refinement; no iteration) |
| 2 | `approve --gate 2` → generate | classify → `directive --gate 2 --structural ...` → skeleton or resolve |
| 3 | `approve --gate 3` → emit | classify → `directive --gate 3 ...`; functional keeps working code as `[cached]` |
| 4 | `approve --gate 4` → finalize | classify → `directive --gate 4 ...` |
| emit failure | — | `directive --gate emit --text "<validation error>"` → resolve, once automatically |

Every Gate 2–4 change request and every automatic retry counts toward the iteration cap
(default 5).

## Resume table (`spec.py status`)

| `stage` | Continue with |
|---|---|
| `skeleton` | step 1.2 (a pending refinement is in `prompt-context`) |
| `gate1` | show Gate 1 and ask |
| `resolve` | step 2.2 |
| `gate2` | show Gate 2 and ask |
| `generate` | step 3.1 with `scratch.py build --resume` (keeps finished functions) |
| `gate3` | show Gate 3 and ask |
| `emit` | step 4.1 |
| `gate4` | show Gate 4 and ask |
| `finalize` | step 5 |
| `stopped` | ask which stage to reopen; `set-stage` there |
| `done` | nothing to do; offer a new run |
