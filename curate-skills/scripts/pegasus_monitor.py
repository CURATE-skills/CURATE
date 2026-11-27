"""curate-monitor for a Pegasus run (deploy_choice "pegasus"); monitor.py poll dispatches here.

The workflow was submitted by pegasus_deploy.py launch and runs under HTCondor's DAGMan;
state.pegasus.run_dir names its Pegasus run folder, so this works after a logout or reboot.
Each call polls for about a minute with the backend's read-only `status` (DAGMan's exit line and
jobstate.log), here or over ssh, saves its progress in state.monitor and returns, like the FaaSr
monitor. When DAGMan has finished it runs the backend's `collect` (copies the declared outputs to
<work>/deliver/ and writes <work>/run-report.json, on the Pegasus host) and, for an ssh host,
fetches both into <run>/pegasus/result/. Then:

  success  DAGMan exit 0 and every declared output delivered -> stage finalize
  failure  a job failed, DAGMan exited non-zero, or an output is missing -> stage stopped
  timeout  the time budget ran out while DAGMan is still running -> stage stopped; the workflow
           keeps running, and `curate-monitor --run` re-checks it with a fresh budget

It never submits, removes or changes the workflow. Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

import hostcmd

SCRIPT_DIR = Path(__file__).resolve().parent
IR_TOOLS = SCRIPT_DIR.parent / "curate_ir"
DEFAULT_MINUTES = 10
DEFAULT_INTERVAL = 10
DEFAULT_CHUNK = 60
MAX_CALL_SECONDS = 540
NOTE = ("Output correctness is not judged: the monitor checks that the workflow finished and that each "
        "declared output was delivered.")
RUN = hostcmd.run  # tests replace this


class InputError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def q(x) -> str:
    return shlex.quote(str(x))


def _load(run: Path) -> dict:
    import runstate
    return runstate.load(run)


def _update(run: Path, change) -> dict:
    """Change the run's state (runstate.update: an execution's records go to its execution run)."""
    import runstate
    return runstate.update(run, change)


def _setting(flag, env_var, config, default, cast):
    for v, src in ((flag, "flag"), (os.environ.get(env_var), env_var), (config, "plugin setting")):
        if v not in (None, "") and not str(v).startswith("${"):
            return cast(v), src
    return default, "default"


# ── the backend on the Pegasus host ──────────────────────────────────────────

def _where(rec: dict) -> dict:
    """Paths on the Pegasus host for the backend's status/collect."""
    if rec.get("host"):
        root = rec["remote_dir"]
        folder = Path(rec.get("ir_dir") or "bundle").name  # the Pegasus folder (the execution run's bundle/)
        return {"host": rec["host"], "workflow": f"{root}/{folder}/workflow.yml", "work": rec["work_dir"],
                "backend": f"{root}/curate_ir/backends/pegasus_backend.py"}
    return {"host": None, "workflow": str(Path(rec["ir_dir"]) / "workflow.yml"), "work": rec["work_dir"],
            "backend": str(IR_TOOLS / "backends" / "pegasus_backend.py")}


def _backend(w: dict, cmd: str, timeout=300) -> dict:
    code, out, err = hostcmd.on(w["host"], f"python3 {q(w['backend'])} {cmd} {q(w['workflow'])} --work {q(w['work'])}",
                                runner=RUN, timeout=timeout)
    try:
        return json.loads(out[out.index("{"):])
    except ValueError:
        raise InputError(f"the backend's {cmd} gave no result on {w['host'] or 'this machine'} "
                         f"(exit {code}): {hostcmd.tail(err or out, 4)}")


def local_dir(run: Path, s: dict) -> Path:
    """Where this run keeps its Pegasus results and report: the execution run's folder for a
    versioned run (<workflow-id>/runs/<run-id>/), else <run>/pegasus/."""
    ex = s.get("execution") or {}
    if s.get("workflow_id") and ex.get("target") == "pegasus" and ex.get("dir"):
        return Path(ex["dir"])
    return Path(run) / "pegasus"


def _fetch(run: Path, w: dict, s: dict | None = None) -> Path:
    """An ssh host's run-report.json and deliver/ -> <local>/result/ (this run's own folder)."""
    dest = local_dir(run, s or {}) / "result"
    dest.mkdir(parents=True, exist_ok=True)
    for src in (f"{w['work']}/run-report.json", f"{w['work']}/deliver"):
        code, _, err = RUN(["scp", "-r", "-q", *hostcmd.SSH_OPTS, f"{w['host']}:{src}", str(dest) + "/"], timeout=600)
        if code != 0 and src.endswith(".json"):
            raise InputError(f"could not fetch {w['host']}:{src} ({hostcmd.tail(err, 2)})")
    return dest


# ── lines and summaries ──────────────────────────────────────────────────────

def groups(jobs: dict) -> dict[str, list[str]]:
    """{step: [its copies' states]} in workflow order (copies are "<step>__<value>")."""
    out: dict[str, list[str]] = {}
    for jid, st in jobs.items():
        out.setdefault(jid.split("__")[0], []).append(st)
    return out


def step_line(step: str, states: list[str]) -> str | None:
    n, done = len(states), states.count("done")
    of = f" ({done}/{n})" if n > 1 else ""
    if "failed" in states:
        return f"✗ {step} failed" + (f" ({states.count('failed')}/{n} copies)" if n > 1 else "")
    if "held" in states:
        return f"! {step} held by HTCondor{of}"
    if done == n:
        return f"✓ {step} succeeded" + (f" ({n}/{n})" if n > 1 else "")
    if any(x in ("running", "queued") for x in states):
        return f"… {step} running{of}"
    return None


def _failed_steps(jobs: dict) -> list[str]:
    return [k for k, v in groups(jobs).items() if "failed" in v]


def summary_md(rec: dict, result: str, st: dict, report: dict | None, deliver: str | None,
               out_dir: str | None = None) -> str:
    where = f"`{rec['host']}`" if rec.get("host") else "this machine"
    counts = st.get("counts") or {}
    n = sum(counts.values())
    text = {"success": "success", "failure": "failure", "timeout": "not finished yet", "pending": "running"}[result]
    out = [f"**Pegasus run** `{rec.get('name')}` on {where}: {text} — {counts.get('done', 0)}/{n} jobs done"
           + (f", DAGMan exit {st['dagman_exit']}" if st.get("dagman_exit") is not None else ""),
           f"- Image: `{rec.get('image')}`" + (f" ({rec['image_source']})" if rec.get("image_source") else ""),
           f"- Pegasus run folder: `{rec['run_dir']}`"]
    if report:
        files = [f"`{d}`" for d in report.get("delivered") or []]
        out.append(f"- Results in `{out_dir or deliver}/`: " + (", ".join(files) or "nothing")
                   + (" (partial; marked FAILED)" if out_dir and result == "failure" else ""))
        if out_dir:
            out.append(f"- Full Pegasus work folder: `{rec.get('work_dir')}/`" + (f" on {rec['host']}" if rec.get("host") else ""))
        if report.get("missing"):
            out.append("- Missing outputs: " + ", ".join(f"`{m}`" for m in report["missing"]))
    failed = _failed_steps(st.get("jobs") or {})
    if failed:
        out.append("- Failed steps: " + ", ".join(f"`{f}`" for f in failed)
                   + f" (details: `pegasus-analyzer {rec['run_dir']}`" + (f" on {rec['host']})" if rec.get("host") else ")"))
    if result in ("success", "failure"):
        out.append(f"- {NOTE}")
    return "\n".join(out)


def report_md(rec: dict, result: str, st: dict, report: dict | None, deliver: str | None, minutes_used: float,
              out_dir: str | None = None) -> str:
    out = [f"# Pegasus monitor report — {rec.get('name')}", "",
           summary_md(rec, result, st, report, deliver, out_dir), "",
           f"- Submitted: {rec.get('submitted_at')}; DAGMan cluster {rec.get('dagman_cluster')}",
           f"- Monitor time used: {minutes_used:.1f} min", "", "| Job | State |", "|---|---|"]
    out += [f"| `{j}` | {s} |" for j, s in (st.get("jobs") or {}).items()]
    if _failed_steps(st.get("jobs") or {}):
        out += ["", f"Job logs are in `{rec['run_dir']}/00/00/` (`<step>_<job>.out.000`, `.err.000`); "
                    f"`pegasus-analyzer {rec['run_dir']}` summarises the failures."]
    return "\n".join(out)


# ── the visible output folder ────────────────────────────────────────────────

def _unique(base: Path) -> Path:
    """`base` if it is free (missing or empty), else `base-2`, `base-3`, … (as emit's bundle folder)."""
    candidate, n = base, 2
    while candidate.is_dir() and any(candidate.iterdir()):
        candidate, n = Path(f"{base}-{n}"), n + 1
    return candidate


def output_folder(run_p: Path, s: dict, mon: dict, deliver: str, report_file: str, result: str, why: str) -> str:
    """<project>/pegasus_output/<workflow>-<run_id>/: the delivered files (results/ ...) and
    run-report.json, next to faasr_output/; a FAILED.txt marker when the run failed (its outputs
    are partial). Copied once per launch: the folder recorded for this launch is reused; a new
    launch gets a new folder (-2, -3 ...). Never overwrites anything."""
    prev = mon.get("output_dir")
    if prev and Path(prev).is_dir():
        return prev
    project = Path(s.get("project_dir") or run_p.parents[2])
    name = re.sub(r"[^a-zA-Z0-9_-]", "_", (s.get("spec") or {}).get("name") or "workflow")
    dest = _unique(project / "pegasus_output" / f"{name}-{re.sub(r'[^a-zA-Z0-9_-]', '_', s['run_id'])}")
    if deliver and Path(deliver).is_dir():
        shutil.copytree(deliver, dest, dirs_exist_ok=True)  # dest is new or empty
    else:
        dest.mkdir(parents=True, exist_ok=True)
    if report_file and Path(report_file).is_file():
        shutil.copy2(report_file, dest / "run-report.json")
    if result != "success":
        (dest / "FAILED.txt").write_text(
            f"This Pegasus run FAILED: {why or 'see run-report.json'}.\n"
            "The files here are the outputs it delivered before failing (partial).\n"
            f"Details: {run_p / 'pegasus' / 'monitor-report.md'}\n")
    return str(dest)


# ── poll ─────────────────────────────────────────────────────────────────────

def poll(run: str, chunk=None, report_only=False, minutes=None, interval=None, config_minutes=None,
         clock=time.monotonic, sleep=time.sleep) -> dict:
    run_p = Path(run).resolve()
    s = _load(run_p)
    rec = s.get("pegasus") or {}
    if not ((rec.get("steps") or {}).get("launch") or {}).get("ok") or not rec.get("run_dir"):
        raise InputError("this Pegasus run was not launched (pegasus_deploy.py launch has not run)")
    record = not report_only and s.get("stage") == "monitor"
    mon = dict(s.get("monitor") or {})
    if mon.get("target") != "pegasus" or mon.get("run_dir") != rec["run_dir"]:
        mon = {}
    if record and mon.get("result") == "success":
        s = _update(run_p, lambda x: x.update(stage="finalize", begun=None))
        return {"ok": True, "result": "success", "no_op": True, "stage": s["stage"], "lines": [],
                "summary": mon.get("summary", ""), "next_skill": "curate-skills:wda-finalize"}
    recheck = None
    if record and mon.get("result") in ("failure", "timeout"):
        recheck = mon["result"]  # the same workflow, a fresh budget; every step's line again
        mon.update(result="pending", seconds_used=0, shown={})
    settings = dict(mon.get("settings") or {})
    settings_changed = None
    if settings and minutes not in (None, "") and float(minutes) != settings["minutes"]:
        settings_changed = {"minutes": [settings["minutes"], float(minutes)]}
        settings.update(minutes=float(minutes), minutes_source="flag (changed on resume)")
    if not settings:
        m, m_src = _setting(minutes, "CURATE_MONITOR_MINUTES", config_minutes, DEFAULT_MINUTES, float)
        i, i_src = _setting(interval, "CURATE_MONITOR_INTERVAL", None, DEFAULT_INTERVAL, float)
        settings = {"minutes": m, "minutes_source": m_src, "interval": i, "interval_source": i_src}
    used = float(mon.get("seconds_used") or 0)
    call_budget = min(chunk or DEFAULT_CHUNK, MAX_CALL_SECONDS, max(settings["minutes"] * 60 - used, 0))
    shown = dict(mon.get("shown") or {}) if record else {}
    w = _where(rec)

    start, polls, lines = clock(), 0, []
    while True:
        st = _backend(w, "status")
        polls += 1
        for step, states in groups(st["jobs"]).items():
            line = step_line(step, states)
            if line and shown.get(step) != line:
                lines.append(line)
                shown[step] = line
        if st["state"] != "running" or clock() - start + settings["interval"] > call_budget:
            break
        sleep(settings["interval"])
    used += clock() - start

    report = deliver = None
    if st["state"] == "running":
        result = "timeout" if used >= settings["minutes"] * 60 - 1e-6 else "pending"
    elif not record:
        result = st["state"]  # report only: never collect
    else:
        report = _backend(w, "collect")
        if w["host"]:
            dest = _fetch(run_p, w, s)
            deliver = str(dest / "deliver")
            report_file = str(dest / "run-report.json")
        else:
            deliver = f"{w['work']}/deliver"
            report_file = f"{w['work']}/run-report.json"
        ok = st["state"] == "success" and report.get("status") == "success" and not report.get("missing")
        result = "success" if ok else "failure"
    why = ([f"step {', '.join(_failed_steps(st['jobs']))} failed"] if _failed_steps(st["jobs"]) else []) \
        + ([f"DAGMan exit {st['dagman_exit']}"] if st.get("dagman_exit") not in (None, 0) else []) \
        + ([f"missing outputs: {', '.join((report or {}).get('missing') or [])}"] if (report or {}).get("missing") else [])
    out_dir = None
    if report is not None:  # once per launch: a re-check of a copied launch reuses its folder
        out_dir = output_folder(run_p, s, mon, deliver, report_file, result, "; ".join(why))
    if result == "success":
        lines.append(f"✓ workflow succeeded ({st['counts'].get('done', 0)} jobs; results in {out_dir})")
    elif result == "failure":
        lines.append("✗ workflow failed: " + ("; ".join(why) or "see the details")
                     + (f"; partial outputs in {out_dir}" if out_dir else ""))
    elif result == "timeout":
        lines.append(f"… not finished after {settings['minutes']:g} min (still running under DAGMan)")
    summary = summary_md(rec, result, st, report, deliver, out_dir)
    out = {"ok": result not in ("failure", "timeout"), "result": result, "lines": lines,
           "mode": "recording" if record else "report only", "polls": polls, "minutes_used": round(used / 60, 2),
           "settings": settings, "summary": summary, "plot_note": NOTE, "jobs": st["jobs"],
           **({"rechecked": recheck} if recheck else {}),
           **({"settings_changed": settings_changed} if settings_changed else {})}
    if result in ("timeout", "failure"):
        out.update(resume_command=f"/curate-skills:curate --resume {s['run_id']}",
                   recheck_command=f"/curate-skills:curate-monitor --run {s['run_id']}")
    if not record:
        out["report"] = report_md(rec, result, st, report, deliver, used / 60, out_dir)
        return out

    details = local_dir(run_p, s) / "monitor-report.md"
    details.parent.mkdir(parents=True, exist_ok=True)
    details.write_text(report_md(rec, result, st, report, deliver, used / 60, out_dir) + "\n")
    out["details"] = str(details)

    def change(x):
        prev = x.get("monitor") or {}
        x["monitor"] = {
            "target": "pegasus", "run_dir": rec["run_dir"], "settings": settings, "seconds_used": round(used, 1),
            "polls": int(prev.get("polls") or 0) + polls if prev.get("target") == "pegasus" else polls,
            "result": result, "finished": result in ("success", "failure"), "success": result == "success",
            "accepted": None, "failed": _failed_steps(st["jobs"]), "jobs": st["jobs"], "dagman_exit": st["dagman_exit"],
            "summary": summary, "shown": shown, "report": str(details),
            **({"deliver_dir": deliver, "run_report": report_file, "delivered": report.get("delivered"),
                "missing": report.get("missing"), "output_dir": out_dir} if report else {}),
            "ts": _now()}
        if result == "pending":
            return
        decision = {"success": "succeeded", "failure": "failed", "timeout": "timed-out"}[result]
        text = f"Pegasus run {rec.get('name')}: {result}" + (f" (re-checked after {recheck})" if recheck else "")
        x.setdefault("history", []).append({"gate": "monitor", "decision": decision, "structural": None,
                                            "text": text, "ts": _now()})
        if result == "success":
            x.update(stage="finalize", begun=None)
        else:
            x.update(stage="stopped", stopped_from="monitor", begun=None)
    s = _update(run_p, change)
    out["stage"] = s["stage"]
    if s["stage"] == "finalize":
        out["next_skill"] = "curate-skills:wda-finalize"
    return out
