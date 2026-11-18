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

import fcntl
import json
import os
import shlex
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
    return json.loads((run / "state.json").read_text())


def _update(run: Path, change) -> dict:
    """The same lock spec.py and deploy.py hold (<run>/state.lock) around every state.json change."""
    with open(run / "state.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            path = run / "state.json"
            s = json.loads(path.read_text())
            change(s)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(s, indent=2) + "\n")
            tmp.replace(path)
            return s
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


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
        return {"host": rec["host"], "ir": f"{root}/ir/ir.yaml", "work": rec["work_dir"],
                "backend": f"{root}/curate_ir/backends/pegasus_backend.py"}
    return {"host": None, "ir": str(Path(rec["ir_dir"]) / "ir.yaml"), "work": rec["work_dir"],
            "backend": str(IR_TOOLS / "backends" / "pegasus_backend.py")}


def _backend(w: dict, cmd: str, timeout=300) -> dict:
    code, out, err = hostcmd.on(w["host"], f"python3 {q(w['backend'])} {cmd} {q(w['ir'])} --work {q(w['work'])}",
                                runner=RUN, timeout=timeout)
    try:
        return json.loads(out[out.index("{"):])
    except ValueError:
        raise InputError(f"the backend's {cmd} gave no result on {w['host'] or 'this machine'} "
                         f"(exit {code}): {hostcmd.tail(err or out, 4)}")


def _fetch(run: Path, w: dict) -> Path:
    """An ssh host's run-report.json and deliver/ -> <run>/pegasus/result/ (this run's own folder)."""
    dest = run / "pegasus" / "result"
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


def summary_md(rec: dict, result: str, st: dict, report: dict | None, deliver: str | None) -> str:
    where = f"`{rec['host']}`" if rec.get("host") else "this machine"
    counts = st.get("counts") or {}
    n = sum(counts.values())
    text = {"success": "success", "failure": "failure", "timeout": "not finished yet", "pending": "running"}[result]
    out = [f"**Pegasus run** `{rec.get('name')}` on {where}: {text} — {counts.get('done', 0)}/{n} jobs done"
           + (f", DAGMan exit {st['dagman_exit']}" if st.get("dagman_exit") is not None else ""),
           f"- Pegasus run folder: `{rec['run_dir']}`"]
    if report:
        files = [f"`{d}`" for d in report.get("delivered") or []]
        out.append(f"- Delivered to `{deliver}/`: " + (", ".join(files) or "nothing"))
        if report.get("missing"):
            out.append("- Missing outputs: " + ", ".join(f"`{m}`" for m in report["missing"]))
    failed = _failed_steps(st.get("jobs") or {})
    if failed:
        out.append("- Failed steps: " + ", ".join(f"`{f}`" for f in failed)
                   + f" (details: `pegasus-analyzer {rec['run_dir']}`" + (f" on {rec['host']})" if rec.get("host") else ")"))
    if result in ("success", "failure"):
        out.append(f"- {NOTE}")
    return "\n".join(out)


def report_md(rec: dict, result: str, st: dict, report: dict | None, deliver: str | None, minutes_used: float) -> str:
    out = [f"# Pegasus monitor report — {rec.get('name')}", "", summary_md(rec, result, st, report, deliver), "",
           f"- Submitted: {rec.get('submitted_at')}; DAGMan cluster {rec.get('dagman_cluster')}",
           f"- Monitor time used: {minutes_used:.1f} min", "", "| Job | State |", "|---|---|"]
    out += [f"| `{j}` | {s} |" for j, s in (st.get("jobs") or {}).items()]
    if _failed_steps(st.get("jobs") or {}):
        out += ["", f"Job logs are in `{rec['run_dir']}/00/00/` (`<step>_<job>.out.000`, `.err.000`); "
                    f"`pegasus-analyzer {rec['run_dir']}` summarises the failures."]
    return "\n".join(out)


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
            dest = _fetch(run_p, w)
            deliver = str(dest / "deliver")
            report_file = str(dest / "run-report.json")
        else:
            deliver = f"{w['work']}/deliver"
            report_file = f"{w['work']}/run-report.json"
        ok = st["state"] == "success" and report.get("status") == "success" and not report.get("missing")
        result = "success" if ok else "failure"
    if result == "success":
        lines.append(f"✓ workflow succeeded ({st['counts'].get('done', 0)} jobs; results in {deliver})")
    elif result == "failure":
        why = ([f"step {', '.join(_failed_steps(st['jobs']))} failed"] if _failed_steps(st["jobs"]) else []) \
            + ([f"DAGMan exit {st['dagman_exit']}"] if st.get("dagman_exit") not in (None, 0) else []) \
            + ([f"missing outputs: {', '.join((report or {}).get('missing') or [])}"] if (report or {}).get("missing") else [])
        lines.append("✗ workflow failed: " + ("; ".join(why) or "see the details"))
    elif result == "timeout":
        lines.append(f"… not finished after {settings['minutes']:g} min (still running under DAGMan)")
    summary = summary_md(rec, result, st, report, deliver)
    out = {"ok": result not in ("failure", "timeout"), "result": result, "lines": lines,
           "mode": "recording" if record else "report only", "polls": polls, "minutes_used": round(used / 60, 2),
           "settings": settings, "summary": summary, "plot_note": NOTE, "jobs": st["jobs"],
           **({"rechecked": recheck} if recheck else {}),
           **({"settings_changed": settings_changed} if settings_changed else {})}
    if result in ("timeout", "failure"):
        out.update(resume_command=f"/curate-skills:curate --resume {s['run_id']}",
                   recheck_command=f"/curate-skills:curate-monitor --run {s['run_id']}")
    if not record:
        out["report"] = report_md(rec, result, st, report, deliver, used / 60)
        return out

    details = run_p / "pegasus" / "monitor-report.md"
    details.parent.mkdir(parents=True, exist_ok=True)
    details.write_text(report_md(rec, result, st, report, deliver, used / 60) + "\n")
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
                "missing": report.get("missing")} if report else {}),
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
