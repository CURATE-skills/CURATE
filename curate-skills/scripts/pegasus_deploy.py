# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "rank-bm25==0.2.2", "pyyaml==6.0.2", "boto3==1.37.11", "cwl-utils==0.47", "pegasus-wms.api==5.1.3"]
# ///
"""The Pegasus target's deploy steps, for a run whose Gate 4 chose "Approve & run on Pegasus".

  pegasus_deploy.py preflight --run RUN [--inputs DIR]
  pegasus_deploy.py prepare   --run RUN [--inputs DIR] [--confirmed] [--overwrite-own]
  pegasus_deploy.py copy      --run RUN [--confirmed] [--overwrite-own]       (ssh host only)
  pegasus_deploy.py launch    --run RUN [--confirmed] [--overwrite-own]
  pegasus_deploy.py status    --run RUN

preflight is read-only. Each step without --confirmed writes nothing and returns
`needs_confirmation` with a `plan`; the stage asks the user, then repeats it with --confirmed.

  prepare  writes the run's bundle/ (workflow.yml: the Pegasus workflow, scripts/cwl_pegasus.py;
           functions/; inputs/ with the files from --inputs) and checks it (cwl_pegasus.validate_folder).
  copy     CURATE_PEGASUS_HOST only: creates <host>:~/curate-runs/<run_id>/ and copies ir/ and
           the plugin's curate_ir/ into it.
  launch   pegasus_backend.py submit, here or over ssh: plans and submits to HTCondor and returns
           at once (DAGMan runs the workflow; nothing waits). Records the Pegasus run folder in
           state.pegasus.run_dir, so curate-monitor finds it after a logout or reboot, and moves
           the run to `monitor`.

Nothing is overwritten: a target that exists is refused. A failed step can be repeated with
--overwrite-own, which first removes only what that step itself wrote (from
the run's deploy-log.jsonl). Settings (CURATE_PEGASUS_HOST / _RUNTIME / _IMAGE) are pinned
by prepare; later steps refuse if they changed. Prints JSON {"ok", ...}; exit 0 ok, 1 needs
attention (or confirmation), 2 bad input.
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import hostcmd  # noqa: E402
import images  # noqa: E402
import leakcheck  # noqa: E402
import pegasus_ir as P  # noqa: E402
import spec as S  # noqa: E402
from curate_models import WorkflowSpec  # noqa: E402
import runstate  # noqa: E402
from runstate import RUN_HELP  # noqa: E402

PLUGIN_DIR = SCRIPT_DIR.parent
IR_TOOLS = PLUGIN_DIR / "curate_ir"
LOG = Path("deploy-log.jsonl")  # in the run
LEGACY_LOG = Path("pegasus") / "deploy-log.jsonl"  # runs made before runs had their own state
REMOTE_ROOT = "curate-runs"
SSH_OPTS = hostcmd.SSH_OPTS
PEGASUS_PY = 'PYTHONPATH="$(pegasus-config --python)" python3'
STEP_DONE = {"prepare": "prepared", "copy": "copied", "launch": "launched"}
STEP_WHAT = {"prepare": "write the Pegasus workflow folder (IR, functions, inputs) and validate it",
             "copy": "copy the workflow folder and CURATE's IR tools to the Pegasus host",
             "launch": "plan and submit the workflow to HTCondor (it then runs on its own)"}


class StepError(Exception):
    pass


host_env = hostcmd.host_env  # commands run with the system's Python, not uv's (hostcmd.py)
_run = hostcmd.run
RUN = _run  # tests replace this; every command this script runs goes through it
_tail = hostcmd.tail


def _on(host, command, timeout=600):
    """A shell command here, or on `host` over ssh (no password prompt)."""
    return hostcmd.on(host, command, runner=RUN, timeout=timeout)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def q(x) -> str:
    return shlex.quote(str(x))


# ── state and log ────────────────────────────────────────────────────────────

def _no_leak(text: str) -> None:
    if leakcheck.find_leaks(text):
        raise StepError("refusing to write state.json: it would contain a secret value")


def _update(run: Path, change) -> dict:
    """Change the run's state (runstate.update: an execution's records go to its run)."""
    return runstate.update(run, change, _no_leak)


def log_path(run: Path) -> Path:
    """The run's deploy-log.jsonl (<run>/pegasus/ for a run made before runs
    had their own state)."""
    s = runstate.load(run)
    return Path(run) / LEGACY_LOG if runstate.is_legacy(run, s) else runstate.files_dir(run, s) / LOG


def _log(run: Path, step: str, where: str, path: str) -> None:
    f = log_path(run)
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a") as fh:
        fh.write(json.dumps({"ts": _now(), "step": step, "where": where, "path": str(path)}) + "\n")


def read_log(run: Path) -> list[dict]:
    f = log_path(run)
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.is_file() else []


def _own(run: Path, step: str, where: str, path: str) -> bool:
    return any(e["step"] == step and e["where"] == where and e["path"] == str(path) for e in read_log(run))


# ── where things go ──────────────────────────────────────────────────────────

def steps_for(host) -> tuple[str, ...]:
    return ("prepare", "copy", "launch") if host else ("prepare", "launch")


def _settings(s: dict) -> dict:
    """Pinned by prepare; before that, the current settings with the run's own image (chosen at
    Gate 4 or "Start this run?") if it has one. Includes `image_source`."""
    return P.effective(s)


def _changed(s: dict) -> dict | None:
    """After prepare: the settings that differ from what prepare pinned ({name: [pinned, now]}).
    CURATE_PEGASUS_IMAGE does not count when the run has its own image."""
    rec = s.get("pegasus") or {}
    if not rec.get("runtime"):
        return None
    cur = P.settings()
    keys = ["host", "runtime"] + ([] if rec.get("image_choice") else ["image"])
    diff = {k: [rec.get(k), cur[k]] for k in keys if rec.get(k) != cur[k]}
    return diff or None


def _layout(run: Path, s: dict, st: dict) -> dict:
    rec = s.get("pegasus") or {}
    ex = s.get("execution") or {}
    if not (s.get("workflow_id") and ex.get("target") == "pegasus" and ex.get("dir")):
        import execution as X
        raise StepError(X.pre_version(s) if not s.get("workflow_id") else
                        "this run has no Pegasus run (it is opened when Gate 4 or \"Start this run?\" "
                        "chooses Pegasus); approve Gate 4 again with --deploy pegasus")
    # the folder is the run's bundle, and the host path its full reference
    local, ir_dir, ref = Path(ex["dir"]), Path(ex["bundle_dir"]), ex["reference"]  # ir_dir: the Pegasus folder
    out = {"local": local, "ir_dir": ir_dir, "where": st["host"] or "local", "reference": ref}
    if st["host"]:
        remote = rec.get("remote_dir")  # absolute, known after copy
        shown = remote or f"~/{REMOTE_ROOT}/{ref}"
        out.update(remote_dir=remote, shown_remote=shown, workflow_on_host=f"{shown}/{ir_dir.name}/workflow.yml",
                   work=f"{shown}/work", backend=f"{shown}/curate_ir/backends/pegasus_backend.py")
    else:
        out.update(remote_dir=None, workflow_on_host=str(ir_dir / "workflow.yml"), work=str(local / "work"),
                   backend=str(IR_TOOLS / "backends" / "pegasus_backend.py"))
    return out


def _check_run(run: Path) -> tuple[dict, list[str]]:
    s = S.load_state(run)
    problems = []
    if s.get("deploy_choice") != "pegasus":
        problems.append("Gate 4 did not choose the Pegasus target for this run")
    elif s.get("stage") != "deploy":
        problems.append(f"the run is at stage {s.get('stage')!r}, not deploy")
    elif not s.get("workflow_id"):
        import execution as X
        problems.append(X.pre_version(s))
    else:
        problems += S.require_problems(s, "deploy")
    return s, problems


def _done(s: dict) -> list[str]:
    steps = ((s.get("pegasus") or {}).get("steps") or {})
    return [k for k in ("prepare", "copy", "launch") if (steps.get(k) or {}).get("ok")]


def _next(s: dict, st: dict):
    done = _done(s)
    return next((k for k in steps_for(st["host"]) if k not in done), None)


def _inputs(needed: list[str], inputs_dir) -> list[str]:
    if not needed:
        return []
    if not inputs_dir:
        return [f"this workflow needs input files ({', '.join(needed)}); give the folder that holds them "
                "with --inputs <folder>"]
    d = Path(inputs_dir)
    missing = [f for f in needed if not (d / f).is_file()]
    return [f"not in {d}: {', '.join(missing)}"] if missing else []


# ── preflight ────────────────────────────────────────────────────────────────

def preflight(run: str, inputs_dir=None) -> dict:
    run_p = Path(run).resolve()
    s, problems = _check_run(run_p)
    if problems:
        return {"ok": False, "problems": problems}
    st = _settings(s)
    changed = _changed(s)
    if changed:
        problems.append("the Pegasus settings changed since prepare ("
                        + "; ".join(f"{k}: pinned {a!r}, now {b!r}" for k, (a, b) in changed.items())
                        + "); set them back, or start a new run")
    lay = _layout(run_p, s, st)
    plan = P.plan(s, st)
    if not plan["ok"]:
        return {"ok": False, "problems": [f"cannot run on Pegasus: {p}" for p in plan["problems"]]}
    done, nxt = _done(s), _next(s, st)
    lines = [f"Pegasus run `{plan['name']}` on " + (f"`{st['host']}` (over ssh)" if st["host"] else "this machine")
             + f": {plan['jobs']} jobs, image `{st['image']}` ({st['image_source']}), runtime {st['runtime']}"]
    if "prepare" not in done:
        img = images.check_image(st["image"], st["runtime"], st["host"], runner=RUN)
        if not img["ok"]:
            problems.append(img["error"])

    code, out, err = _on(st["host"], "pegasus-version", timeout=60)
    if code != 0:
        problems.append(f"pegasus-version failed on {lay['where']} ({_tail(err, 2)}); "
                        "run /curate-skills:curate-doctor --pegasus")
    else:
        lines.append(f"- Pegasus {out.strip()} on {lay['where']}")

    if "prepare" in done:
        lines.append(f"- `{lay['ir_dir']}/`: written by this run (prepared)")
    else:
        state_ = "exists — refused" if lay["ir_dir"].exists() else "new"
        if lay["ir_dir"].exists():
            problems.append(f"{lay['ir_dir']} already exists; nothing is overwritten")
        lines.append(f"- writes `{lay['ir_dir']}/` (workflow.yml, functions/ {plan['functions']} files, inputs/): {state_}")
        problems += _inputs(plan["inputs"], inputs_dir)
    if st["host"]:
        if "copy" in done:
            lines.append(f"- `{st['host']}:{lay['shown_remote']}/`: written by this run (copied)")
        else:
            code, _, _ = _on(st["host"], f"test -e ~/{REMOTE_ROOT}/{q(lay['reference'])}", timeout=60)
            if code == 0:
                problems.append(f"{st['host']}:{lay['shown_remote']} already exists; nothing is overwritten")
            lines.append(f"- copies to `{st['host']}:{lay['shown_remote']}/` ({lay['ir_dir'].name}/, curate_ir/): "
                         + ("exists — refused" if code == 0 else "new"))
    if "launch" not in done:
        exists = Path(lay["work"]).exists() if not st["host"] else False
        if exists:
            problems.append(f"{lay['work']} already exists; nothing is overwritten")
        lines.append(f"- submits to HTCondor; Pegasus work folder `{lay['work']}/`: " + ("exists — refused" if exists else "new"))
    lines.append("- input files: " + (", ".join(f"`{f}`" for f in plan["inputs"]) + f" from `{inputs_dir}`"
                                     if plan["inputs"] else "none"))
    lines.append("- delivered when it finishes: " + ", ".join(f"`{d}`" for d in plan["deliverables"]))
    lines += [f"- note: {w}" for w in plan["warnings"]]
    lines.append("- steps: " + " → ".join(steps_for(st["host"])) + (f" (done: {', '.join(done)})" if done else ""))
    return {"ok": not problems, "problems": problems, "summary": "\n".join(lines), "next_step": nxt,
            "steps": list(steps_for(st["host"])), "done": done, "name": plan["name"]}


# ── the steps ────────────────────────────────────────────────────────────────

def _plan(step: str, run_p: Path, s: dict, st: dict, lay: dict) -> dict:
    where = st["host"] or "this machine"
    if step == "prepare":
        paths = [f"{lay['ir_dir']}/workflow.yml", f"{lay['ir_dir']}/functions/", f"{lay['ir_dir']}/inputs/"]
    elif step == "copy":
        paths = [f"{st['host']}:{lay['shown_remote']}/{lay['ir_dir'].name}/", f"{st['host']}:{lay['shown_remote']}/curate_ir/"]
    else:
        paths = [f"{lay['work']}/ (Pegasus work folder, submit/ run folder)"]
    return {"step": step, "what": STEP_WHAT[step], "where": where, "paths": paths}


def step(run: str, name: str, confirmed=False, overwrite_own=False, inputs_dir=None) -> dict:
    run_p = Path(run).resolve()
    s, problems = _check_run(run_p)
    base = {"step": name, "run": str(run_p)}
    if problems:
        return {**base, "ok": False, "problems": problems}
    st = _settings(s)
    if _changed(s):
        return {**base, "ok": False, "problems": ["the Pegasus settings changed since prepare; set them back"]}
    if name not in steps_for(st["host"]):
        return {**base, "ok": False, "problems": [f"{name} is not a step on {st['host'] or 'this machine'}"]}
    nxt = _next(s, st)
    if name != nxt:
        return {**base, "ok": False, "problems": [f"{name} is already done" if name in _done(s)
                                                   else f"the next step is {nxt}"]}
    lay = _layout(run_p, s, st)
    plan = _plan(name, run_p, s, st, lay)
    if not confirmed:
        return {**base, "ok": False, "needs_confirmation": True, "plan": plan}
    try:
        result = {"prepare": _prepare, "copy": _copy, "launch": _launch}[name](
            run_p, s, st, lay, overwrite_own, inputs_dir)
    except StepError as e:
        _update(run_p, lambda x: _set_step(x, name, False, str(e)))
        return {**base, "ok": False, "error": str(e), "retry_with": "--overwrite-own"}
    s = _update(run_p, lambda x: (_set_step(x, name, True, None), x["pegasus"].update(result.get("record", {})),
                                  x.update(execution=result["execution"]) if result.get("execution") else None,
                                  x.update(stage="monitor", begun=None) if name == "launch" else None))
    done = _done(s)
    out = {**base, "ok": True, "progress": " · ".join(STEP_DONE[k] for k in steps_for(st["host"]) if k in done),
           "next_step": _next(s, st), "stage": s["stage"]}
    if result.get("message"):
        out["message"] = result["message"]
    return out


def _set_step(s: dict, name: str, ok: bool, error):
    rec = s.get("pegasus") or {}
    rec.setdefault("steps", {})[name] = {"ok": ok, "ts": _now(), "error": error}
    s["pegasus"] = rec


def _prepare(run_p, s, st, lay, overwrite_own, inputs_dir) -> dict:
    ir_dir = lay["ir_dir"]
    if ir_dir.exists():
        if not (overwrite_own and _own(run_p, "prepare", "local", ir_dir)):
            raise StepError(f"{ir_dir} already exists; nothing is overwritten"
                            + ("" if overwrite_own else " (if this run's prepare wrote it, retry with --overwrite-own)"))
        shutil.rmtree(ir_dir)
    return _prepare_versioned(run_p, s, st, lay, inputs_dir, WorkflowSpec.model_validate(s["spec"]))


def _prepare_versioned(run_p, s, st, lay, inputs_dir, spec) -> dict:
    """Build the Pegasus folder from the approved version (cwl_pegasus), copy the input files into it,
    then write the execution's manifest (execution.finish_pegasus)."""
    import cwl_pegasus
    import execution as X
    ex = s["execution"]
    ir_dir = lay["ir_dir"]
    name = P.run_ir_name(spec.name, ex["run_id"])
    try:
        built = X.pegasus_built(s, run_p, name, st["image"])
    except X.ExecutionError as e:
        raise StepError(str(e))
    missing = _inputs(built["needed"], inputs_dir)
    if missing:
        raise StepError(missing[0])
    _log(run_p, "prepare", "local", ir_dir)
    cwl_pegasus.write_folder(ir_dir, built)
    for f in built["needed"]:
        shutil.copy2(Path(inputs_dir) / f, ir_dir / "inputs" / f)
    v = cwl_pegasus.validate_folder(ir_dir)
    if v["errors"]:
        raise StepError("the Pegasus workflow folder is not valid: " + " | ".join(v["errors"]))
    return {"execution": X.finish_pegasus(s, ex), "record": {"name": name, "host": st["host"], "runtime": st["runtime"], "image": st["image"],
                       "image_source": st["image_source"], "reference": ex["reference"],
                       "ir_dir": str(ir_dir), "inputs_from": str(Path(inputs_dir).resolve()) if inputs_dir else None,
                       "jobs": v["jobs"]},
            "message": f"wrote {ir_dir} ({v['jobs']} jobs, {ex['reference']}); validated"}


def _copy(run_p, s, st, lay, overwrite_own, inputs_dir) -> dict:
    host = st["host"]
    code, out, err = _on(host, "cd && pwd", timeout=60)
    if code != 0 or not out.strip().startswith("/"):
        raise StepError(f"could not reach {host} ({_tail(err, 2)})")
    remote = f"{out.strip()}/{REMOTE_ROOT}/{lay['reference']}"
    code, _, _ = _on(host, f"test -e {q(remote)}", timeout=60)
    if code == 0:
        if not (overwrite_own and _own(run_p, "copy", host, remote)):
            raise StepError(f"{host}:{remote} already exists; nothing is overwritten")
        code, _, err = _on(host, f"rm -rf {q(remote)}", timeout=120)
        if code != 0:
            raise StepError(f"could not remove this run's own {host}:{remote} ({_tail(err, 2)})")
    code, _, err = _on(host, f"mkdir -p {q(str(Path(remote).parent))} && mkdir {q(remote)}", timeout=60)
    if code != 0:
        raise StepError(f"could not create {host}:{remote} ({_tail(err, 2)})")
    _log(run_p, "copy", host, remote)
    code, _, err = RUN(["scp", "-r", "-q", *SSH_OPTS, str(lay["ir_dir"]), str(IR_TOOLS), f"{host}:{remote}/"],
                       timeout=600)
    if code != 0:
        raise StepError(f"scp to {host}:{remote} failed ({_tail(err, 3)})")
    return {"record": {"remote_dir": remote}, "message": f"copied to {host}:{remote}"}


def _launch(run_p, s, st, lay, overwrite_own, inputs_dir) -> dict:
    host, work = st["host"], lay["work"]
    where = host or "local"
    code, _, _ = _on(host, f"test -e {q(work)}", timeout=60)
    if code == 0:
        if not (overwrite_own and _own(run_p, "launch", where, work)):
            raise StepError(f"{work} already exists; nothing is overwritten")
        code, _, err = _on(host, f"rm -rf {q(work)}", timeout=120)
        if code != 0:
            raise StepError(f"could not remove this run's own {work} ({_tail(err, 2)})")
    _log(run_p, "launch", where, work)
    cmd = (f"{PEGASUS_PY} {q(lay['backend'])} submit {q(lay['workflow_on_host'])} --work {q(work)} "
           f"--runtime {q(st['runtime'])}")
    code, out, err = _on(host, cmd, timeout=600)
    if code != 0:
        raise StepError(f"submit failed on {host or 'this machine'}: {_tail(err or out)}")
    try:
        rec = json.loads(out[out.index("{"):])
    except ValueError:
        raise StepError(f"submit gave no record: {_tail(out)}")
    return {"record": {"work_dir": rec["work"], "run_dir": rec["run_dir"], "submitted_at": rec["submitted_at"],
                       "dagman_cluster": rec.get("dagman_cluster")},
            "message": f"submitted `{rec['workflow']}` to HTCondor"
                       + (f" (DAGMan cluster {rec['dagman_cluster']})" if rec.get("dagman_cluster") else "")
                       + f"; Pegasus run folder {rec['run_dir']}"}


def status(run: str) -> dict:
    run_p = Path(run).resolve()
    s = S.load_state(run_p)
    st = _settings(s)
    rec = s.get("pegasus") or {}
    return {"ok": True, "stage": s.get("stage"), "deploy_choice": s.get("deploy_choice"),
            "steps": list(steps_for(st["host"])), "done": _done(s), "next_step": _next(s, st),
            "pegasus": rec, "log": read_log(run_p)}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="The Pegasus target's deploy steps")
    sub = p.add_subparsers(dest="cmd", required=True)
    q_ = sub.add_parser("preflight")
    q_.add_argument("--run", required=True, help=RUN_HELP)
    q_.add_argument("--inputs")
    for name in ("prepare", "copy", "launch"):
        q_ = sub.add_parser(name)
        q_.add_argument("--run", required=True, help=RUN_HELP)
        q_.add_argument("--confirmed", action="store_true")
        q_.add_argument("--overwrite-own", action="store_true")
        if name == "prepare":
            q_.add_argument("--inputs")
    sub.add_parser("status").add_argument("--run", required=True, help=RUN_HELP)
    a = p.parse_args(argv)
    if runstate.resolve_args(a, "run", note=True):  # <workflow-id>/<run-id>, a run id or a folder; a session: its run
        return 2
    if not (Path(a.run) / "state.json").is_file():
        print(json.dumps({"ok": False, "error": f"no run at {a.run}"}))
        return 2
    if a.cmd == "preflight":
        out = preflight(a.run, a.inputs)
    elif a.cmd == "status":
        out = status(a.run)
    else:
        out = step(a.run, a.cmd, a.confirmed, a.overwrite_own, getattr(a, "inputs", None))
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
