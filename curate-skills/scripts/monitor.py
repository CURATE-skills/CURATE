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

Nothing is judged while any Actions run in the window is queued or in progress, and every run of
an action counts (a fan-in step may run once per predecessor). Outputs still stale or missing get
GRACE_SECONDS after the action's last run before they count as not written.

Results: success / success_unconfirmed (Gate 5 asks the user to accept it) / failure / pending
(timeout after `monitor_minutes`). `poll` returns after `--chunk-seconds` with `lines` (one per
change, per action) for the stage to print; the full report goes to deploy/monitor-report.md.
After a failure or timeout, polling again re-checks the same invocation and replaces the verdict.
It never writes to GitHub or S3. It records state.json only for a run at stage `monitor`; for any
other run it is report-only and writes nothing.
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
import envfile  # noqa: E402
import leakcheck  # noqa: E402
from vendor.faasr_runtime.artifacts import build_artifact_list  # noqa: E402

FAILED_RX = re.compile(r"\[[\d\.]+?\] \[ERROR\]")  # vendored faasr_function.FaaSrFunction.failed_regex
BAD_CONCLUSIONS = {"failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale"}
MAX_CALL_SECONDS = 540  # one call stays under the 10-minute Bash limit
DEFAULT_CHUNK = 60  # seconds per call: the stage prints progress between calls
GRACE_SECONDS = 60  # outputs still stale or missing this long after an action's last run finished: failed
REASON_MAX = 160
ERRORISH_RX = re.compile(r"error|exception|traceback|failed", re.I)
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


def _graph(wf: dict) -> tuple[dict, dict]:
    """(rank count per action named in an InvokeNext, successors per action)."""
    ranks, succ = {}, {}
    for a, d in (wf.get("ActionList") or {}).items():
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
    return ranks, succ


def units(wf: dict) -> list[dict]:
    """Each action rank in workflow order (topological, entry first), as CURATE's
    WorkflowRunner names them: `action` or `action(r)` (ranks from InvokeNext, as FaaSr_py's
    build_adjacency_graph reads them)."""
    actions = wf.get("ActionList") or {}
    ranks, succ = _graph(wf)
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

def _predecessors(wf: dict) -> dict[str, list[str]]:
    _, succ = _graph(wf)
    return {a: [p for p, ns in succ.items() if a in ns] for a in (wf.get("ActionList") or {})}


def _clean(line: str) -> str:
    line = re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s*", "", line.strip())  # Actions log timestamp
    line = re.sub(r"^(##\[error\]|\[[\d.]+\] \[ERROR\]\s*)", "", line).strip()
    return line if len(line) <= REASON_MAX else line[:REASON_MAX - 1] + "…"


def one_line_reason(bad: dict | None, err: dict | None) -> str:
    """Why an action failed, in one line: its FaaSr [ERROR] line, else the job log's last error-like
    line, else the Actions conclusion. From redacted excerpts only."""
    if err:
        hit = next((x for x in reversed(err.get("excerpt") or []) if FAILED_RX.search(x)), None)
        if hit and _clean(hit):
            return _clean(hit)
    if bad:
        hit = next((x for x in reversed(bad.get("excerpt") or []) if ERRORISH_RX.search(x) and _clean(x)), None)
        return _clean(hit) if hit else f"its Actions run ended with {bad['conclusion']}"
    return "[ERROR] in its FaaSr log"


def poll_once(ctx: dict, matches: dict) -> dict:
    """A snapshot of the invocation and a verdict per action, in workflow order.

    An action has succeeded when none of its Actions runs is queued or in progress, every run of it
    for this invocation concluded `success` (a fan-in step may run once per predecessor: all of them
    count, not just the first), its predecessors have succeeded, and its declared outputs were
    written after the dispatch. Outputs that are still stale or missing are waited for for
    GRACE_SECONDS after the action's last run finished before the action counts as failed. The
    invocation is never judged while any run is queued or in progress. `matches` caches each
    run's classification."""
    wf, dep, gh, s3, extra = ctx["wf"], ctx["deploy"], ctx["gh"], ctx["s3"], ctx["extra"]
    now = ctx.get("now", lambda: datetime.now(timezone.utc))()
    inv, since = dep["invocation_id"], _iso(dep["dispatched_at"]) - WINDOW_BEFORE
    counts = rank_counts(wf)
    preds = _predecessors(wf)
    actions = []
    for action in dict.fromkeys(u["action"] for u in units(wf)):
        wf_file = f"{dep['deploy_name']}-{action}.yml"
        runs, active = [], 0
        for r in list_runs(gh, dep["repo"], wf_file, since):
            key = str(r["id"])
            m = matches.get(key)
            if r.get("status") != "completed":
                active += 1  # not matchable yet (its log is incomplete): wait for it whoever's it is
                continue
            if m is None:
                hit, excerpt = job_log_matches(gh, ctx["opener"], dep["repo"], r["id"], inv, extra)
                m = {"action": action, "match": {True: "confirmed", False: "other", None: "time_only"}[hit],
                     "excerpt": excerpt if r.get("conclusion") in BAD_CONCLUSIONS else []}
                matches[key] = m
            if m["match"] == "other":
                continue  # another invocation's run
            runs.append({"id": r["id"], "status": r.get("status"), "conclusion": r.get("conclusion"),
                         "url": r.get("html_url"), "created_at": r.get("created_at"),
                         "updated_at": r.get("updated_at") or r.get("created_at"),
                         "match": m["match"], "excerpt": m.get("excerpt", [])})
        ok = sum(x["conclusion"] == "success" for x in runs)
        actions.append({"action": action, "expected": counts[action], "runs": runs, "in_progress": active,
                        "succeeded": ok, "extra_runs": max(0, len(runs) - counts[action])})

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

    dispatched = _iso(dep["dispatched_at"])
    outputs_now = []
    for o in declared_outputs(wf):
        h = s3.head(o["key"])
        state = ("missing" if h is None else
                 "stale" if h.get("last_modified") and _iso(h["last_modified"]) < dispatched else "present")
        outputs_now.append({**o, "state": state, **({"size": h.get("size"), "last_modified": h.get("last_modified")}
                                                     if h else {})})

    verdicts, failed = {}, None
    for a in actions:
        name = a["action"]
        fa = [f for f in faasr if f["action"] == name]
        bad = next((r for r in a["runs"] if r["conclusion"] in BAD_CONCLUSIONS), None)
        err = next((f for f in fa if f["status"] == "error"), None)
        v = {"action": name, "expected": a["expected"], "done": min(a["succeeded"], a["expected"])}
        if bad or err:
            v.update(state="failed", reason=one_line_reason(bad, err))
            failed = failed or {
                "action": name, "label": (err or {}).get("label") or name, "reason": v["reason"],
                "why": (f"Actions run concluded {bad['conclusion']}" if bad else "")
                       + (" and " if bad and err else "") + ("[ERROR] in its FaaSr log" if err else ""),
                "url": (bad or {}).get("url"), "excerpt": (err or {}).get("excerpt") or (bad or {}).get("excerpt") or [],
                "excerpt_from": "FaaSr log" if err and err.get("excerpt") else "job log (redacted)"}
        elif (a["in_progress"] or a["succeeded"] < a["expected"]
              or any(verdicts[p]["state"] != "succeeded" for p in preds.get(name, []) if p in verdicts)):
            started = a["runs"] or a["in_progress"] or any(f["status"] != "not started" for f in fa)
            v["state"] = "running" if started else "waiting"
        else:
            unwritten = [o for o in outputs_now if o["action"] == name and o["state"] != "present"]
            last = max(_iso(r["updated_at"]) for r in a["runs"])
            if not unwritten:
                v["state"] = "succeeded"
            elif (now - last).total_seconds() < GRACE_SECONDS:
                v.update(state="running", waiting_for_outputs=[o["key"] for o in unwritten])
            else:
                files = [o["key"].rsplit("/", 1)[-1] for o in unwritten]
                v.update(state="no_output", files=files, reason="succeeded but did not write " + ", ".join(files))
                failed = failed or {
                    "action": name, "label": name, "reason": v["reason"],
                    "why": "declared output(s) " + ", ".join(f"{o['key']} ({o['state']})" for o in unwritten),
                    "url": None, "excerpt": [], "excerpt_from": None}
        verdicts[name] = v

    active = sum(a["in_progress"] for a in actions)
    actions_ok = not active and all(a["succeeded"] >= a["expected"] and all(r["conclusion"] == "success"
                                                                           for r in a["runs"]) for a in actions)
    outputs_ok = all(o["state"] == "present" for o in outputs_now) if actions_ok else None
    unconfirmed = any(r["match"] == "time_only" for a in actions for r in a["runs"])
    if active:
        result = "pending"  # never judged while any run is queued or in progress
    elif failed:
        result = "failure"
    elif all(v["state"] == "succeeded" for v in verdicts.values()):
        result = "success_unconfirmed" if unconfirmed else "success"
    else:
        result = "pending"
    return {"result": result, "actions": actions, "verdicts": list(verdicts.values()), "faasr": faasr,
            "outputs": outputs_now if actions_ok else [], "outputs_now": outputs_now, "actions_ok": actions_ok,
            "outputs_ok": outputs_ok, "failed": failed, "unconfirmed": unconfirmed, "in_progress": active,
            "invocation_folder": folder}


# ── what the user sees ───────────────────────────────────────────────────────

def function_line(v: dict) -> str | None:
    """One line per action: running / ✓ succeeded / ✗ failed; a count for fan-out steps."""
    name, n = v["action"], (f" ({v['done']}/{v['expected']})" if v["expected"] > 1 else "")
    return {"running": f"… {name} running{n}", "succeeded": f"✓ {name} succeeded{n}",
            "failed": f"✗ {name} failed: {v.get('reason')}",
            "no_output": f"✗ {name} {v.get('reason')}"}.get(v["state"])


def final_line(result: str, snap: dict, minutes: float) -> str | None:
    f = snap.get("failed") or {}
    if result == "success":
        return "✓ workflow succeeded"
    if result == "success_unconfirmed":
        return "✓ workflow succeeded (some runs matched by time only: Gate 5 asks you to accept them)"
    if result == "failure":
        return f"✗ workflow failed: {f.get('label') or f.get('action')}: {f.get('reason')}"
    if result == "timeout":
        busy = [v["action"] for v in snap["verdicts"] if v["state"] in ("running", "waiting")]
        return f"… not finished after {minutes:g} min" + (f": {', '.join(busy)} still running" if busy else "")
    return None


def status_block(snap: dict, result: str, minutes: float) -> str:
    """The current state of every action plus the verdict: what finalize shows."""
    lines = [x for x in (function_line(v) for v in snap["verdicts"]) if x]
    end = final_line(result, snap, minutes)
    return "\n".join(lines + ([end] if end else []) + ["", f"_{PLOT_NOTE}_"])


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


def poll(run: str, chunk: int | None = None, report_only: bool = False, minutes=None, interval=None,
         config_minutes=None, opener=None, s3_factory=None, clock=time.monotonic, sleep=time.sleep,
         now=None) -> dict:
    """Poll for up to `chunk` seconds (default DEFAULT_CHUNK), save progress, return. While the
    result is `pending` the stage calls again, until a verdict or the `minutes` budget is spent.

    `lines` holds one line per change, per action, since the previous call (and the verdict at the
    end); the full details go to deploy/monitor-report.md and state.json."""
    run_p = Path(run)
    s, _ = D.load_run(run)
    record = _recording(s, report_only)
    mon = dict(s.get("monitor") or {})
    if record and mon.get("result") in ("success", "success_unconfirmed"):
        # Resume after success: nothing to poll again.
        s = D._update_state(run_p, lambda st: st.update(stage="finalize", begun=None))
        return {"ok": True, "result": mon["result"], "no_op": True, "stage": s["stage"],
                "lines": [], "summary": mon.get("summary", ""), "next_skill": "curate-skills:wda-finalize"}

    s, ctx, extra = _context(run, opener, s3_factory)
    if now is not None:
        ctx["now"] = now
    dep = ctx["deploy"]
    if record and mon.get("invocation_id") not in (None, dep["invocation_id"]):
        mon = {}  # a new deployment of this run: start over
    recheck = None
    if record and mon.get("result") in ("failure", "timeout"):
        # Re-check the SAME invocation (nothing is redeployed) with a fresh budget; the new verdict
        # replaces the old one. Every action's line is shown again.
        recheck = mon["result"]
        mon.update(result="pending", seconds_used=0, shown={})
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
    call_budget = min(chunk or DEFAULT_CHUNK, MAX_CALL_SECONDS, max(remaining_total, 0))
    matches = dict(mon.get("matches") or {})
    shown = dict(mon.get("shown") or {}) if record else {}

    start, polls, interim, lines = clock(), 0, [], []
    while True:
        snap = poll_once(ctx, matches)
        polls += 1
        interim.append(_interim(snap, round(used + clock() - start)))
        for v in snap["verdicts"]:
            line = function_line(v)
            if line and shown.get(v["action"]) != line:
                lines.append(line)
                shown[v["action"]] = line
        if snap["result"] != "pending" or clock() - start + settings["interval"] > call_budget:
            break
        sleep(settings["interval"])
    used += clock() - start
    timed_out = snap["result"] == "pending" and used >= settings["minutes"] * 60 - 1e-6
    result = "timeout" if timed_out else snap["result"]
    end = final_line(result, snap, settings["minutes"])
    if end:
        lines.append(end)
    mode = "recording" if record else "report only"
    summary = status_block(snap, result, settings["minutes"])
    report = summary_md({**snap, "result": result}, dep, mode, used / 60, interim)
    resume = f"/curate-skills:curate --resume {s['run_id']}"
    out = {"ok": result not in ("failure", "timeout"), "result": result, "lines": lines, "mode": mode,
           "polls": polls, "minutes_used": round(used / 60, 2), "settings": settings,
           "actions_ok": snap["actions_ok"], "outputs_ok": snap["outputs_ok"], "unconfirmed": snap["unconfirmed"],
           "failed": snap["failed"], "summary": summary, "plot_note": PLOT_NOTE,
           **({"rechecked": recheck} if recheck else {}),
           **({"settings_changed": settings_changed} if settings_changed else {})}
    if result in ("timeout", "failure"):
        out.update(resume_command=resume, recheck_command=f"/curate-skills:curate-monitor --run {s['run_id']}")
    if not record:
        out["report"] = report
        return out

    logs_dir = run_p / "deploy" / "monitor"
    logs_dir.mkdir(parents=True, exist_ok=True)
    for f in snap["faasr"]:  # FaaSr's own S3 logs, redacted; job logs are never saved
        if f["log"]:
            (logs_dir / (f["label"].replace("(", ".").replace(")", "") + ".txt")).write_text(f["log"])
    report_path = run_p / "deploy" / "monitor-report.md"  # beside the logs folder, which holds FaaSr's logs only
    report_path.write_text(report + "\n")
    out["details"] = str(report_path)

    record_snap = {k: v for k, v in snap.items() if k not in ("faasr", "outputs_now")}
    record_snap["faasr"] = [{k: v for k, v in f.items() if k != "log"} for f in snap["faasr"]]
    record_snap["outputs_now"] = [{k: v for k, v in o.items()} for o in snap["outputs_now"]]

    def change(st):
        st["monitor"] = {
            "invocation_id": dep["invocation_id"], "settings": settings, "seconds_used": round(used, 1),
            "polls": int((st.get("monitor") or {}).get("polls") or 0) + polls, "matches": matches,
            "result": result, "finished": result != "pending" and result != "timeout",
            "success": result in ("success", "success_unconfirmed"), "accepted": None,
            "failed": snap["failed"], "snapshot": record_snap, "summary": summary, "shown": shown,
            "poll_log": (((st.get("monitor") or {}).get("poll_log") or []) + interim)[-POLL_LOG_MAX:],
            "logs_dir": str(logs_dir), "report": str(report_path), "ts": D._now_iso()}
        if result == "pending":
            return
        decision = {"success": "succeeded", "success_unconfirmed": "succeeded-unconfirmed",
                    "failure": "failed", "timeout": "timed-out"}[result]
        text = f"invocation {dep['invocation_id']}: {RESULT_TEXT[result]}"
        if recheck:
            text += f" (re-checked after {RESULT_TEXT[recheck]})"
        st.setdefault("history", []).append({"gate": "monitor", "decision": decision, "structural": None,
                                             "text": text, "ts": D._now_iso()})
        if result in ("success", "success_unconfirmed"):
            st.update(stage="finalize", begun=None)
        else:  # failure or timeout: stop; `curate-monitor --run` re-checks the same invocation
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

def main(argv=None, opener=None, s3_factory=None, clock=time.monotonic, sleep=time.sleep, now=None) -> int:
    p = argparse.ArgumentParser(prog="monitor.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("poll", help="poll the run's invocation (read-only), within a time budget")
    q.add_argument("--run", required=True)
    q.add_argument("--chunk-seconds", "--budget", dest="chunk", type=int,
                   help=f"poll up to this long, save progress, return (default {DEFAULT_CHUNK}; at most "
                        f"{MAX_CALL_SECONDS})")
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
    envfile.load()  # ~/.config/curate/.env fills deploy variables the environment leaves unset
    token, _ = D.get_token() if args.cmd == "poll" else (None, None)
    extra = {"gh_token": token} if token else None
    try:
        if args.cmd == "poll":
            out = poll(args.run, args.chunk, args.report_only, args.minutes, args.interval,
                       args.config_minutes, opener, s3_factory, clock, sleep, now)
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
