# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "pyyaml==6.0.2", "cwl-utils==0.47"]
# ///
"""The Pegasus target as Gate 4 and "Start this run?" show it, read-only: the run's Pegasus
settings (host, runtime, image and where the image comes from), the option labels, and what a
Pegasus run of the draft or the approved version would do (plan: built in memory by
execution.pegasus_built, i.e. scripts/cwl_pegasus.py).

The Pegasus folder itself is written by pegasus_deploy.py prepare, from the approved version.
Until phase 8 this module also mapped a spec straight to an IR v0.2 folder (build_ir); every
workflow now goes through the CWL draft.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import envfile  # noqa: E402
from curate_models import WorkflowSpec  # noqa: E402

DEFAULT_IMAGE = envfile.DEFAULT_PEGASUS_IMAGE
RUN_TS_RX = re.compile(r"^\d{4}(\d{2})(\d{2})-(\d{2})(\d{2})")  # run ids: YYYYMMDD-HHMMSS-<4 hex> (older: -<name>)


def ir_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "workflow"


# ── Gate 4: the Pegasus target, read-only ────────────────────────────────────

def run_ir_name(spec_name: str, run_id: str) -> str:
    """The IR (Pegasus workflow) name for a run: <workflow>-<MMDD-HHMM> of the run, the same
    timestamp rule as the FaaSr deploy name's default suffix."""
    m = RUN_TS_RX.match(run_id or "")
    return ir_name(spec_name) + (f"-{m[1]}{m[2]}-{m[3]}{m[4]}" if m else "")


def settings(env=None) -> dict:
    """CURATE_PEGASUS_* from the environment, else ~/.config/curate/.env (never modifies os.environ)."""
    env = dict(os.environ if env is None else env)
    envfile.load(env, names=envfile.PEGASUS_NAMES)
    return envfile.pegasus_settings(env)


IMAGE_SOURCE_SETTING = "CURATE_PEGASUS_IMAGE"
IMAGE_SOURCE_DEFAULT = "the built-in default"


def effective(s: dict, current: dict | None = None) -> dict:
    """The run's Pegasus settings: pinned by prepare; before that, the current settings with the
    run's own image (chosen at Gate 4 or "Start this run?") if it has one. Adds `image_source`."""
    rec = s.get("pegasus") or {}
    if rec.get("runtime"):  # pinned
        return {"host": rec.get("host"), "runtime": rec["runtime"], "image": rec["image"],
                "image_source": rec.get("image_source") or IMAGE_SOURCE_SETTING}
    st = dict(current or settings())
    if rec.get("image_choice"):
        st.update(image=rec["image_choice"], image_source=rec.get("image_source") or "this run only")
    else:
        explicit = settings_image_set()
        st["image_source"] = IMAGE_SOURCE_SETTING if explicit else IMAGE_SOURCE_DEFAULT
    return st


def settings_image_set(env=None) -> bool:
    env = dict(os.environ if env is None else env)
    envfile.load(env, names=envfile.PEGASUS_NAMES)
    return bool(str(env.get("CURATE_PEGASUS_IMAGE", "")).strip())


def target_label(st: dict) -> str:
    return st["host"] or "this machine"


DIFFERENT_IMAGE_OPTION = "Approve & run on Pegasus with a different image…"


def option_label(st: dict) -> str:
    """Gate 4's Pegasus option, naming where it would run and the image it would use."""
    return f"Approve & run on Pegasus ({target_label(st)} · {st['image']})"


def plan(s: dict, st: dict) -> dict:
    """What the Pegasus target would do for this run, without writing anything: the IR name,
    job count, inputs and deliverables; or the problems that make it impossible."""
    spec = WorkflowSpec.model_validate(s["spec"])
    import execution as X
    if X.is_versioned(s) and s.get("run_dir"):  # built in memory from the draft or the approved version
        ex = s.get("execution") or {}
        name = run_ir_name(spec.name, ex.get("run_id") or s.get("run_id", ""))
        peg_v = (s.get("validation") or {}).get("pegasus") or {}
        if peg_v.get("ok") is False and not X.stale_draft(s):  # emit's validation already refused it
            return {"ok": False, "name": name, "problems": peg_v.get("problems") or [peg_v.get("error")]}
        try:
            b = X.pegasus_built(s, s["run_dir"], name, st["image"])
        except X.ExecutionError as e:
            return {"ok": False, "name": name, "problems": e.problems}
        return {"ok": True, "name": name, "jobs": len(b["ir"]["nodes"]), "functions": len(b["functions"]),
                "inputs": b["needed"], "deliverables": [o["deliver_as"] for o in b["ir"]["outputs"]],
                "deliverable_files": len(b["ir"]["outputs"]), "warnings": b["warnings"]}
    return {"ok": False, "name": run_ir_name(spec.name, s.get("run_id", "")),
            "problems": [f"no workflow draft yet: run /curate-skills:curate-emit --run {s.get('run_id')}"]}


def summary_lines(s: dict, st: dict, p: dict | None = None) -> list[str]:
    """The short Pegasus summary: Gate 4's "Where it can run", and the image check before a run
    with a different image. No file lists (those are in plan_lines, for the full review)."""
    where = f"`{st['host']}` (over ssh)" if st["host"] else "this machine"
    p = p or plan(s, st)
    if not p["ok"]:
        return [f"**Pegasus** on {where}: **cannot run on Pegasus:**"] + [f"- {x}" for x in p["problems"]]
    src = st.get("image_source")
    n = p["deliverable_files"]
    out = [f"**Pegasus** on {where}: `{p['name']}`, {p['jobs']} jobs, image `{st['image']}`"
           + (f" ({src})" if src else "") + f", runtime {st['runtime']}, {n} deliverable{'s' if n != 1 else ''}",
           "- input files you must provide: " + (f"{len(p['inputs'])} (listed in the full review)"
                                                  if p["inputs"] else "none")]
    out += [f"- note: {w}" for w in p["warnings"]]
    return out


def plan_lines(s: dict, st: dict) -> list[str]:
    """The full "Pegasus run would write" lines, for the Gate 4 review file."""
    run_dir = s.get("run_dir") or "<run>"
    local = f"{run_dir}/pegasus"
    out = [f"If you choose **{option_label(st)}** (nothing is written now; the run asks before each step):", ""]
    if st["host"]:
        out.append(f"- Runs on: `{st['host']}` over ssh; `{local}/` is copied to "
                   f"`{st['host']}:~/curate-runs/{s.get('run_id')}/` and run there")
    else:
        out.append(f"- Runs on: this machine (Pegasus + HTCondor), in `{local}/`")
    p = plan(s, st)
    if not p["ok"]:
        out.append("- **Cannot run on Pegasus:**")
        out += [f"  - {x}" for x in p["problems"]]
        return out
    out += [f"- Workflow: `{p['name']}`, {p['jobs']} jobs, image `{st['image']}`, runtime {st['runtime']}",
            f"- Writes: `{local}/ir.yaml`, `{local}/functions/` ({p['functions']} files), `{local}/inputs/`",
            "- Input files you must provide: " + (", ".join(f"`{f}`" for f in p["inputs"]) or "none"),
            "- Delivered: " + (", ".join(f"`{d}`" for d in p["deliverables"]) or "nothing")]
    out += [f"- Note: {w}" for w in p["warnings"]]
    return out
