# /// script
# requires-python = "==3.13.*"
# dependencies = ["FaaSr_py==0.1.13", "PyGithub>=2.8.1", "requests"]  # FaaSr_py pins boto3
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Follows CURATE-prototype's run monitor (src/faasr_agents/faasr/runtime/, vendored in
# scripts/vendor/faasr_runtime; commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). See scripts/VENDORED.md.
"""curate-monitor: watch one deployed invocation, read-only.

For the run's InvocationID it finds, per action and in workflow order:
  - the GitHub Actions run(s) of `.github/workflows/<deploy name>-<action>.yml` (GET only). A run
    counts as this invocation's when its job log contains the InvocationID (searched in memory;
    raw job-log text is never saved or printed). Each run's log is fetched once; the match is
    cached. A finished run whose log is unavailable is matched by time only ("unconfirmed").
  - the FaaSr log and `.done` marker in S3 (FaaSrLog/<name>/<timestamp>/<InvocationID>/).
  - every output file the deploy copy declares (`output<N>` under the action's `folder`); a file
    counts only if it was written at or after the dispatch (older files are "stale").

Results: success / success_unconfirmed (Gate 5 asks the user to accept it) / failure / pending
(timeout after `monitor_minutes`). It never writes to GitHub or S3. It records state.json only
for a run at stage `monitor`; for any other run it is report-only and writes nothing.
Prints JSON with "ok"; exit 1 = needs attention, 2 = bad input.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import deploy as D  # noqa: E402
import leakcheck  # noqa: E402
from vendor.faasr_runtime.artifacts import build_artifact_list  # noqa: E402

FAILED_RX = re.compile(r"\[[\d\.]+?\] \[ERROR\]")  # vendored faasr_function.FaaSrFunction.failed_regex
BAD_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale"}
MAX_CALL_SECONDS = 540  # one call stays under the 10-minute Bash limit
DEFAULT_MINUTES = 10
DEFAULT_INTERVAL = 10  # seconds, as CURATE's WorkflowRunner(check_interval=10)
WINDOW_BEFORE = timedelta(minutes=2)  # clock skew allowance before dispatched_at
EXCERPT_LINES = 20
POLL_LOG_MAX = 200  # interim poll records kept in state.monitor.poll_log
PLOT_NOTE = "Output correctness (e.g. the plot) is not judged: the monitor only checks that each declared file exists and is fresh."


class InputError(Exception):
    pass


# ── the workflow's units and outputs ─────────────────────────────────────────

def _rank_of(entry: str) -> tuple[str, int]:
    m = re.fullmatch(r"(.+?)\((\d+)\)", entry.strip())
    return (m.group(1), int(m.group(2))) if m else (entry.strip(), 1)


def units(wf: dict) -> list[dict]:
    """Each action rank in workflow order (topological, entry first), as CURATE's
    WorkflowRunner names them: `action` or `action(r)` (ranks from InvokeNext, as FaaSr_py's
    build_adjacency_graph reads them)."""
    actions = wf.get("ActionList") or {}
    ranks, succ = {}, {}
    for a, d in actions.items():
        nxt = d.get("InvokeNext") or []
        nxt = [nxt] if isinstance(nxt, str) else nxt
        names = []
        for e in nxt:
            for item in (e.values() if isinstance(e, dict) else [e]):
                for x in ([item] if isinstance(item, str) else item):
                    n, r = _rank_of(x)
                    ranks[n] = max(ranks.get(n, 1), r)
                    names.append(n)
        succ[a] = names
    # Topological order (each action after all its predecessors), entry action first.
    preds = {a: {p for p, ns in succ.items() if a in ns} for a in actions}
    ready = sorted((a for a in actions if not preds[a]), key=lambda a: a != wf.get("FunctionInvoke"))
    order = []
    while ready:
        a = ready.pop(0)
        order.append(a)
        for n in succ.get(a, []):
            if n in preds:
                preds[n].discard(a)
                if not preds[n] and n not in order and n not in ready:
                    ready.append(n)
    order += [a for a in actions if a not in order]
    out = []
    for a in order:
        n = ranks.get(a, 1)
        out += [{"action": a, "rank": r if n > 1 else None, "label": f"{a}({r})" if n > 1 else a}
                for r in range(1, n + 1)]
    return out


def rank_counts(wf: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for u in units(wf):
        counts[u["action"]] = counts.get(u["action"], 0) + 1
    return counts


def declared_outputs(wf: dict) -> list[dict]:
    """Every output<N> of every action under its folder (CURATE's artifacts.build_artifact_list),
    with {rank} expanded to 1..N: N = the producer's rank count, or, for an unranked producer,
    the largest rank count among its successors."""
    counts = rank_counts(wf)
    out = []
    for art in build_artifact_list(wf):
        key = art["s3_key"]
        if "{rank}" not in key:
            out.append({"action": art["action"], "key": key})
            continue
        n = counts.get(art["action"], 1)
        if n <= 1:
            nxt = (wf["ActionList"][art["action"]].get("InvokeNext") or [])
            nxt = [nxt] if isinstance(nxt, str) else nxt
            n = max([counts.get(_rank_of(x)[0], 1) for x in nxt if isinstance(x, str)] or [1])
        out += [{"action": art["action"], "key": key.replace("{rank}", str(r))} for r in range(1, n + 1)]
    return out


def invocation_folder(wf: dict, invocation_id: str, invocation_timestamp: str) -> str:
    """FaaSr_py helpers.s3_helper_functions.get_invocation_folder."""
    return f"{wf.get('FaaSrLog') or 'FaaSrLog'}/{wf['WorkflowName']}/{invocation_timestamp}/{invocation_id}"


def log_key(folder: str, label: str) -> str:  # vendored FaaSrFunctionLogger.logs_key
    return f"{folder}/{label}.txt"


def done_key(folder: str, label: str) -> str:  # vendored FaaSrFunction.done_key
    return f"{folder}/function_completions/{label.replace('(', '.').replace(')', '')}.done"


# ── read-only S3 ─────────────────────────────────────────────────────────────

class S3Reader:
    """head / get only (no writes), on a client built as the vendored s3_client builds it."""

    def __init__(self, client, bucket: str):
        self._c, self.bucket = client, bucket

    def head(self, key: str) -> dict | None:
        try:
            r = self._c.head_object(Bucket=self.bucket, Key=key)
        except Exception as e:  # noqa: BLE001
            code = str((getattr(e, "response", None) or {}).get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        lm = r.get("LastModified")
        return {"size": r.get("ContentLength"), "last_modified": lm.isoformat() if hasattr(lm, "isoformat") else lm}

    def get_bytes(self, key: str) -> bytes:
        return self._c.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def text(self, key: str) -> str | None:
        if self.head(key) is None:
            return None
        return self._c.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8", "replace")


def s3_reader(wf: dict) -> S3Reader:
    store = wf.get("DefaultDataStore") or "S3"
    cfg = (wf.get("DataStores") or {}).get(store) or {}
    keys, _, problems = D.s3_keys(cfg)
    if not keys:
        raise InputError("S3 cannot be read: " + "; ".join(problems))
    return S3Reader(D.boto3_s3(cfg, *keys), cfg.get("Bucket"))


# ── read-only GitHub Actions ─────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # never follow: the token must not go to another host
        return None


def default_opener():
    return urllib.request.build_opener(_NoRedirect()).open


def _iso(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def list_runs(gh: D.GitHub, repo: str, wf_file: str, since: datetime) -> list[dict]:
    st, _, body = gh.get(f"/repos/{repo}/actions/workflows/{wf_file}/runs", event="workflow_dispatch",
                         created=">=" + since.strftime("%Y-%m-%dT%H:%M:%SZ"), per_page=100)
    if st == 404:
        return []
    if st != 200:
        raise InputError(f"could not list Actions runs of {wf_file} (HTTP {st})")
    return sorted((body or {}).get("workflow_runs") or [], key=lambda r: (r.get("created_at") or "", r["id"]))


def job_log_matches(gh: D.GitHub, opener, repo: str, run_id: int, needle: str, extra: dict | None
                    ) -> tuple[bool | None, list[str]]:
    """(True/False: the job log does / doesn't contain needle; None: no log available), and a
    redacted excerpt (last lines) for later use if the run failed. The raw text is not kept."""
    st, _, jobs = gh.get(f"/repos/{repo}/actions/runs/{run_id}/jobs")
    if st != 200 or not (jobs or {}).get("jobs"):
        return None, []
    found, excerpt = False, []
    for job in jobs["jobs"]:
        st, headers, _ = gh.get(f"/repos/{repo}/actions/jobs/{job['id']}/logs")
        location = (headers or {}).get("Location") or (headers or {}).get("location")
        if st not in (301, 302, 303, 307) or not location:
            return None, []
        try:  # the signed URL needs no token, and must not get one
            with opener(urllib.request.Request(location, headers={"User-Agent": "curate-skills"}), timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError):
            return None, []
        found = found or needle in text
        excerpt = leakcheck.redact(text, extra_values=extra).splitlines()[-EXCERPT_LINES:]
        del text
    return found, excerpt


# ── one poll ─────────────────────────────────────────────────────────────────

def poll_once(ctx: dict, matches: dict) -> dict:
    """A snapshot of the invocation: per action its matched runs, per rank its FaaSr status, and
    (once every expected run concluded) the outputs. `matches` caches each run's classification."""
    wf, dep, gh, s3, extra = ctx["wf"], ctx["deploy"], ctx["gh"], ctx["s3"], ctx["extra"]
    inv, since = dep["invocation_id"], _iso(dep["dispatched_at"]) - WINDOW_BEFORE
    counts = rank_counts(wf)
    actions = []
    for action in dict.fromkeys(u["action"] for u in units(wf)):
        wf_file = f"{dep['deploy_name']}-{action}.yml"
        runs = []
        for r in list_runs(gh, dep["repo"], wf_file, since):
            key = str(r["id"])
            m = matches.get(key)
            done = r.get("status") == "completed"
            if m is None and done:
                hit, excerpt = job_log_matches(gh, ctx["opener"], dep["repo"], r["id"], inv, extra)
                m = {"action": action, "match": {True: "confirmed", False: "other", None: "time_only"}[hit],
                     "excerpt": excerpt if r.get("conclusion") in BAD_CONCLUSIONS else []}
                matches[key] = m
            if m and m["match"] == "other":
                continue  # another invocation's run
            runs.append({"id": r["id"], "status": r.get("status"), "conclusion": r.get("conclusion"),
                         "url": r.get("html_url"), "created_at": r.get("created_at"),
                         "match": m["match"] if m else "pending", "excerpt": (m or {}).get("excerpt", [])})
        confirmed = [x for x in runs if x["match"] == "confirmed"]
        time_only = [x for x in runs if x["match"] == "time_only"]
        pending = [x for x in runs if x["match"] == "pending"]
        chosen = (confirmed + time_only)[:counts[action]]
        actions.append({"action": action, "expected": counts[action], "runs": chosen,
                        "in_progress": len(pending), "extra_runs": max(0, len(confirmed + time_only) - counts[action])})

    folder = invocation_folder(wf, inv, dep["invocation_timestamp"])
    faasr = []
    for u in units(wf):
        text = s3.text(log_key(folder, u["label"]))
        done = s3.head(done_key(folder, u["label"])) is not None
        status = ("error" if text and FAILED_RX.search(text) else "completed" if done
                  else "running" if text else "not started")
        excerpt = (leakcheck.redact(text, extra_values=extra).splitlines()[-EXCERPT_LINES:]
                   if status == "error" else [])
        faasr.append({"label": u["label"], "action": u["action"], "status": status, "excerpt": excerpt,
                      "log": leakcheck.redact(text, extra_values=extra) if text else None})

    failed = None
    for a in actions:
        bad = next((r for r in a["runs"] if r["conclusion"] in BAD_CONCLUSIONS), None)
        err = next((f for f in faasr if f["action"] == a["action"] and f["status"] == "error"), None)
        if bad or err:
            failed = {"action": a["action"], "label": (err or {}).get("label") or a["action"],
                      "why": (f"Actions run concluded {bad['conclusion']}" if bad else "")
                             + (" and " if bad and err else "") + ("[ERROR] in its FaaSr log" if err else ""),
                      "url": (bad or {}).get("url"),
                      "excerpt": (err or {}).get("excerpt") or (bad or {}).get("excerpt") or [],
                      "excerpt_from": "FaaSr log" if err and err.get("excerpt") else "job log (redacted)"}
            break

    all_concluded = all(len(a["runs"]) == a["expected"] and all(r["status"] == "completed" for r in a["runs"])
                        for a in actions)
    actions_ok = all_concluded and all(r["conclusion"] == "success" for a in actions for r in a["runs"])
    # Output states every poll (shown as interim progress); judged only once every Actions run succeeded.
    dispatched = _iso(dep["dispatched_at"])
    outputs_now = []
    for o in declared_outputs(wf):
        h = s3.head(o["key"])
        state = ("missing" if h is None else
                 "stale" if h.get("last_modified") and _iso(h["last_modified"]) < dispatched else "present")
        outputs_now.append({**o, "state": state, **({"size": h.get("size"), "last_modified": h.get("last_modified")}
                                                     if h else {})})
    outputs, outputs_ok = [], None
    if actions_ok and not failed:
        outputs = outputs_now
        outputs_ok = all(o["state"] == "present" for o in outputs)
        if not outputs_ok:
            bad = [o for o in outputs if o["state"] != "present"]
            failed = {"action": bad[0]["action"], "label": bad[0]["action"],
                      "why": "declared output(s) " + ", ".join(f"{o['key']} ({o['state']})" for o in bad),
                      "url": None, "excerpt": [], "excerpt_from": None}

    unconfirmed = any(r["match"] == "time_only" for a in actions for r in a["runs"])
    if failed:
        result = "failure"
    elif actions_ok and outputs_ok:
        result = "success_unconfirmed" if unconfirmed else "success"
    else:
        result = "pending"
    return {"result": result, "actions": actions, "faasr": faasr, "outputs": outputs, "outputs_now": outputs_now,
            "actions_ok": actions_ok, "outputs_ok": outputs_ok, "failed": failed,
            "unconfirmed": unconfirmed, "invocation_folder": folder}


# ── the report ───────────────────────────────────────────────────────────────

def _interim(snap: dict, seconds: int) -> dict:
    """One poll, compactly: Actions runs found/concluded and each output's state at that moment."""
    expected = sum(a["expected"] for a in snap["actions"])
    runs = [r for a in snap["actions"] for r in a["runs"]]
    counts = {}
    for o in snap["outputs_now"]:
        counts[o["state"]] = counts.get(o["state"], 0) + 1
    return {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seconds": seconds,
            "runs_found": len(runs), "runs_concluded": sum(r["status"] == "completed" for r in runs),
            "runs_expected": expected, "outputs": counts,
            "output_states": {o["key"]: o["state"] for o in snap["outputs_now"]}}


RESULT_TEXT = {"success": "success", "success_unconfirmed": "success, unconfirmed match",
               "failure": "FAILURE", "pending": "not finished", "timeout": "not finished (timed out)"}


def summary_md(snap: dict, dep: dict, mode: str, minutes_used: float | None = None,
               interim: list[dict] | None = None) -> str:
    lines = [f"**Monitor — {RESULT_TEXT[snap['result']]}** ({mode}; read-only: nothing was written to "
             "GitHub or S3)", "",
             f"- Deployment `{dep['deploy_name']}` in `{dep['repo']}`, invocation `{dep['invocation_id']}` "
             f"(dispatched {dep['dispatched_at']})"]
    if minutes_used is not None:
        lines.append(f"- Monitored for {minutes_used:.1f} min")
    lines += ["", "| Step | Action run(s) | Conclusion | Match | FaaSr log |", "|---|---|---|---|---|"]
    for a in snap["actions"]:
        fa = [f for f in snap["faasr"] if f["action"] == a["action"]]
        runs = a["runs"]
        concl = ", ".join(r["conclusion"] or r["status"] for r in runs) or "—"
        match = ", ".join({"confirmed": "InvocationID", "time_only": "time only"}[r["match"]] for r in runs) or "—"
        found = f"{len(runs)}/{a['expected']}" + (f" (+{a['in_progress']} in progress)" if a["in_progress"] else "")
        lines.append(f"| `{a['action']}` | {found} | {concl} | {match} | "
                     + ", ".join(f["status"] for f in fa) + " |")
    lines += ["", f"- **Actions:** {'all succeeded' if snap['actions_ok'] else 'not all succeeded yet' if not snap['failed'] else 'FAILED'}"]
    if snap["outputs"]:
        ok = sum(o["state"] == "present" for o in snap["outputs"])
        lines.append(f"- **Outputs:** {ok}/{len(snap['outputs'])} declared files present and fresh "
                     f"(written after the dispatch)")
        lines += [f"  - `{o['key']}` — {o['state']}" + (f", {o.get('size')} bytes" if o.get("size") is not None else "")
                  for o in snap["outputs"]]
    else:
        lines.append("- **Outputs:** not checked yet (checked once every Actions run has succeeded)")
    if snap["unconfirmed"]:
        lines.append("- **Unconfirmed match:** some runs were matched by time only (their job logs were not "
                     "available to confirm the InvocationID); Gate 5 asks you to accept this before finalize.")
    if snap["failed"]:
        f = snap["failed"]
        lines += ["", f"**Failed: `{f['label']}`** — {f['why']}" + (f" ({f['url']})" if f.get("url") else "")]
        if f.get("excerpt"):
            lines += ["", f"Last lines of its {f['excerpt_from']}:", "", "```", *f["excerpt"], "```"]
    if interim:
        shown = interim if len(interim) <= 12 else interim[:6] + interim[-6:]
        lines += ["", f"Polls this call ({len(interim)}" + (", first and last 6 shown" if len(interim) > 12 else "")
                  + "): outputs are judged only after every run succeeded; before that, files from an earlier "
                    "invocation show as stale.", "",
                  "| Poll | At (s) | Runs concluded | Outputs present / stale / missing |", "|---|---|---|---|"]
        for i, p in enumerate(shown):
            n = i + 1 if len(interim) <= 12 or i < 6 else len(interim) - (len(shown) - i) + 1
            o = p["outputs"]
            lines.append(f"| {n} | {p['seconds']} | {p['runs_concluded']}/{p['runs_expected']} | "
                         f"{o.get('present', 0)} / {o.get('stale', 0)} / {o.get('missing', 0)} |")
    lines += ["", f"_{PLOT_NOTE}_"]
    return "\n".join(lines)


# ── poll command ─────────────────────────────────────────────────────────────

def _setting(flag, env_var: str, config, default, cast):
    for v, src in ((flag, "flag"), (os.environ.get(env_var), env_var), (config, "plugin setting")):
        if v not in (None, "") and not str(v).startswith("${"):
            return cast(v), src
    return default, "default"


def _context(run: str, opener, s3_factory) -> tuple[dict, dict, dict]:
    s, _ = D.load_run(run)
    dep = s.get("deploy") or {}
    if not dep.get("invocation_id") or not dep.get("dispatched_at"):
        raise InputError("this run has no deployed invocation (deploy.py invoke has not run)")
    wf_path = Path(run) / "deploy" / "workflow.json"
    if not wf_path.is_file():
        raise InputError(f"{wf_path} not found (the deploy copy written by the upload step)")
    wf = json.loads(wf_path.read_text())
    token, _ = D.get_token()
    if not token:
        raise InputError("no GitHub token: set GH_PAT (as for CURATE) or log in with `gh auth login`")
    opener = opener or default_opener()
    ctx = {"wf": wf, "deploy": dep, "gh": D.GitHub(token, opener), "opener": opener,
           "extra": {"gh_token": token}, "s3": (s3_factory or s3_reader)(wf)}
    return s, ctx, ctx["extra"]


def _recording(s: dict, report_only: bool) -> bool:
    return not report_only and s.get("stage") == "monitor"


def poll(run: str, budget: int | None = None, report_only: bool = False, minutes=None, interval=None,
         config_minutes=None, opener=None, s3_factory=None, clock=time.monotonic, sleep=time.sleep) -> dict:
    run_p = Path(run)
    s, _ = D.load_run(run)
    record = _recording(s, report_only)
    mon = dict(s.get("monitor") or {})
    if record and mon.get("result") in ("success", "success_unconfirmed"):
        # Resume after success: nothing to poll again.
        s = D._update_state(run_p, lambda st: st.update(stage="finalize", begun=None))
        return {"ok": True, "result": mon["result"], "no_op": True, "stage": s["stage"],
                "summary": mon.get("summary", ""), "next_skill": "curate-skills:wda-finalize"}
    if record and mon.get("result") == "failure":
        return {"ok": False, "result": "failure", "no_op": True, "stage": s["stage"],
                "summary": mon.get("summary", ""), "failed": mon.get("failed")}

    s, ctx, extra = _context(run, opener, s3_factory)
    dep = ctx["deploy"]
    if record and mon.get("invocation_id") not in (None, dep["invocation_id"]):
        mon = {}  # a new deployment of this run: start over
    if record and mon.get("result") == "timeout":
        mon.update(result="pending", seconds_used=0)  # a resumed run gets a fresh budget
    settings = dict(mon.get("settings") or {})
    settings_changed = None
    if settings and minutes not in (None, "") and float(minutes) != settings["minutes"]:
        # An explicit --minutes (e.g. on resume) replaces the pinned value; recorded.
        settings_changed = {"minutes": [settings["minutes"], float(minutes)]}
        settings.update(minutes=float(minutes), minutes_source="flag (changed on resume)")
    if not settings:
        m, m_src = _setting(minutes, "CURATE_MONITOR_MINUTES", config_minutes, DEFAULT_MINUTES, float)
        i, i_src = _setting(interval, "CURATE_MONITOR_INTERVAL", None, DEFAULT_INTERVAL, float)
        settings = {"minutes": m, "minutes_source": m_src, "interval": i, "interval_source": i_src}
    used = float(mon.get("seconds_used") or 0)
    remaining_total = settings["minutes"] * 60 - used
    call_budget = min(budget or MAX_CALL_SECONDS, MAX_CALL_SECONDS, max(remaining_total, 0))
    matches = dict(mon.get("matches") or {})

    start, polls, interim = clock(), 0, []
    while True:
        snap = poll_once(ctx, matches)
        polls += 1
        interim.append(_interim(snap, round(used + clock() - start)))
        if snap["result"] != "pending" or clock() - start + settings["interval"] > call_budget:
            break
        sleep(settings["interval"])
    used += clock() - start
    timed_out = snap["result"] == "pending" and used >= settings["minutes"] * 60 - 1e-6
    result = "timeout" if timed_out else snap["result"]
    mode = "recording" if record else "report only"
    summary = summary_md({**snap, "result": result}, dep, mode, used / 60, interim)
    resume = f"/curate-skills:curate --resume {s['run_id']}"
    out = {"ok": result not in ("failure", "timeout"), "result": result, "mode": mode,
           "polls": polls, "minutes_used": round(used / 60, 2), "settings": settings,
           "actions_ok": snap["actions_ok"], "outputs_ok": snap["outputs_ok"], "unconfirmed": snap["unconfirmed"],
           "failed": snap["failed"], "summary": summary, "plot_note": PLOT_NOTE, "interim": interim,
           **({"settings_changed": settings_changed} if settings_changed else {})}
    if result in ("timeout", "failure"):
        out.update(resume_command=resume, resume_stage_command=f"/curate-skills:curate-monitor --run {s['run_id']}")
    if not record:
        return out

    logs_dir = run_p / "deploy" / "monitor"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for f in snap["faasr"]:  # FaaSr's own S3 logs, redacted; job logs are never saved
        if f["log"]:
            (logs_dir / (f["label"].replace("(", ".").replace(")", "") + ".txt")).write_text(f["log"])

    record_snap = {k: v for k, v in snap.items() if k not in ("faasr", "outputs_now")}
    record_snap["faasr"] = [{k: v for k, v in f.items() if k != "log"} for f in snap["faasr"]]

    def change(st):
        st["monitor"] = {
            "invocation_id": dep["invocation_id"], "settings": settings, "seconds_used": round(used, 1),
            "polls": int((st.get("monitor") or {}).get("polls") or 0) + polls, "matches": matches,
            "result": result, "finished": result != "pending" and result != "timeout",
            "success": result in ("success", "success_unconfirmed"), "accepted": None,
            "failed": snap["failed"], "snapshot": record_snap, "summary": summary,
            "poll_log": (((st.get("monitor") or {}).get("poll_log") or []) + interim)[-POLL_LOG_MAX:],
            "logs_dir": str(logs_dir), "ts": D._now_iso()}
        if result == "pending":
            return
        decision = {"success": "succeeded", "success_unconfirmed": "succeeded-unconfirmed",
                    "failure": "failed", "timeout": "timed-out"}[result]
        st.setdefault("history", []).append({"gate": "monitor", "decision": decision, "structural": None,
                                             "text": f"invocation {dep['invocation_id']}: {RESULT_TEXT[result]}",
                                             "ts": D._now_iso()})
        if result in ("success", "success_unconfirmed"):
            st.update(stage="finalize", begun=None)
        else:  # failure or timeout: stop; resuming continues monitoring, never redeploys
            st.update(stage="stopped", stopped_from="monitor", begun=None)
    s = D._update_state(run_p, change)
    out["stage"] = s["stage"]
    if s["stage"] == "finalize":
        out["next_skill"] = "curate-skills:wda-finalize"
    return out


def accept_unconfirmed(run: str, confirmed: bool) -> dict:
    s, _ = D.load_run(run)
    mon = s.get("monitor") or {}
    if mon.get("result") != "success_unconfirmed":
        return {"ok": False, "error": f"nothing to accept: the monitor result is {mon.get('result')!r}"}
    if not confirmed:
        return {"ok": False, "needs_confirmation": True,
                "error": "accepting an unconfirmed match is the user's decision — ask them at Gate 5; only "
                         "after they choose Accept, run this again with --confirmed"}

    def change(st):
        st["monitor"]["accepted"] = {"ts": D._now_iso(), "by": "user"}
        st.setdefault("history", []).append({"gate": 5, "decision": "accepted-unconfirmed-match", "structural": None,
                                             "text": "the user accepted runs matched by time only", "ts": D._now_iso()})
    D._update_state(Path(run), change)
    return {"ok": True, "accepted": True}


def download_outputs(run: str, to: str, s3_factory=None) -> dict:
    """Copy the run's declared output files from S3 (get only) to a local folder outside the run,
    with a manifest of each file's size, LastModified and sha256. Writes nothing in the run."""
    import hashlib
    run_p, dest = Path(run).resolve(), Path(to).expanduser().resolve()
    if dest == run_p or run_p in dest.parents:
        raise InputError(f"{dest} is inside the run folder; download outside it")
    if dest.exists() and any(dest.iterdir()):
        raise InputError(f"{dest} is not empty; nothing is overwritten")
    s, _ = D.load_run(run)
    if not (s.get("deploy") or {}).get("invocation_id"):
        raise InputError("this run has no deployed invocation")
    wf = json.loads((run_p / "deploy" / "workflow.json").read_text())
    s3 = (s3_factory or s3_reader)(wf)
    files, skipped = [], []
    for o in declared_outputs(wf):
        h = s3.head(o["key"])
        if h is None:
            skipped.append({"key": o["key"], "reason": "missing"})
            continue
        data = s3.get_bytes(o["key"])
        leaked = leakcheck.find_leaks(data.decode("utf-8", "ignore"))
        if leaked:
            skipped.append({"key": o["key"], "reason": f"contains secret values ({', '.join(leaked)}); not saved"})
            continue
        target = dest / o["key"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files.append({"key": o["key"], "action": o["action"], "size": h.get("size"), "bytes": len(data),
                      "last_modified": h.get("last_modified"), "sha256": hashlib.sha256(data).hexdigest(),
                      "saved_as": str(target)})
    manifest = {"run": s["run_id"], "invocation_id": s["deploy"]["invocation_id"], "bucket": s3.bucket,
                "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "files": files,
                "skipped": skipped}
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return {"ok": not skipped, **manifest, "manifest": str(dest / "manifest.json")}


def status(run: str) -> dict:
    s, _ = D.load_run(run)
    mon = s.get("monitor") or {}
    return {"ok": True, "stage": s.get("stage"), "result": mon.get("result"), "accepted": mon.get("accepted"),
            "minutes_used": round(float(mon.get("seconds_used") or 0) / 60, 2), "settings": mon.get("settings"),
            "summary": mon.get("summary")}


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None, opener=None, s3_factory=None, clock=time.monotonic, sleep=time.sleep) -> int:
    p = argparse.ArgumentParser(prog="monitor.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("poll", help="poll the run's invocation (read-only), within a time budget")
    q.add_argument("--run", required=True)
    q.add_argument("--budget", type=int, help=f"seconds for this call (at most {MAX_CALL_SECONDS})")
    q.add_argument("--report-only", action="store_true", help="print the report; write nothing")
    q.add_argument("--minutes", help=f"total monitoring minutes (default {DEFAULT_MINUTES}; pinned per run)")
    q.add_argument("--interval", help=f"seconds between polls (default {DEFAULT_INTERVAL})")
    q.add_argument("--config-minutes", help="plugin setting value; empty = unset")
    sub.add_parser("status").add_argument("--run", required=True)
    q = sub.add_parser("download-outputs", help="copy the declared outputs from S3 to a local folder (read-only)")
    q.add_argument("--run", required=True)
    q.add_argument("--to", required=True, help="an empty folder outside the run")
    q = sub.add_parser("accept-unconfirmed", help="the user accepted runs matched by time only (Gate 5)")
    q.add_argument("--run", required=True)
    q.add_argument("--confirmed", action="store_true")
    args = p.parse_args(argv)
    token, _ = D.get_token() if args.cmd == "poll" else (None, None)
    extra = {"gh_token": token} if token else None
    try:
        if args.cmd == "poll":
            out = poll(args.run, args.budget, args.report_only, args.minutes, args.interval,
                       args.config_minutes, opener, s3_factory, clock, sleep)
        elif args.cmd == "status":
            out = status(args.run)
        elif args.cmd == "download-outputs":
            out = download_outputs(args.run, args.to, s3_factory)
        else:
            out = accept_unconfirmed(args.run, args.confirmed)
        return D._print(out, extra)
    except (InputError, D.InputError) as e:
        print(json.dumps({"ok": False, "error": leakcheck.redact(str(e), extra_values=extra)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
