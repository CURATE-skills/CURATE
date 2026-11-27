# /// script
# requires-python = "==3.13.*"
# dependencies = ["FaaSr_py==0.1.13", "PyGithub>=2.8.1", "requests", "pydantic==2.11.7", "cwl-utils==0.47", "pyyaml==6.0.2", "pegasus-wms.api==5.1.3"]  # FaaSr_py pins boto3
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# The deploy steps follow CURATE-prototype src/faasr_agents/agents/wda.py 56-157 and the
# vendored deploy/ modules (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). See scripts/VENDORED.md.
"""curate-deploy: put an approved bundle on FaaSr (GitHub Actions) the way CURATE does.

`preflight` is read-only: it reads the run, its bundle, (with a token) the GitHub repo and (with
S3 keys) the S3 bucket, and reports what deploying would write. Nothing is written anywhere.
The four steps — publish, upload, register, invoke (CURATE's order) — each write only with
--confirmed, which the stage file passes after the user said yes to that step; each re-runs the
preflight first and records every path it writes in the run's deploy-log.jsonl. The deploy copy
may differ from the approved bundle only in ALLOWED_DIFF fields and placeholder fields.

One deployment per version, one payload and S3 folder per run. The deployment is named after the
version, <Name> = its workflow id (+ $CURATE_DEPLOY_NAME_SUFFIX / --name-suffix if set), so CURATE's
own deployment of the workflow is never touched:
  functions/<Name>/<function>.py           the version's wrappers (a plain step also ships its
  functions/<Name>/<function>_main.py      unchanged function next to its wrapper)
  .github/workflows/<Name>-<action>.yml    its Actions files
  workflows/<Name>/deployment.json         its marker: version content hash, file shas, projects
Each run of the version adds only:
  workflows/<Name>/<run-id>.json           its payload (FaaSr reads PAYLOAD_URL = this file, by
                                           branch, at every action's start: it is never rewritten)
  S3: <Name>/<run-id>/...                  its outputs, and FaaSr's FaaSrLog/<Name>/<ts>/<invocation>/
publish and register write the deployment when the repo has none, and reuse it (writing nothing)
when its marker names this version and every file is byte-identical. A marker naming another
version is refused (tear it down, or set a suffix); a deployment of this version whose files differ
is refused with `update-deployment --workflow <wid>`, which re-pushes them after checking every
main() against the version. `list` shows each version's deployment recorded in this project's runs, with its runs;
`teardown --run` deletes one run's payload and S3 folders, `teardown --workflow <wid> --all` the
deployment and every run's payload and S3 folder, after the user's yes.
The deploy copy also fills emit's <FAASR_...> placeholders from the environment (non-secret
values: user, repo, S3 endpoint/bucket/region). The approved bundle is not changed.

Credentials come only from environment variables (CURATE's names) or `gh auth token`, read
inside this script; they are never printed, written or passed on a command line.
Prints JSON with "ok"; exit 1 = needs attention, 2 = bad input.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import envfile  # noqa: E402
import leakcheck  # noqa: E402
from runstate import RUN_HELP  # noqa: E402

NATIVE_CONTAINERS = SCRIPT_DIR / "vendor" / "curate_deploy" / "native_containers.txt"
SUFFIX_RX = re.compile(r"^[-_A-Za-z0-9]+$")
SCHEME = "version"  # deploy records made with one deployment per version (older ones: one per run)
MARKER = "deployment.json"  # workflows/<Name>/deployment.json: the deployment's marker
MAX_PATH_PART = 255  # bytes per file name in a git tree / most file systems
COMMIT_TAG = "(curate-skills)"  # in every commit message curate-deploy makes
DEPLOY_LOG = Path("deploy-log.jsonl")  # in the run; one JSON line per path a deploy step wrote
LEGACY_DEPLOY_LOG = Path("deploy") / "deploy-log.jsonl"  # runs made before runs had their own state


def files_dir(run, s: dict | None = None) -> Path:
    """The run's folder (runstate.files_dir): the deploy log, deploy/workflow.json (the
    deploy copy) and deploy/preflight-diff.md go there."""
    import runstate
    return runstate.files_dir(run, s)


def deploy_log_path(run, s: dict | None = None) -> Path:
    import runstate
    s = runstate.load(run) if s is None else s
    return Path(run) / LEGACY_DEPLOY_LOG if runstate.is_legacy(run, s) else files_dir(run, s) / DEPLOY_LOG
# The only fields in which the deploy copy may differ from the approved bundle (besides fields that
# held an emit placeholder). Anything else is refused: the deploy must run what Gate 4 approved.
ALLOWED_DIFF = [("WorkflowName", "deploy name"),
                (r"ActionList\.[^.]+\.Arguments\.folder", "the run's own S3 folder (<deploy name>/<run id>)"),
                (r"FunctionGitRepo\.[^.]+(\[\d+\])?", "per-deployment function folder")]
API = "https://api.github.com"


class InputError(Exception):
    pass


# ── credentials ──────────────────────────────────────────────────────────────

def get_token() -> tuple[str | None, str]:
    """(token, where it came from). GH_PAT as in CURATE, else `gh auth token`."""
    tok = os.environ.get("GH_PAT", "").strip()
    if tok:
        return tok, "GH_PAT"
    tok = gh_cli_token()
    return (tok, "gh auth token") if tok else (None, "none")


def gh_cli_token() -> str | None:
    """The GitHub CLI's token (`gh auth token`), or None. Tests replace this (conftest)."""
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        p = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


class GitHub:
    """GitHub REST calls. `get`/`put` return (status, headers, parsed body or None). Only the
    publish and upload steps call `put`; the preflight only reads."""

    def __init__(self, token: str, opener=None):
        self._token = token
        self._open = opener or urllib.request.urlopen

    def get(self, api_path: str, **params):
        return self._request("GET", api_path, None, params)

    def put(self, api_path: str, body: dict):
        return self._request("PUT", api_path, body, {})

    def delete(self, api_path: str, body: dict):  # only teardown, after the user's yes
        return self._request("DELETE", api_path, body, {})

    def _request(self, method: str, api_path: str, body, params):
        url = API + api_path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "curate-skills",
            **({"Content-Type": "application/json"} if body else {})})
        try:
            with self._open(req, timeout=30) as r:
                return r.status, dict(r.headers), json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), None


# ── the run and its bundle ───────────────────────────────────────────────────

# Files the OS or Python leave in a folder when someone browses or imports it; not bundle content.
OS_METADATA = re.compile(r"(^|/)(\.DS_Store|Thumbs\.db|desktop\.ini|\._[^/]*|__pycache__/.*)$")


def tree_hash(folder: Path) -> str:
    """sha256 over the bundle's files (relative path + bytes), ignoring OS metadata: ids.tree_hash,
    the one hash rule for bundles, drafts and versions."""
    import ids
    return ids.tree_hash(folder)


def load_state_only(run: str) -> dict:
    """The run's state with load_run's checks, for commands that need no bundle (monitor status)."""
    import runstate
    path = Path(run) / "state.json"
    if not path.is_file():
        raise InputError(f"{path} not found: {run} is not a run, and no session links it as its run "
                         "(spec.py runs lists this project's sessions and runs)")
    return runstate.load(run)


def load_run(run: str) -> tuple[dict, Path]:
    import runstate
    path = Path(run) / "state.json"
    if not path.is_file():
        raise InputError(f"{path} not found: {run} is not a run, and no session links it as its run "
                         "(spec.py runs lists this project's sessions and runs)")
    s = runstate.load(run)
    if s.get("schema_version", 1) < 2:
        raise InputError("this run was made by curate-skills v1, which does not deploy")
    if not s.get("workflow_id"):  # only the bundle built from an approved version is deployed
        import execution as X
        raise InputError(X.pre_version(s) if s.get("bundle_dir") else
                         "this run has no approved workflow version yet — run curate-emit and approve Gate 4 first")
    if s.get("deploy_choice") == "pegasus":
        raise InputError(PEGASUS_REFUSAL)
    ex = s.get("execution") or {}
    if ex.get("target") != "faasr" or not ex.get("bundle_dir"):
        raise InputError(f"this run has no FaaSr bundle for its approved version {s['workflow_id']} (it is built "
                         "when Gate 4 or \"Start this run?\" chooses Approve & deploy); approve Gate 4 again "
                         "with --deploy yes")
    bundle = Path(ex["bundle_dir"])
    if not (bundle / "workflow.json").is_file():
        raise InputError(f"{bundle}/workflow.json not found; approve Gate 4 again to rebuild the bundle")
    return s, bundle


PEGASUS_REFUSAL = ("Gate 4 chose the Pegasus target for this run; the FaaSr deploy steps do not apply "
                   "(nothing is written to GitHub or S3)")


def gate4_problem(s: dict) -> str | None:
    """None if the run's version is Gate 4-approved: approved in this run, or carried over from another
    run's approval by `start --at deploy|finalize --from-run` (the version is then checked by its
    hash, and the bundle by its manifest)."""
    if s.get("deploy_choice") == "pegasus":
        return PEGASUS_REFUSAL
    hist = s.get("history") or []
    approvals = [i for i, h in enumerate(hist)
                 if h.get("gate") == 4 and h.get("decision") in ("approve", "copied")]
    if not approvals:
        return "Gate 4 was never approved in this run; only an approved bundle can be deployed"
    for h in hist[approvals[-1] + 1:]:
        if h.get("decision") in ("revise", "rewind"):
            return (f"the last Gate 4 approval was superseded by a later {h['decision']} "
                    f"({(h.get('text') or '')[:60]!r}); approve Gate 4 again first")
    return None


def placeholders(obj, path="") -> list[str]:
    """JSON paths whose string value still holds an emit placeholder such as <FAASR_S3_BUCKET>."""
    if isinstance(obj, dict):
        return [p for k, v in obj.items() for p in placeholders(v, f"{path}.{k}" if path else k)]
    if isinstance(obj, list):
        return [p for i, v in enumerate(obj) for p in placeholders(v, f"{path}[{i}]")]
    return [path] if isinstance(obj, str) and "<FAASR_" in obj else []


PLACEHOLDER_RX = re.compile(r"<(FAASR_[A-Z0-9_]+)>")


def fill_placeholders(obj, path="", filled=None, missing=None):
    """A copy with every <FAASR_X> replaced by $FAASR_X; also {json path: var} filled and the
    unset vars ({path: [vars]}). Only emit's non-secret placeholders exist (user, repo, S3 endpoint/bucket/region)."""
    filled = {} if filled is None else filled
    missing = [] if missing is None else missing
    if isinstance(obj, dict):
        out = {k: fill_placeholders(v, f"{path}.{k}" if path else k, filled, missing)[0] for k, v in obj.items()}
    elif isinstance(obj, list):
        out = [fill_placeholders(v, f"{path}[{i}]", filled, missing)[0] for i, v in enumerate(obj)]
    elif isinstance(obj, str) and PLACEHOLDER_RX.search(obj):
        def sub(m):
            val = os.environ.get(m.group(1), "").strip()
            if not val:
                missing.append(m.group(1))
                return m.group(0)
            filled.setdefault(path, []).append(m.group(1))
            return val
        out = PLACEHOLDER_RX.sub(sub, obj)
    else:
        out = obj
    return out, filled, sorted(set(missing))


def required_secret_names(wf: dict) -> list[str]:
    """GitHub secret names the generated Actions YAML imports (vendored register_workflow.py
    generate_github_secret_imports + generate_user_defined_secret_imports; GitHub Actions only)."""
    names = [f"{name}_PAT" for name, cs in (wf.get("ComputeServers") or {}).items()
             if cs.get("FaaSType") == "GitHubActions"]
    for ds in (wf.get("DataStores") or {}):
        names += [f"{ds}_AccessKey", f"{ds}_SecretKey"]
    names += list(wf.get("Secrets") or [])
    return sorted(set(names))


# ── the renamed deploy copy ──────────────────────────────────────────────────

def _file_names(entry, fn: str) -> list[str]:
    """The .py file names a FunctionGitRepo entry (a path or a list of paths) names."""
    if entry is None:
        return [f"{fn}.py"]
    return [p.rsplit("/", 1)[-1] for p in ([entry] if isinstance(entry, str) else entry)]


def function_files(wf: dict) -> list[tuple[str, str]]:
    """(function, file name) for every file FunctionGitRepo names, by function. A FaaSr-style
    step has one, <fn>.py; a plain step two: the FaaSr wrapper <fn>.py and its unchanged function
    <fn>_main.py. The bundle holds each as functions/<file>; the deploy as functions/<new>/<file>."""
    return [(fn, b) for fn, p in sorted((wf.get("FunctionGitRepo") or {}).items()) for b in _file_names(p, fn)]


def payload_path(name: str, rid: str) -> str:
    return f"workflows/{name}/{rid}.json"


def marker_path(name: str) -> str:
    return f"workflows/{name}/{MARKER}"


def logs_prefix(wf: dict, name: str, invocation_timestamp: str | None = None, invocation_id: str | None = None) -> str:
    """FaaSr's log folder: FaaSrLog/<Name>/ (the deployment's), or one invocation's
    (helpers.s3_helper_functions.get_invocation_folder)."""
    base = f"{wf.get('FaaSrLog') or 'FaaSrLog'}/{name}/"
    return f"{base}{invocation_timestamp}/{invocation_id}/" if invocation_id else base


def deploy_copy(wf: dict, name: str, rid: str, user: str, repo: str, branch: str
                ) -> tuple[dict, list[dict], list[dict]]:
    """The workflow as this run would upload it (its payload), every repo path deploying writes
    (scope "deployment": shared by the version's runs; "run": this run's payload) and every S3
    prefix the invoked run writes (its outputs under <Name>/<run id>/; its logs, known at invoke)."""
    out = copy.deepcopy(wf)
    out["WorkflowName"] = name
    default_store = wf.get("DefaultDataStore") or "S3"
    base = f"{name}/{rid}"
    actions = out.get("ActionList") or {}
    folders = sorted({(a.get("Arguments") or {}).get("folder") for a in actions.values()} - {None, ""})
    s3_targets = []
    for action, a in sorted(actions.items()):
        args = a.get("Arguments") or {}
        if not args.get("folder"):
            s3_targets.append({"store": default_store, "prefix": None, "kind": "outputs", "action": action})
            continue
        if not args["folder"].startswith(base):
            args["folder"] = base if len(folders) == 1 else f"{base}/{args['folder']}"
    if folders:
        s3_targets.append({"store": default_store, "prefix": base + "/", "kind": "outputs"})
    s3_targets.append({"store": wf.get("LoggingDataStore") or default_store, "prefix": None, "kind": "logs",
                       "shown": logs_prefix(wf, name) + "<invocation timestamp>/<invocation id>/"})
    for t in s3_targets:
        t["bucket"] = ((wf.get("DataStores") or {}).get(t["store"]) or {}).get("Bucket")
    folder = f"functions/{name}"
    fns = sorted({a["FunctionName"] for a in actions.values()})
    approved = wf.get("FunctionGitRepo") or {}

    def repo_paths(fn):  # the same shape as the approved entry: one path, or a list of paths
        paths = [f"{user}/{repo}/{branch}/{folder}/{b}" for b in _file_names(approved.get(fn), fn)]
        return paths if isinstance(approved.get(fn), list) else paths[0]

    out["FunctionGitRepo"] = {fn: repo_paths(fn) for fn in fns}
    targets = [{"step": "publish", "path": f"{folder}/{b}", "kind": "function", "scope": "deployment"}
               for fn in fns for b in _file_names(approved.get(fn), fn)]
    targets.append({"step": "upload", "path": payload_path(name, rid), "kind": "workflow", "scope": "run"})
    servers = wf.get("ComputeServers") or {}
    for action, a in sorted(actions.items()):
        if (servers.get(a.get("FaaSServer")) or {}).get("FaaSType") == "GitHubActions":
            targets.append({"step": "register", "path": f".github/workflows/{name}-{action}.yml",
                            "kind": "actions", "action": action, "scope": "deployment"})
    targets.append({"step": "register", "path": marker_path(name), "kind": "marker", "scope": "deployment"})
    return out, targets, s3_targets


def field_diff(a, b, path="") -> list[tuple[str, object, object]]:
    """(json path, approved value, deploy-copy value) for every leaf that differs."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a) | set(b), key=str)
                for d in field_diff(a.get(k), b.get(k), f"{path}.{k}" if path else str(k))]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in field_diff(x, y, f"{path}[{i}]")]
    return [] if a == b else [(path, a, b)]


def deploy_diff(wf: dict, copy_wf: dict, filled: dict) -> tuple[list[dict], list[str]]:
    """Field-by-field diff of the deploy copy against the approved bundle, each with its reason,
    and a problem for any field outside ALLOWED_DIFF that did not hold a placeholder."""
    rows, problems = [], []
    for path, old, new in field_diff(wf, copy_wf):
        reason = next((why for rx, why in ALLOWED_DIFF if re.fullmatch(rx, path)), None)
        if path in filled:
            reason = "placeholder " + ", ".join(f"${v}" for v in filled[path]) + (f"; {reason}" if reason else "")
        if reason is None:
            problems.append(f"the deploy copy differs from the approved bundle at {path} "
                            f"({_short(old)} → {_short(new)}), which is not an allowed deploy change "
                            "(refusing: the deploy must run what Gate 4 approved)")
        rows.append({"field": path, "approved": old, "deploy": new, "reason": reason or "NOT ALLOWED"})
    return rows, problems


def derived_changes(wf: dict, copy_wf: dict, rid: str = "<run-id>") -> list[dict]:
    """Locations that change only because the name changes (not fields of workflow.json)."""
    old, new = wf["WorkflowName"], copy_wf["WorkflowName"]
    return [{"what": "S3 log prefix (FaaSr)", "approved": logs_prefix(wf, old), "deploy": logs_prefix(wf, new)},
            {"what": "workflow JSON in the repo (this run's payload)", "approved": f"workflows/{old}.json",
             "deploy": payload_path(new, rid)},
            {"what": "function files in the repo", "approved": "functions/<fn>.py", "deploy": f"functions/{new}/<fn>.py"},
            {"what": "Actions files in the repo", "approved": f".github/workflows/{old}-<action>.yml",
             "deploy": f".github/workflows/{new}-<action>.yml"}]


def read_deploy_log(run: str) -> tuple[list[dict], list[str]]:
    """This run's own deploy log: lines curate-deploy appended for each path a step wrote.
    Repo lines: {run_id, deploy_name, step, repo, branch, path}; S3 lines (written at invoke for
    the prefixes the run writes): {run_id, deploy_name, step, bucket, prefix}."""
    path = deploy_log_path(run)
    entries, bad = [], []
    if path.is_file():
        for i, line in enumerate(path.read_text().splitlines(), 1):
            try:
                e = json.loads(line)
                if isinstance(e, dict):
                    entries.append(e)
                    continue
            except ValueError:
                pass
            bad.append(f"{DEPLOY_LOG} line {i} is not a JSON object; ignored")
    return entries, bad


def own_paths(entries: list[dict], run_id, deploy_name: str, repo: str, branch: str
              ) -> tuple[set[str], set[tuple[str, str]]]:
    """Repo paths and (bucket, prefix) pairs the deploy log says this run (or, for a reused deployment,
    the run that deployed it — run_id may be a set) wrote under this name."""
    ids = run_id if isinstance(run_id, (set, frozenset)) else {run_id}
    mine = [e for e in entries if e.get("run_id") in ids and e.get("deploy_name") == deploy_name]
    files = {e["path"] for e in mine if e.get("path") and e.get("repo") == repo and e.get("branch") == branch}
    s3 = {(e["bucket"], e["prefix"]) for e in mine if e.get("bucket") and e.get("prefix")}
    return files, s3


def name_problems(copy_wf: dict, targets: list[dict], s3_targets: list[dict], rid: str,
                  user: str, repo: str, branch: str) -> list[str]:
    """Every written path (repo and S3) and every FunctionGitRepo entry must be under the deploy
    name, and the run's outputs under its own folder <Name>/<run id>/."""
    new = copy_wf["WorkflowName"]
    out = []
    for t in s3_targets:
        if t["kind"] != "outputs":
            continue
        if t["prefix"] is None:
            out.append(f"action {t['action']} has no \"folder\" argument, so its S3 outputs cannot be put in "
                       f"the run's own folder {new}/{rid}/")
        elif not t["prefix"].startswith(f"{new}/{rid}/"):
            out.append(f"S3 folder {t['prefix']} is not under the run's own folder {new}/{rid}/")
    for t in targets:
        if new not in t["path"]:
            out.append(f"{t['path']} is not under the deploy name {new!r}")
        long = [x for x in t["path"].split("/") if len(x.encode()) > MAX_PATH_PART]
        if long:
            out.append(f"{t['path']}: the name {long[0][:40]}… is longer than {MAX_PATH_PART} bytes")
    prefix = f"{user}/{repo}/{branch}/functions/{new}/"
    for fn, p in (copy_wf.get("FunctionGitRepo") or {}).items():
        for one in ([p] if isinstance(p, str) else p):
            if not (one.startswith(prefix) and one.endswith(".py")):
                out.append(f"FunctionGitRepo[{fn}] = {one!r} is not a .py file under {prefix}")
    return out


# ── S3 (read-only) ───────────────────────────────────────────────────────────

def env_key(name: str) -> tuple[str | None, str | None, str | None]:
    """(value, which variable it came from, problem) for a credential such as S3_AccessKey.
    The name or its upper-case spelling is accepted (GitHub stores S3_AccessKey as
    S3_ACCESSKEY); both set with different values is refused. Messages never hold values."""
    found = [(n, os.environ[n]) for n in dict.fromkeys([name, name.upper()]) if os.environ.get(n)]
    if not found:
        return None, None, None
    if len({v for _, v in found}) > 1:
        return None, None, (f"{found[0][0]} and {found[1][0]} are both set, with different values; "
                            "unset one (values not shown)")
    spelling = found[0][0] if len(found) == 1 else f"{found[0][0]} and {found[1][0]} (same value)"
    return found[0][1], spelling, None


def s3_keys(cfg: dict) -> tuple[tuple[str, str] | None, dict, list[str]]:
    """The data store's (access key, secret key) from the environment, which spelling each came
    from, and problems. Used for every S3 client this script (and curate-monitor) builds."""
    names = (cfg.get("AccessKey") or "S3_AccessKey", cfg.get("SecretKey") or "S3_SecretKey")
    got = [env_key(n) for n in names]
    problems = [err for _, _, err in got if err]
    spellings = {n: frm for n, (_, frm, _) in zip(names, got) if frm}
    if problems:
        return None, spellings, problems
    if not all(v for v, _, _ in got):
        return None, spellings, [f"{names[0]} / {names[1]} (or {names[0].upper()} / {names[1].upper()}) "
                                 "not set here"]
    return (got[0][0], got[1][0]), spellings, []


def boto3_s3(cfg: dict, access_key: str, secret_key: str):
    """An S3 client built the way the vendored faasr_runtime/s3_client.py builds it."""
    import boto3
    kw = {"aws_access_key_id": access_key, "aws_secret_access_key": secret_key,
          "region_name": cfg.get("Region")}
    if cfg.get("Endpoint"):
        kw["endpoint_url"] = cfg["Endpoint"]
    return boto3.client("s3", **kw)


def _s3_error(e: Exception) -> str:
    code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code")
    return f"{type(e).__name__}{f' {code}' if code else ''}"


def s3_check(wf: dict, s3_targets: list[dict], curate_prefixes: list[str], factory) -> tuple[dict, list[str]]:
    """head_bucket and one-key listings only (no writes): is the bucket reachable, and does any
    prefix the run would write already hold objects? CURATE's own prefixes are listed as info."""
    info, problems = {"checked": False, "buckets": {}, "curate_prefixes": []}, []
    clients = {}
    for t in s3_targets:
        if t["prefix"] is None:
            continue
        store = t["store"]
        if store not in clients:
            cfg = (wf.get("DataStores") or {}).get(store) or {}
            keys, spellings, key_problems = s3_keys(cfg)
            info.setdefault("key_vars", {}).update(spellings)
            if not keys:
                problems += [f"S3 not checked: {x}, so the bucket and existing S3 paths could not be checked"
                             for x in key_problems]
                clients[store] = None
                continue
            ak, sk = keys
            try:
                c = factory(cfg, ak, sk)
                c.head_bucket(Bucket=cfg.get("Bucket"))
                info["buckets"][store] = {"bucket": cfg.get("Bucket"), "endpoint": cfg.get("Endpoint"),
                                          "region": cfg.get("Region"), "reachable": True}
                clients[store] = (c, cfg.get("Bucket"))
                info["checked"] = True
            except Exception as e:  # noqa: BLE001 — reported by type/code only
                problems.append(f"S3 bucket {cfg.get('Bucket')!r} ({store}) not reachable with these keys: "
                                f"{_s3_error(e)}")
                clients[store] = None
        if not clients[store]:
            t["exists"] = None
            continue
        c, bucket = clients[store]
        try:
            r = c.list_objects_v2(Bucket=bucket, Prefix=t["prefix"], MaxKeys=1)
            t["exists"] = bool(r.get("KeyCount") or r.get("Contents"))
        except Exception as e:  # noqa: BLE001
            t["exists"] = None
            problems.append(f"could not list s3://{bucket}/{t['prefix']}: {_s3_error(e)}")
    store = next((t["store"] for t in s3_targets if clients.get(t["store"])), None)
    if store:
        c, bucket = clients[store]
        for pfx in curate_prefixes:
            try:
                r = c.list_objects_v2(Bucket=bucket, Prefix=pfx, MaxKeys=1)
                if r.get("KeyCount") or r.get("Contents"):
                    info["curate_prefixes"].append(pfx)
            except Exception:  # noqa: BLE001 — informational only
                pass
    return info, problems


# ── the deploy name ──────────────────────────────────────────────────────────

def _read_state(run_dir: Path) -> dict | None:
    import runstate
    try:
        return runstate.load(run_dir)
    except (OSError, ValueError):
        return None


def _project_of(run: str | Path) -> Path:
    s = _read_state(Path(run)) or {}
    if s.get("project_dir"):
        return Path(s["project_dir"])
    import ids
    return ids.project_of_path(run) or Path(run).resolve().parents[2]


def project_runs(run: str | Path) -> list[tuple[Path, dict]]:
    """Every run of the project (this one included), with its state: each session (with its
    current execution's records), then each run that is not some session's current
    one (a standalone run, or one a later Gate 4 replaced)."""
    import ids
    import runstate
    project = _project_of(run)
    out = []
    for d in ids.session_dirs(project):
        if (st := _read_state(d)):
            out.append((d, st))
    for d in ids.execution_dirs(project):
        if runstate.resolve_run(d) == d.resolve() and (st := _read_state(d)):
            out.append((d, st))
    return out


def run_id_of(s: dict) -> str:
    """The run's own id (a session's state carries its current run's under execution)."""
    return (s.get("execution") or {}).get("run_id") or s["run_id"]


def is_legacy_deploy(s: dict) -> bool:
    """A run that started deploying under its own per-run name (before one deployment per version)."""
    rec = s.get("deploy") or {}
    return rec.get("scheme") != SCHEME and any((rec.get("steps") or {}).get(x, {}).get("ok") for x in STEPS)


def deploy_name(run, override: str | None = None, s: dict | None = None) -> dict:
    """The deployment's name: the version's workflow id, plus $CURATE_DEPLOY_NAME_SUFFIX /
    --name-suffix if set; pinned once a deploy step ran. {"name", "suffix", "source"}."""
    s = (_read_state(Path(run)) or {}) if s is None else s
    rec = s.get("deploy") or {}
    if rec.get("scheme") == SCHEME and rec.get("deploy_name"):
        sfx = rec.get("suffix") or ""
        if override and override != sfx:
            raise InputError(f"this run's deploy started as {rec['deploy_name']!r}; keep its suffix {sfx!r} "
                             f"(got {override!r})")
        return {"name": rec["deploy_name"], "suffix": sfx, "source": "pinned: this run's deploy started with it"}
    sfx = override or ""
    if sfx and not SUFFIX_RX.match(sfx):
        raise InputError(f"deploy name suffix {sfx!r} must be letters, digits, '-' or '_'")
    wid = s.get("workflow_id")
    if not wid:
        return {"name": "<workflow-id>" + sfx, "suffix": sfx, "source": "the version's workflow id, once Gate 4 publishes it"}
    return {"name": wid + sfx, "suffix": sfx,
            "source": "the version's workflow id" + (" + CURATE_DEPLOY_NAME_SUFFIX / --name-suffix" if sfx else "")}


# ── the deployment (one per version) and its marker ─────────────────────────

def read_marker(gh: GitHub, user: str, repo: str, branch: str, name: str) -> tuple[dict | None, str | None]:
    """(the deployment's marker, None), (None, None) if there is none, or (None, problem)."""
    path = marker_path(name)
    st, _, body = gh.get(f"/repos/{user}/{repo}/contents/{path}", ref=branch)
    if st == 404:
        return None, None
    if st != 200 or not isinstance(body, dict):
        return None, f"could not read {path} (HTTP {st})"
    try:
        m = json.loads(base64.b64decode(body.get("content") or ""))
    except ValueError:
        m = None
    if not isinstance(m, dict) or m.get("curate_deployment") != 1:
        return None, f"{path} exists but is not curate-skills' deployment marker; refusing"
    return {**m, "_sha": body.get("sha")}, None


def marker_text(m: dict) -> str:
    return json.dumps({k: v for k, v in m.items() if not k.startswith("_")}, indent=2) + "\n"


def expected_deployment(functions: dict[str, str], copy_wf: dict) -> dict[str, str]:
    """{repo path: text} of the deployment's files (not the marker): the wrappers (functions:
    {file name: text}) and the Actions files CURATE's generator writes for this deploy name."""
    new = copy_wf["WorkflowName"]
    out = {f"functions/{new}/{b}": functions[b] for _, b in function_files(copy_wf) if b in functions}
    out.update(expected_actions_files(copy_wf))
    return out


def bundle_functions(bundle: Path, copy_wf: dict) -> dict[str, str]:
    return {b: (bundle / "functions" / b).read_text() for _, b in function_files(copy_wf)
            if (bundle / "functions" / b).is_file()}


def deployment_state(gh: GitHub, user: str, repo: str, branch: str, copy_wf: dict, functions: dict[str, str],
                     content_hash: str | None) -> dict:
    """Is the version's deployment in the repo? state:
    absent    no marker, none of its files;
    partial   no marker, some files (a deploy that did not finish);
    same      the marker names this version and every file is byte-identical: reused;
    changed   the marker names this version but files differ or are missing (update-deployment);
    different the marker names another version (refused);
    unreadable."""
    name = copy_wf["WorkflowName"]
    marker, problem = read_marker(gh, user, repo, branch, name)
    if problem:
        return {"state": "unreadable", "problem": problem, "present": [], "differs": []}
    expected = expected_deployment(functions, copy_wf)
    present, differs, missing = [], [], []
    for path, text in sorted(expected.items()):
        st, _, body = gh.get(f"/repos/{user}/{repo}/contents/{path}", ref=branch)
        if st == 404:
            missing.append(path)
            continue
        present.append(path)
        if st != 200 or not isinstance(body, dict) or body.get("encoding") not in (None, "base64"):
            differs.append(path)
        elif base64.b64decode(body.get("content") or "") != text.encode():
            differs.append(path)
    out = {"present": present, "differs": differs, "missing": missing, "expected": sorted(expected)}
    if marker is None:
        return {**out, "state": "partial" if present else "absent"}
    out["marker"] = marker
    if marker.get("content_hash") != content_hash:
        return {**out, "state": "different"}
    return {**out, "state": "changed" if differs or missing else "same"}


def version_content_hash(s: dict) -> str | None:
    import ids
    vdir = Path(s.get("version_dir") or "")
    return ids.draft_content_hash(vdir) if (vdir / "workflow.yml").is_file() else None


def different_version_problem(name: str, repo: str, marker: dict, wid: str | None) -> str:
    return (f"the deployment {name!r} in {repo} was made from another version ({marker.get('workflow_id')}, content "
            f"hash {str(marker.get('content_hash'))[:12]}…), not this one; refusing. Ways out: tear it down from the "
            f"project that made it (/curate-skills:curate-deploy --teardown --workflow {marker.get('workflow_id')} --all); "
            f"if it really is this version, `deploy.py update-deployment --workflow {wid}` (it refuses a different "
            "version too); or deploy under another name with CURATE_DEPLOY_NAME_SUFFIX (e.g. -alice)")


def changed_problem(name: str, repo: str, dep: dict, wid: str | None) -> str:
    what = [f"{x} differs" for x in dep["differs"]] + [f"{x} is missing" for x in dep["missing"]]
    return (f"the deployment {name!r} in {repo} is this version, but its files are not what this version deploys ("
            + "; ".join(what) + f"); `deploy.py update-deployment --workflow {wid}` re-pushes them after checking "
            "every main() against the version")


# ── preflight ────────────────────────────────────────────────────────────────

def _owner_of(gh: GitHub, user: str, repo: str, branch: str, t: dict, new: str) -> dict:
    """Does a target path exist, and is it curate-skills' own (same suffixed deployment)?"""
    st, _, body = gh.get(f"/repos/{user}/{repo}/contents/{t['path']}", ref=branch)
    if st == 404:
        return {"exists": False, "ours": None}
    if st != 200 or not isinstance(body, dict):
        return {"exists": None, "ours": None, "error": f"HTTP {st}"}
    if t["kind"] == "workflow":
        try:
            name = json.loads(base64.b64decode(body.get("content") or "")).get("WorkflowName")
        except (ValueError, AttributeError):
            name = None
        return {"exists": True, "ours": name == new, "why": f"WorkflowName {name!r}"}
    if t["kind"] == "actions":
        text = base64.b64decode(body.get("content") or "").decode("utf-8", "replace")
        first = text.splitlines()[0] if text else ""
        return {"exists": True, "ours": first.strip() == f"name: {new}-{t['action']}",
                "why": f"first line {first.strip()!r}"}
    st, _, commits = gh.get(f"/repos/{user}/{repo}/commits", path=t["path"], sha=branch, per_page=1)
    msg = (commits or [{}])[0].get("commit", {}).get("message", "") if st == 200 else ""
    return {"exists": True, "ours": COMMIT_TAG in msg, "why": f"last commit {msg.splitlines()[0]!r}"
            if msg else "last commit unknown"}


def preflight(run: str, name: dict, gh: GitHub | None = None, token_source: str = "none",
              s3_factory=boto3_s3, overwrite_own: bool = False, done_steps=(), reused_steps=()) -> dict:
    """Read-only. name: deploy_name(). done_steps: deploy steps this run already finished (their
    paths must still be there and, for files this run wrote, in its deploy log); reused_steps: those
    that reused the version's deployment (it must still be the same)."""
    s, bundle = load_run(run)
    wf = json.loads((bundle / "workflow.json").read_text())
    rid = run_id_of(s)
    problems, warnings = [], []
    bhash = tree_hash(bundle)
    if (p := gate4_problem(s)):
        problems.append(p)
    import execution as X
    if (p := X.check(s)):  # the bundle: built from the approved version, unchanged since (its manifest)
        problems.append(p)
    import ids  # the version Gate 4 published must be the one deployed
    vdir = Path(s.get("version_dir") or "")
    if not vdir.is_dir():
        problems.append(f"the approved version {s['workflow_id']} is missing ({vdir})")
    elif ids.version_hash(vdir) != s.get("version_hash"):
        problems.append(f"the approved version {s['workflow_id']} changed after it was published")
    if is_legacy_deploy(s):
        problems.append("this run started deploying under its own per-run name (before one deployment per "
                        "version); it is not continued: start a new run of the version with "
                        f"/curate-skills:curate --start-at deploy --workflow {s.get('workflow_id')} (teardown still "
                        "works on this run's deployment)")
    filled_wf, filled, unset = fill_placeholders(wf)
    if unset:
        problems.append(f"the bundle's placeholders cannot be filled in the deploy copy: {', '.join(unset)} "
                        "not set (at " + ", ".join(placeholders(filled_wf)) + ")")
    try:
        ext = json.loads((bundle / "external_inputs.json").read_text())
    except (OSError, ValueError):
        ext = []
    if ext:
        problems.append("the workflow reads files that must already be in S3 ("
                        + ", ".join(str(e.get("s3_path") or e.get("file")) for e in ext)
                        + f"); the run's own S3 folder ({name['name']}/{rid}/) would not have them")
    servers = wf.get("ComputeServers") or {}
    other = sorted({cs.get("FaaSType") for cs in servers.values()} - {"GitHubActions"})
    if other:
        problems.append(f"only GitHub Actions is deployed (as CURATE); found {other}")
    if os.environ.get("CUSTOM_CONTAINER", "").lower() != "true":
        native = {l.strip() for l in NATIVE_CONTAINERS.read_text().splitlines() if l.strip()}
        bad = sorted({c for c in (wf.get("ActionContainers") or {}).values() if c not in native})
        if bad:
            problems.append(f"containers not on FaaSr's native list: {bad} (CURATE refuses these too; "
                            "set CUSTOM_CONTAINER=true to skip, as in CURATE)")

    user = os.environ.get("FAASR_GH_USERNAME", "").strip()
    repo = os.environ.get("FAASR_ACTION_REPO", "").strip()
    branch = os.environ.get("FAASR_ACTION_BRANCH", "").strip() or "main"
    missing_env = [n for n, v in (("FAASR_GH_USERNAME", user), ("FAASR_ACTION_REPO", repo)) if not v]
    for n in ("GITHUB_REPOSITORY", "GITHUB_REF_NAME"):
        if os.environ.get(n):  # only set inside an Actions runner; CURATE's code would deploy there instead
            problems.append(f"{n} is set here; CURATE's register and invoke code would use it instead of "
                            "FAASR_GH_USERNAME/FAASR_ACTION_REPO/FAASR_ACTION_BRANCH — unset it")
    if missing_env:
        problems.append(f"environment variables not set: {', '.join(missing_env)}")
    for sname, cs in (filled_wf.get("ComputeServers") or {}).items():
        for field, want in (("UserName", user), ("ActionRepoName", repo), ("Branch", branch)):
            if want and cs.get(field) not in (want, None) and "<FAASR_" not in str(cs.get(field)):
                problems.append(f"bundle ComputeServers.{sname}.{field} = {cs.get(field)!r} but the "
                                f"deploy target is {want!r}")
    local_s3 = [n for n in ("S3_AccessKey", "S3_SecretKey") if not env_key(n)[0] and not env_key(n)[2]]
    if local_s3:
        warnings.append(f"{', '.join(local_s3)} (or the upper-case spelling) not set here: deploying works, "
                        "but curate-monitor needs them to read the run's logs from S3")

    copy_wf, targets, s3_targets = deploy_copy(filled_wf, name["name"], rid, user or "<user>", repo or "<repo>", branch)
    problems += name_problems(copy_wf, targets, s3_targets, rid, user or "<user>", repo or "<repo>", branch)
    if copy_wf["WorkflowName"] == wf["WorkflowName"]:
        problems.append(f"the deploy name {wf['WorkflowName']!r} is the workflow's own name, which CURATE's "
                        "deployment uses; refusing")
    req_secrets = required_secret_names(wf)
    diff_rows, diff_problems = deploy_diff(wf, copy_wf, filled)
    problems += diff_problems
    log_entries, bad_lines = read_deploy_log(run)
    warnings += bad_lines
    own_files, own_s3 = own_paths(log_entries, s.get("run_id"), copy_wf["WorkflowName"], f"{user}/{repo}", branch)
    out = {"run": str(Path(run).resolve()), "run_id": rid, "bundle": str(bundle), "bundle_hash": bhash,
           "approved_name": wf.get("WorkflowName"), "deploy_name": copy_wf["WorkflowName"],
           "suffix": name["suffix"], "name_source": name.get("source"), "workflow_id": s.get("workflow_id"),
           "repo": f"{user}/{repo}" if user and repo else None, "branch": branch,
           "token_source": token_source, "targets": targets, "function_git_repo": copy_wf["FunctionGitRepo"],
           "payload": payload_path(copy_wf["WorkflowName"], rid),
           "filled_placeholders": {k: ", ".join(f"${x}" for x in v) + f" = {_dig(copy_wf, k)}" for k, v in filled.items()
                                   if not k.startswith("FunctionGitRepo")},
           "s3_targets": s3_targets, "secrets": {"required": req_secrets}, "curate_files": [],
           "github_checked": False, "deploy_diff": diff_rows, "deployment": None,
           "derived_changes": derived_changes(wf, copy_wf, rid), "overwrite_own": overwrite_own,
           "deploy_log": {"path": str(deploy_log_path(run)), "repo_paths": sorted(own_files),
                          "s3_prefixes": sorted(f"s3://{b}/{x}" for b, x in own_s3)}}

    if unset:
        out["s3"] = {"checked": False, "buckets": {}, "curate_prefixes": []}
    else:
        old_folders = sorted({(a.get("Arguments") or {}).get("folder") for a in (wf.get("ActionList") or {}).values()}
                             - {None, ""})
        curate_prefixes = [f + "/" for f in old_folders] + [logs_prefix(wf, wf["WorkflowName"])]
        out["s3"], s3_problems = s3_check(copy_wf, s3_targets, curate_prefixes, s3_factory)
        problems += s3_problems
        for t in s3_targets:
            if not t.get("exists"):
                continue
            if overwrite_own and (t["bucket"], t["prefix"]) in own_s3:
                t["overwrite"] = True
            else:
                problems.append(f"S3 path s3://{t['bucket']}/{t['prefix']} already holds objects; refusing — "
                                + _new_or_own(overwrite_own))

    if gh is None:
        problems.append("no GitHub token: set GH_PAT (as for CURATE) or log in with `gh auth login`; "
                        "the repo, its secrets and existing files were not checked")
    elif user and repo:
        out["github_checked"] = True
        st, headers, me = gh.get("/user")
        if st != 200:
            problems.append(f"the GitHub token was rejected (HTTP {st})")
        else:
            scopes = headers.get("X-OAuth-Scopes") or headers.get("x-oauth-scopes")
            if scopes is None:
                out["token_scopes"] = ("fine-grained: write access can't be checked (needs Contents, Workflows "
                                       "and Actions: read and write). Recommended: a classic token with repo + "
                                       "workflow")
            else:
                have = {x.strip() for x in scopes.split(",") if x.strip()}
                out["token_scopes"] = sorted(have)
                lack = [x for x in ("repo", "workflow") if x not in have]
                if lack:
                    problems.append(f"the token lacks scope(s) {lack}: CURATE needs repo and workflow "
                                    "(workflow is needed to write .github/workflows/)")
        st, _, r = gh.get(f"/repos/{user}/{repo}")
        if st != 200:
            problems.append(f"repo {user}/{repo} not found or not visible to this token (HTTP {st})")
        else:
            out["default_branch"] = r.get("default_branch")
            if not (r.get("permissions") or {}).get("push"):
                problems.append(f"this token cannot push to {user}/{repo}")
            if r.get("default_branch") != branch:
                problems.append(f"{user}/{repo}'s default branch is {r.get('default_branch')!r} but "
                                f"FAASR_ACTION_BRANCH is {branch!r}: CURATE writes function code and Actions "
                                "files to the default branch and FaaSr reads them from the branch; "
                                "make them the same")
            st, _, sec = gh.get(f"/repos/{user}/{repo}/actions/secrets", per_page=100)
            if st == 200:
                # GitHub stores secret names in upper case and resolves ${{ secrets.X }} without regard
                # to case, so the YAML's S3_AccessKey is the repo's S3_ACCESSKEY.
                have = {x["name"].upper() for x in (sec or {}).get("secrets", [])}
                out["secrets"].update(present=[n for n in req_secrets if n.upper() in have],
                                      missing=[n for n in req_secrets if n.upper() not in have])
                if out["secrets"]["missing"]:
                    problems.append(f"GitHub secrets missing in {user}/{repo}: "
                                    f"{', '.join(out['secrets']['missing'])} (add with `gh secret set NAME`)")
            else:
                warnings.append(f"could not list the repo's secret names (HTTP {st}); check by hand: "
                                + ", ".join(req_secrets))
            new = copy_wf["WorkflowName"]
            problems += _deployment_targets(out, gh, user, repo, branch, copy_wf, bundle, s, targets, own_files,
                                            overwrite_own, done_steps, reused_steps)
            for t in targets:  # this run's payload: new, or this run's own (written / retried)
                if t["scope"] != "run":
                    continue
                t.update(_owner_of(gh, user, repo, branch, t, new))
                if t["step"] in done_steps:
                    t["written"] = True
                    if not (t.get("exists") and t.get("ours") and t["path"] in own_files):
                        problems.append(f"{t['path']} was written by this deploy's {t['step']} step but is "
                                        f"{'gone' if not t.get('exists') else 'no longer ours (' + str(t.get('why')) + ')'}"
                                        "; refusing to continue")
                    continue
                if not t.get("exists"):
                    continue
                if overwrite_own and t.get("ours") and t["path"] in own_files:
                    t["overwrite"] = True
                    continue
                problems.append(f"{t['path']} already exists ({t.get('why')}); refusing — a run's payload is never "
                                "rewritten (FaaSr reads it by branch while the run is going); "
                                + _new_or_own(overwrite_own))
            name0 = wf["WorkflowName"]
            for p in [f"workflows/{name0}.json"] + [f"functions/{b}" for _, b in function_files(copy_wf)] \
                     + [f".github/workflows/{name0}-{a}.yml" for a in sorted(wf.get("ActionList") or {})]:
                st, _, _ = gh.get(f"/repos/{user}/{repo}/contents/{p}", ref=branch)
                if st == 200:
                    out["curate_files"].append(p)
    out["existing_paths"] = [t["path"] for t in targets if t.get("exists") and not t.get("written")
                             and not t.get("reused")] + \
        [f"s3://{t['bucket']}/{t['prefix']}" for t in s3_targets if t.get("exists")]
    out["refused_paths"] = [t["path"] for t in targets if t.get("exists") and not t.get("overwrite")
                            and not t.get("written") and not t.get("reused")] + \
        [f"s3://{t['bucket']}/{t['prefix']}" for t in s3_targets if t.get("exists") and not t.get("overwrite")]
    out["overwrite_paths"] = [t["path"] for t in targets if t.get("overwrite")] + \
        [f"s3://{t['bucket']}/{t['prefix']}" for t in s3_targets if t.get("overwrite")]
    out.update(ok=not problems, problems=problems, warnings=warnings)
    try:  # the full comparison, for anyone who wants it; never shown in the summary
        diff_path = files_dir(run) / "deploy" / "preflight-diff.md"
        text = diff_md(out)
        if not leakcheck.find_leaks(text):
            diff_path.parent.mkdir(parents=True, exist_ok=True)
            diff_path.write_text(text)
            out["diff_file"] = str(diff_path)
    except OSError:
        pass
    out["summary"] = preflight_summary(out)
    out["_copy_wf"] = copy_wf  # for the deploy steps; never printed
    return out


def _deployment_targets(out: dict, gh: GitHub, user: str, repo: str, branch: str, copy_wf: dict, bundle: Path,
                        s: dict, targets: list[dict], own_files: set, overwrite_own: bool, done_steps,
                        reused_steps) -> list[str]:
    """Check the version's deployment in the repo and mark each deployment target: reused (same),
    written (this run made it), new, or overwrite (this run's own partial deploy, retried)."""
    import ids
    new, full = copy_wf["WorkflowName"], f"{user}/{repo}"
    try:
        dep = deployment_state(gh, user, repo, branch, copy_wf, bundle_functions(bundle, copy_wf),
                               version_content_hash(s))
    except StepError as e:
        return [str(e)]
    marker = dep.get("marker") or {}
    project = _project_of_state(s)
    out["deployment"] = {"name": new, "state": dep["state"], "differs": dep["differs"], "missing": dep["missing"],
                         "present": dep["present"], "marker": marker_path(new),
                         "projects": marker.get("projects") or [], "deployed_at": marker.get("deployed_at"),
                         "adds_project": dep["state"] == "same" and ids.project_id(project) not in (marker.get("projects") or [])}
    problems = []
    if dep["state"] == "unreadable":
        problems.append(dep["problem"])
    elif dep["state"] == "different":
        problems.append(different_version_problem(new, full, marker, s.get("workflow_id")))
    elif dep["state"] == "changed":
        problems.append(changed_problem(new, full, dep, s.get("workflow_id")))
    for t in targets:
        if t["scope"] != "deployment":
            continue
        if t["step"] in reused_steps:
            t["reused"] = True
            if dep["state"] != "same":
                problems.append(f"this run reused the deployment {new!r}, which is no longer the same "
                                f"(state {dep['state']}); refusing to continue")
            continue
        if dep["state"] == "same":
            t.update(exists=True, reused=True)
            continue
        exists = t["path"] in dep["present"] or (t["kind"] == "marker" and bool(marker))
        t["exists"] = exists
        if t["step"] in done_steps:
            t["written"] = True
            if t["kind"] != "marker" and not (exists and t["path"] in own_files):
                problems.append(f"{t['path']} was written by this deploy's {t['step']} step but is "
                                f"{'gone' if not exists else 'not in its deploy log'}; refusing to continue")
            continue
        if not exists or dep["state"] in ("different", "changed", "unreadable"):
            continue
        if overwrite_own and t["path"] in own_files:
            t["overwrite"] = True
            continue
        problems.append(f"{t['path']} already exists without the deployment's marker ({marker_path(new)}): a deploy "
                        "of it did not finish; refusing — " + _new_or_own(overwrite_own))
    return problems


def _project_of_state(s: dict) -> Path:
    import ids
    if s.get("project_dir"):
        return Path(s["project_dir"])
    return ids.project_of_path(Path(s.get("version_dir") or ".")) or Path(".").resolve()


def _new_or_own(overwrite_own: bool) -> str:
    if overwrite_own:
        return ("only paths this run's own deploy log records (and that still carry its mark) may be "
                "overwritten")
    return ("every path the deploy writes must be new (to retry this run's own partial deploy, "
            "use --overwrite-own-deploy)")


def _short(v, n: int = 60) -> str:
    text = "(absent)" if v is None else v if isinstance(v, str) else json.dumps(v)
    return repr(text if len(text) <= n else text[:n - 1] + "…")


def _cell(v) -> str:
    text = "(absent)" if v is None else v if isinstance(v, str) else json.dumps(v)
    return "`" + text.replace("|", "\\|").replace("`", "'") + "`"


def _dig(obj, path: str):
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        obj = obj[int(part[1:-1])] if part.startswith("[") else obj[part]
    return obj


def _file_state(t: dict) -> str:
    if t.get("reused"):
        return "in the repo, byte-identical — reused (nothing written)"
    if t.get("written"):
        return "written by this deploy"
    if t.get("exists") is None:
        return "not checked"
    if not t["exists"]:
        return "new"
    if t.get("overwrite"):
        return "exists, in this run's deploy log — will be overwritten (--overwrite-own-deploy)"
    return "already exists — refused"


def _s3_state(t: dict) -> str:
    if t.get("exists") is None:
        return "not checked"
    if not t["exists"]:
        return "new"
    if t.get("overwrite"):
        return "exists, in this run's deploy log — will be overwritten (--overwrite-own-deploy)"
    return "already exists — refused"


DEPLOYMENT_STATE = {
    "absent": "not in the repo yet — publish and register write it",
    "partial": "some of its files are in the repo without its marker (a deploy that did not finish)",
    "same": "already in the repo for this version, byte-identical — reused: publish and register write nothing",
    "changed": "in the repo for this version, but its files differ — refused (update-deployment)",
    "different": "in the repo for ANOTHER version — refused",
    "unreadable": "could not be checked",
}


def preflight_summary(p: dict) -> str:
    """What the deploy will write, one line per file or folder. The field-by-field comparison with
    the approved bundle runs silently (a field outside the allowed changes is a problem, one line);
    the full comparison is written to the run's deploy folder (diff_md)."""
    lines = [f"**Deploy preflight — {'ready' if p['ok'] else 'not ready'}** (read-only; nothing was written "
             "to GitHub or S3)", "",
             f"Deploys as: `{p['deploy_name']}`" + (f" (name from: {p['name_source']})" if p.get("name_source") else "")
             + f", run `{p.get('run_id')}`"]
    dep = p.get("deployment")
    if dep:
        lines.append(f"Deployment (one per version): {DEPLOYMENT_STATE.get(dep['state'], dep['state'])}"
                     + ("; publish adds this project to its marker" if dep.get("adds_project") else ""))
    lines += ["", f"GitHub (`{p['repo']}`, branch `{p['branch']}`):"]
    lines += [f"- `{t['path']}` — {_file_state(t)}" + (" (this run's payload)" if t.get("scope") == "run" else "")
              for t in p["targets"]]
    by_bucket: dict[str, list[dict]] = {}
    for t in p.get("s3_targets") or []:
        if t["prefix"] or t.get("shown"):
            by_bucket.setdefault(t["bucket"], []).append(t)
    for bucket, ts in by_bucket.items():
        lines += ["", f"S3 (`{bucket}`), written when the workflow runs:"]
        lines += [f"- outputs folder: `{t['prefix']}` — {_s3_state(t)}" if t["prefix"] else
                  f"- logs folder: `{t['shown']}` (FaaSr's, one per invocation)" for t in ts]
    lines += ["", "Then invoke starts the workflow (a workflow_dispatch of the entry action, as CURATE)."]
    if (p.get("s3") or {}).get("key_vars"):
        lines += ["", "- S3 keys read from: " + ", ".join(f"`{v}`" for v in p["s3"]["key_vars"].values())]
    sec = p["secrets"]
    lines += ["", f"- GitHub secrets needed: {', '.join(sec['required'])}"
              + (f" — missing: {', '.join(sec['missing'])}" if sec.get("missing") else
                 " — all present" if "present" in sec else " — not checked")]
    if p["curate_files"] or (p.get("s3") or {}).get("curate_prefixes"):
        lines.append("- CURATE's own deployment found and left untouched: "
                     + ", ".join(f"`{x}`" for x in p["curate_files"] + (p.get("s3") or {}).get("curate_prefixes", [])))
    refused = set(p.get("refused_paths") or [])
    exists_problems = [x for x in p["problems"] if any(r in x for r in refused)]
    if exists_problems:
        lines.append(f"- **Problem:** {len(refused)} path(s) above already exist; "
                     + _new_or_own(p.get("overwrite_own", False)))
    lines += [f"- **Problem:** {x}" for x in p["problems"] if x not in exists_problems]
    lines += [f"- Note: {x}" for x in p["warnings"]]
    if p.get("diff_file"):
        lines.append(f"- Full comparison with the approved bundle: `{p['diff_file']}`")
    return "\n".join(lines)


def diff_md(p: dict) -> str:
    """The full field-by-field comparison (kept in the run's deploy folder, not shown)."""
    lines = [f"# Deploy copy vs approved bundle — `{p['deploy_name']}`", "",
             f"Approved bundle: `{p['bundle']}` (workflow `{p['approved_name']}`). The approved bundle is not changed.",
             "", "| Field | Approved bundle | Deploy copy | Why |", "|---|---|---|---|"]
    lines += [f"| `{r['field']}` | {_cell(r['approved'])} | {_cell(r['deploy'])} | {r['reason']} |"
              for r in p.get("deploy_diff") or []]
    lines += [f"| {d['what']} (derived from the name) | `{d['approved']}` | `{d['deploy']}` | deploy name |"
              for d in p.get("derived_changes") or []]
    return "\n".join(lines) + "\n"


def _gate4_target(run: str) -> dict:
    """The deploy copy, its targets and settings, as Gate 4 shows them. Local only."""
    envfile.load()
    import execution as X
    s = _read_state(Path(run)) or {}
    override = os.environ.get("CURATE_DEPLOY_NAME_SUFFIX") or None
    if not (s.get("execution") or {}).get("bundle_hash"):
        wf = X.faasr_built(s, run)["workflow"]  # Gate 4's preview: built in memory from the draft
    else:
        s, bundle = load_run(run)
        wf = json.loads((bundle / "workflow.json").read_text())
    wid, dropped = X.gate4_ids(s, run)  # before Gate 4 publishes: the id it would publish (or on "drop")
    if wid and not s.get("workflow_id"):
        s = {**s, "workflow_id": wid}
    name = deploy_name(run, override, s)
    rid = (s.get("execution") or {}).get("run_id") or "<run-id>"
    filled_wf, _, unset = fill_placeholders(wf)
    user = os.environ.get("FAASR_GH_USERNAME", "").strip() or "<FAASR_GH_USERNAME>"
    repo = os.environ.get("FAASR_ACTION_REPO", "").strip() or "<FAASR_ACTION_REPO>"
    branch = os.environ.get("FAASR_ACTION_BRANCH", "").strip() or "main"
    copy_wf, targets, s3_targets = deploy_copy(filled_wf, name["name"], rid, user, repo, branch)
    if_dropped = dropped + name["suffix"] if dropped else None
    return {"copy_wf": copy_wf, "targets": targets, "s3_targets": s3_targets, "name": name, "unset": unset,
            "user": user, "repo": repo, "branch": branch, "if_dropped": if_dropped}


def gate4_summary(run: str) -> dict:
    """Gate 4's short FaaSr line: repo, branch, deploy name and unset settings (no file list)."""
    g = _gate4_target(run)
    return {"user": g["user"], "repo": g["repo"], "branch": g["branch"], "name": g["copy_wf"]["WorkflowName"],
            "if_dropped": g["if_dropped"], "unset": g["unset"]}


def plan_lines(run: str) -> list[str]:
    """What Approve & deploy would write, for Gate 4: the deploy name, the repo files and the S3
    folders — the preflight's list without its checks. Local only: reads the run, its bundle and
    the deploy settings (~/.config/curate/.env); no GitHub or S3 call."""
    g = _gate4_target(run)
    copy_wf, targets, s3_targets, name, unset = g["copy_wf"], g["targets"], g["s3_targets"], g["name"], g["unset"]
    user, repo, branch = g["user"], g["repo"], g["branch"]
    lines = [f"Deploys as: `{copy_wf['WorkflowName']}` (name from: {name['source']})"
             + (f"; if you drop it at Gate 4 as a duplicate of {g['if_dropped']}, as `{g['if_dropped']}` instead "
                "(that version's deployment; the paths below then use that name)" if g["if_dropped"] else ""), "",
             f"GitHub (`{user}/{repo}`, branch `{branch}`), the version's deployment (written once, reused by "
             "its later runs while byte-identical):"]
    lines += [f"- `{t['path']}`" for t in targets if t["scope"] == "deployment"]
    lines += ["", "and this run's payload:"] + [f"- `{t['path']}`" for t in targets if t["scope"] == "run"]
    by_bucket: dict[str, list[dict]] = {}
    for t in s3_targets:
        if t["prefix"] or t.get("shown"):
            by_bucket.setdefault(t["bucket"] or "<FAASR_S3_BUCKET>", []).append(t)
    for bucket, ts in by_bucket.items():
        lines += ["", f"S3 (`{bucket}`), written when the workflow runs:"]
        lines += [f"- {t['kind']} folder: `{t['prefix'] or t['shown']}`" for t in ts]
    if unset:
        lines += ["", f"Not set yet in your deploy settings: {', '.join(unset)} (shown as placeholders above)."]
    return lines


# ── the four deploy steps ────────────────────────────────────────────────────
# Each one writes only after the user said yes to it (--confirmed), re-runs the preflight first
# (bundle hash, deploy-copy rule, names, existing paths), records each path it wrote in the deploy
# log as soon as it is written, and records the step in state.json. A finished step is skipped.

STEPS = ("publish", "upload", "register", "invoke")
STEP_DONE = {"publish": "published", "upload": "uploaded", "register": "registered", "invoke": "invoked"}


def progress_line(done) -> str:
    """The deploy steps finished so far, as shown to the user: `published · uploaded · …`."""
    return " · ".join(STEP_DONE[x] for x in STEPS if x in done)
STEP_WHAT = {
    "publish": "write the version's function files to the action repo (once per version)",
    "upload": "write this run's payload (the deploy copy of workflow.json) to the action repo",
    "register": "write the version's GitHub Actions files (CURATE's register_workflow) and its deployment marker",
    "invoke": "start the workflow: a workflow_dispatch of the entry action (CURATE's invoke_workflow)",
}
STEP_REUSE = {
    "publish": "use the version's deployment already in the repo (byte-identical): nothing is written",
    "register": "use the version's Actions files already in the repo (byte-identical): nothing is written",
}


class StepError(Exception):
    pass


def _no_leak(text: str) -> None:
    if leakcheck.find_leaks(text):
        raise StepError("refusing to write state.json: it would contain a secret value")


def _update_state(run: Path, change) -> dict:
    """Change the run's state (runstate.update: an execution's records go to its run)."""
    import runstate
    return runstate.update(run, change, _no_leak)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_written(run: Path, entry: dict) -> None:
    """Append one line to the run's deploy-log.jsonl (what --overwrite-own-deploy trusts)."""
    path = deploy_log_path(run)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": _now_iso(), **entry})
    if leakcheck.find_leaks(line):
        raise StepError("refusing to write the deploy log: the entry would contain a secret value")
    with open(path, "a") as fh:
        fh.write(line + "\n")


def blob_sha(content: str | bytes) -> str:
    """Git's blob id of a file's content: what GitHub's contents API returns as `sha`."""
    data = content.encode() if isinstance(content, str) else content
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _checked(content: str, what: str, extra: dict | None) -> str:
    leaked = leakcheck.find_leaks(content, extra_values=extra)
    if leaked:
        raise StepError(f"refusing to push {what}: it contains secret values ({', '.join(leaked)})")
    return content


def put_file(gh: GitHub, user: str, repo: str, path: str, content: str, message: str, branch: str) -> int:
    """Create or update one file, as PyGithub's create_file/update_file do for CURATE."""
    st, _, cur = gh.get(f"/repos/{user}/{repo}/contents/{path}", ref=branch)
    body = {"message": message, "content": base64.b64encode(content.encode()).decode(), "branch": branch}
    if st == 200 and isinstance(cur, dict) and cur.get("sha"):
        body["sha"] = cur["sha"]
    elif st != 404:
        raise StepError(f"could not read {path} before writing it (HTTP {st})")
    st, _, _ = gh.put(f"/repos/{user}/{repo}/contents/{path}", body)
    if st not in (200, 201):
        raise StepError(f"GitHub refused to write {path} (HTTP {st})")
    return st


@contextlib.contextmanager
def _vendored_env(token: str):
    """CURATE's register/invoke code reads GH_PAT from the environment; when the token came from
    `gh auth token`, provide it to this process only, and capture their log lines (redacted)."""
    old = os.environ.get("GH_PAT")
    os.environ["GH_PAT"] = token
    buf = io.StringIO()
    root = logging.getLogger()
    saved = [(h, h.stream) for h in root.handlers if isinstance(h, logging.StreamHandler)]
    handler = logging.StreamHandler(buf)
    root.addHandler(handler)
    for h, _ in saved:
        h.setStream(buf)
    try:
        yield buf
    finally:
        root.removeHandler(handler)
        for h, stream in saved:
            h.setStream(stream)
        if old is None:
            os.environ.pop("GH_PAT", None)
        else:
            os.environ["GH_PAT"] = old


def _log_tail(buf: io.StringIO, extra: dict | None, n: int = 15) -> list[str]:
    return leakcheck.redact(buf.getvalue(), extra_values=extra).splitlines()[-n:]


def run_register(copy_wf: dict, token: str, extra: dict | None) -> list[str]:
    """CURATE's register_workflow.main on the deploy copy; the YAML is leak-checked first."""
    from vendor.curate_deploy import register_workflow as rw
    secrets = rw.generate_github_secret_imports(copy_wf)
    user_secrets = rw.generate_user_defined_secret_imports(copy_wf)
    if user_secrets:
        secrets += "\n" + user_secrets
    for action in copy_wf["ActionList"]:
        _checked(rw.generate_serverless_yaml(f"{copy_wf['WorkflowName']}-{action}",
                                             copy_wf["ActionContainers"][action], secrets),
                 f"the Actions file for {action}", extra)
    with _vendored_env(token) as buf:
        try:
            rw.main(workflow_data=copy.deepcopy(copy_wf))
        except (SystemExit, Exception) as e:  # noqa: BLE001 — CURATE's code exits on errors
            raise StepError(f"register failed ({type(e).__name__}); its log: "
                            + " | ".join(_log_tail(buf, extra))) from None
    return _log_tail(buf, extra)


def run_invoke(workflow_path: str, token: str, extra: dict | None) -> dict:
    """CURATE's invoke_workflow.main: FaaSr's Scheduler dispatches the entry action."""
    from vendor.curate_deploy import invoke_workflow as iw
    with _vendored_env(token) as buf:
        try:
            payload = iw.main(workflow_path=workflow_path)
        except (SystemExit, Exception) as e:  # noqa: BLE001
            raise StepError(f"invoke failed ({type(e).__name__}); its log: "
                            + " | ".join(_log_tail(buf, extra))) from None
    # The payload holds the token (FaaSr adds it to the server entry); keep only the ids.
    return {"invocation_id": payload.get("InvocationID"),
            "invocation_timestamp": payload.get("InvocationTimestamp"), "log": _log_tail(buf, extra)}


RUNNERS = {"register": run_register, "invoke": run_invoke}


def _resume_cmd(s: dict) -> str:
    import runstate
    return f"/curate-skills:curate-deploy {runstate.flag(s, 'curate-deploy')}"


def _write_marker(gh: GitHub, user: str, repo: str, branch: str, m: dict, extra: dict | None) -> str:
    text = _checked(marker_text(m), marker_path(m["deploy_name"]), extra)
    put_file(gh, user, repo, marker_path(m["deploy_name"]), text,
             f"Deployment marker {m['deploy_name']} (curate-skills)", branch)
    return text


def deploy_step(run: str, step: str, name: dict, gh: GitHub | None, token: str | None,
                token_source: str, s3_factory=boto3_s3, confirmed: bool = False,
                overwrite_own: bool = False) -> dict:
    run_p = Path(run)
    try:
        s, bundle = load_run(run)
    except InputError as e:
        if str(e) != PEGASUS_REFUSAL:
            raise
        return {"run": str(run_p.resolve()), "step": step, "done": [], "ok": False, "error": PEGASUS_REFUSAL}
    rec = s.get("deploy") or {}
    steps_rec = rec.get("steps") or {}
    done = [x for x in STEPS if steps_rec.get(x, {}).get("ok")]
    reused = [x for x in done if steps_rec[x].get("reused")]
    base = {"run": str(run_p.resolve()), "step": step, "done": done}
    if s.get("stage") != "deploy":
        return {**base, "ok": False, "error": f"the run is at stage {s.get('stage')!r}, not deploy; "
                f"curate-deploy runs after Gate 4 is approved with Approve & deploy"}
    if s.get("deploy_choice") == "pegasus":
        return {**base, "ok": False, "error": PEGASUS_REFUSAL}
    if s.get("deploy_choice") != "yes":
        return {**base, "ok": False, "error": "Gate 4 was approved without deploying"}
    if step in done:
        return {**base, "ok": True, "skipped": True, "detail": f"{step} already done in this run",
                "next_step": next((x for x in STEPS if x not in done), None)}
    missing = [x for x in STEPS[:STEPS.index(step)] if x not in done]
    if missing:
        return {**base, "ok": False, "error": f"run {missing[0]} first (steps go publish → upload → "
                f"register → invoke)", "next_step": missing[0]}

    pf = preflight(run, name, gh, token_source, s3_factory, overwrite_own, done_steps=done, reused_steps=reused)
    copy_wf = pf.pop("_copy_wf")
    user, repo, branch = (os.environ.get("FAASR_GH_USERNAME", "").strip(),
                          os.environ.get("FAASR_ACTION_REPO", "").strip(),
                          os.environ.get("FAASR_ACTION_BRANCH", "").strip() or "main")
    new, rid = copy_wf["WorkflowName"], pf["run_id"]
    dep = pf.get("deployment") or {}
    # publish and register reuse the version's deployment when the repo has it, byte-identical
    # (or this run already reused it at publish)
    reuse = step in STEP_REUSE and (dep.get("state") == "same" or "publish" in reused)
    paths = [t["path"] for t in pf["targets"] if t["step"] == step]
    if reuse:
        paths = [marker_path(new)] if step == "publish" and dep.get("adds_project") else []
    if step == "invoke":
        paths = [f"s3://{t['bucket']}/{t['prefix'] or t['shown']}" for t in pf["s3_targets"]
                 if t["prefix"] or t.get("shown")]
    what = STEP_REUSE[step] if reuse else STEP_WHAT[step]
    if reuse and paths:
        what = STEP_REUSE[step].replace("nothing is written", "only this project is added to its marker")
    plan = {"what": what, "repo": pf["repo"], "branch": branch, "deploy_name": new, "paths": paths,
            **({"reuses": new} if reuse else {})}
    base.update(plan=plan, preflight_summary=pf["summary"])
    if not pf["ok"]:
        return {**base, "ok": False, "problems": pf["problems"],
                "error": "the preflight found problems; nothing was written"}
    if not confirmed:
        return {**base, "ok": False, "needs_confirmation": True,
                "error": f"{step} {'uses' if reuse and not paths else 'writes to'} {pf['repo']} — ask the user first; "
                         "only after they say yes, run it again with --confirmed"}

    import ids
    extra = {"gh_token": token} if token else None
    log_base = {"run_id": s["run_id"], "deploy_name": new, "step": step}
    _update_state(run_p, lambda st: st.update(deploy={
        **(st.get("deploy") or {}), "scheme": SCHEME, "deploy_name": new, "suffix": name["suffix"],
        "repo": pf["repo"], "branch": branch, "payload": payload_path(new, rid), "bundle_hash": pf["bundle_hash"],
        **({"deployment": "reused" if reuse else "made"} if step == "publish" else {}),
        "steps": {**((st.get("deploy") or {}).get("steps") or {}), step: {"ok": False, "started": _now_iso()}}}))
    detail: dict = {}
    seen = None  # the marker as this step saw or wrote it: recorded for `list` (which reads no repo)
    project = _project_of_state(s)
    try:
        if step in STEP_REUSE and reuse:
            detail = {"reused": new, "verified": "byte-identical"}
            if step == "publish":
                seen = {"projects": dep.get("projects") or [], "deployed_at": dep.get("deployed_at")}
            if step == "publish" and dep.get("adds_project"):
                marker, problem = read_marker(gh, user, repo, branch, new)
                if problem or marker is None:
                    raise StepError(problem or f"{marker_path(new)} is gone")
                marker["projects"] = sorted({*marker.get("projects", []), ids.project_id(project)})
                marker["updated_at"] = _now_iso()
                text = _write_marker(gh, user, repo, branch, marker, extra)
                _log_written(run_p, {**log_base, "repo": f"{user}/{repo}", "branch": branch,
                                     "path": marker_path(new), "sha": blob_sha(text), "shared": True})
                detail["written"] = [marker_path(new)]
                seen = {"projects": marker["projects"], "deployed_at": marker.get("deployed_at")}
        elif step == "publish":
            # Every file is read and leak-checked before the first one is pushed.
            files = []
            for fn, b in function_files(copy_wf):
                src = bundle / "functions" / b
                if not src.is_file():
                    raise StepError(f"the bundle has no functions/{b}")
                files.append((fn, f"functions/{new}/{b}", _checked(src.read_text(), f"functions/{b}", extra)))
            for fn, path, code in files:
                put_file(gh, user, repo, path, code, f"Add/update {fn} (curate-skills)", branch)
                _log_written(run_p, {**log_base, "repo": f"{user}/{repo}", "branch": branch, "path": path,
                                     "sha": blob_sha(code)})
            detail = {"written": paths}
        elif step == "upload":
            path = payload_path(new, rid)
            text = _checked(json.dumps(copy_wf, indent=2), path, extra)
            (files_dir(run_p) / "deploy").mkdir(exist_ok=True)
            (files_dir(run_p) / "deploy" / "workflow.json").write_text(text + "\n")
            put_file(gh, user, repo, path, text, f"Add workflow payload {new}/{rid} (curate-skills)", branch)
            _log_written(run_p, {**log_base, "repo": f"{user}/{repo}", "branch": branch, "path": path,
                                 "sha": blob_sha(text)})
            detail = {"written": [path], "local_copy": str(files_dir(run_p) / "deploy" / "workflow.json")}
        elif step == "register":
            log = RUNNERS["register"](copy_wf, token, extra)
            actions = expected_actions_files(copy_wf)
            shas = {k: blob_sha(v) for k, v in actions.items()}
            for path in paths:
                if path != marker_path(new):
                    _log_written(run_p, {**log_base, "repo": f"{user}/{repo}", "branch": branch, "path": path,
                                         **({"sha": shas[path]} if path in shas else {})})
            files = expected_deployment(bundle_functions(bundle, copy_wf), copy_wf)
            now = _now_iso()
            text = _write_marker(gh, user, repo, branch, {
                "curate_deployment": 1, "deploy_name": new, "workflow_id": s.get("workflow_id"),
                "content_hash": version_content_hash(s), "files": {k: blob_sha(v) for k, v in sorted(files.items())},
                "projects": [ids.project_id(project)], "deployed_at": now, "updated_at": now}, extra)
            _log_written(run_p, {**log_base, "repo": f"{user}/{repo}", "branch": branch, "path": marker_path(new),
                                 "sha": blob_sha(text)})
            detail = {"written": paths, "log": log}
            seen = {"projects": [ids.project_id(project)], "deployed_at": now}
        elif step == "invoke":
            dispatched = _now_iso()
            got = RUNNERS["invoke"](payload_path(new, rid), token, extra)
            for t in pf["s3_targets"]:
                if t["prefix"]:
                    _log_written(run_p, {**log_base, "bucket": t["bucket"], "prefix": t["prefix"]})
            logs = next(t for t in pf["s3_targets"] if t["kind"] == "logs")
            _log_written(run_p, {**log_base, "bucket": logs["bucket"],
                                 "prefix": logs_prefix(copy_wf, new, got["invocation_timestamp"], got["invocation_id"])})
            detail = {"invocation_id": got["invocation_id"], "invocation_timestamp": got["invocation_timestamp"],
                      "dispatched_at": dispatched, "log": got.get("log", [])}
    except (StepError, OSError) as e:
        err = leakcheck.redact(str(e), extra_values=extra)
        _update_state(run_p, lambda st: st["deploy"]["steps"].__setitem__(
            step, {"ok": False, "ts": _now_iso(), "error": err}))
        return {**base, "ok": False, "failed": True, "error": err,
                "resume_command": _resume_cmd(s),
                "note": "what this step already wrote is in the deploy log; retrying it needs "
                        "--overwrite-own-deploy (the user's choice)"}

    def finish(st):
        st["deploy"]["steps"][step] = {"ok": True, "ts": _now_iso(),
                                       **{k: v for k, v in detail.items() if k != "log"}}
        if seen:
            st["deploy"]["marker"] = {**seen, "seen": _now_iso()}
        if step == "invoke":
            st["deploy"].update(invocation_id=detail["invocation_id"],
                                invocation_timestamp=detail["invocation_timestamp"],
                                dispatched_at=detail["dispatched_at"], workflow_name=new)
            st.setdefault("history", []).append({
                "gate": "deploy", "decision": "deployed", "text": f"{new}/{rid} invocation {detail['invocation_id']}",
                "structural": None, "ts": _now_iso()})
            st.update(stage="monitor", begun=None)  # curate-monitor checks the run (read-only)
    s = _update_state(run_p, finish)
    done = [x for x in STEPS if s["deploy"]["steps"].get(x, {}).get("ok")]
    nxt = next((x for x in STEPS if x not in done), None)
    out = {**base, "ok": True, "done": done, "progress": progress_line(done), "next_step": nxt, **detail,
           "next_stage": s["stage"]}
    if step == "invoke":
        out.update(next_skill="curate-skills:curate-monitor",
                   message=f"Deployed: {new}, run {rid}, invocation {detail['invocation_id']}. Next: curate-monitor "
                           "checks the Actions runs, FaaSr logs and outputs (read-only).")
    return out


# ── the Actions files CURATE's generator writes ─────────────────────────────

def expected_actions_files(copy_wf: dict) -> dict[str, str]:
    """The Actions files register would write for the deploy copy, from CURATE's own generator.
    Generated twice: if the two differ, the deployed files cannot be verified byte for byte."""
    from vendor.curate_deploy import register_workflow as rw

    def generate():
        secrets = rw.generate_github_secret_imports(copy_wf)
        user = rw.generate_user_defined_secret_imports(copy_wf)
        if user:
            secrets += "\n" + user
        out = {}
        for action, a in copy_wf["ActionList"].items():
            name = f"{copy_wf['WorkflowName']}-{action}"
            gen = rw.generate_vm_yaml if a.get("RequiresVM") else rw.generate_serverless_yaml
            out[f".github/workflows/{name}.yml"] = gen(name, copy_wf["ActionContainers"][action], secrets)
        return out
    first, second = generate(), generate()
    if first != second:
        raise StepError("CURATE's Actions-file generator gave different output on two calls for the same "
                        "input, so the deployed Actions files cannot be verified byte for byte; refusing")
    return first


def deploy_status(run: str) -> dict:
    s, _ = load_run(run)
    rec = s.get("deploy") or {}
    steps = rec.get("steps") or {}
    done = [x for x in STEPS if steps.get(x, {}).get("ok")]
    failed = {x: steps[x].get("error") for x in STEPS if x in steps and not steps[x].get("ok") and steps[x].get("error")}
    return {"ok": True, "stage": s.get("stage"), "deploy_choice": s.get("deploy_choice"), "done": done,
            "progress": progress_line(done), "failed": failed,
            "next_step": None if is_legacy_deploy(s) else next((x for x in STEPS if x not in done), None),
            "deploy_name": rec.get("deploy_name"), "payload": rec.get("payload"), "deployment": rec.get("deployment"),
            "invocation_id": rec.get("invocation_id"), **({"per_run_name": True} if is_legacy_deploy(s) else {}),
            "deploy_log": str(deploy_log_path(run)) if deploy_log_path(run).is_file() else None}


# ── update-deployment: re-push the version's deployment files ───────────────

def _version_build(project: Path, wid: str) -> tuple[dict, dict, Path]:
    """(the FaaSr build of version `wid` in memory, a state for it, its folder): the same build a
    run's bundle comes from (execution.faasr_built), from the version's approved spec."""
    import execution as X
    import ids
    vdir = ids.version_dir(project, wid)
    if not (vdir / "workflow.yml").is_file():
        raise InputError(f"no version {wid} in {project}")
    try:
        spec, _ = X.approved_spec(project, wid, vdir)
        s = {"spec": spec, "workflow_id": wid, "version_dir": str(vdir), "version_hash": ids.version_hash(vdir)}
        return X.faasr_built(s, vdir), s, vdir
    except X.ExecutionError as e:
        raise InputError(str(e)) from None


def main_problems(built: dict, vdir: Path, s: dict) -> list[str]:
    """Every step's function file in the build (functions/<step>_main.py, which the wrapper calls)
    must be byte-identical to the version's own function code."""
    import execution as X
    import faasr_wrapper as W
    wf = X.load_workflow(vdir, s)
    out = []
    for sid, st in sorted(wf.steps.items()):
        code_rel = (st.function or {}).get("code")
        if not code_rel:
            continue
        got = built["files"].get(f"functions/{W.main_file(sid)}")
        want = (wf.base / str(code_rel)).read_bytes()
        if got is None or got.encode() != want:
            out.append(f"functions/{W.main_file(sid)} is not byte-identical to the version's {code_rel}")
    return out


def update_deployment(project: str | Path, wid: str, override: str | None, gh: GitHub | None, token: str | None,
                      confirmed: bool = False) -> dict:
    """Re-push the version's wrappers and Actions files where the repo's differ, after checking
    every main() is byte-identical to the version. Only for the same version: the marker must
    name this version's content hash. Payloads and S3 are never touched."""
    import ids
    project = Path(project).expanduser().resolve()
    built, s, vdir = _version_build(project, wid)
    sfx = override or ""
    if sfx and not SUFFIX_RX.match(sfx):
        raise InputError(f"deploy name suffix {sfx!r} must be letters, digits, '-' or '_'")
    name = wid + sfx
    user, repo, branch = (os.environ.get("FAASR_GH_USERNAME", "").strip(),
                          os.environ.get("FAASR_ACTION_REPO", "").strip(),
                          os.environ.get("FAASR_ACTION_BRANCH", "").strip() or "main")
    base = {"workflow_id": wid, "deploy_name": name, "repo": f"{user}/{repo}", "branch": branch}
    if gh is None or not user or not repo:
        return {**base, "ok": False, "error": "needs a GitHub token and FAASR_GH_USERNAME / FAASR_ACTION_REPO"}
    problems = main_problems(built, vdir, s)
    if (p := in_flight_problem(wid, _version_runs(project, wid, name))):
        problems.append(p)
    filled_wf, _, unset = fill_placeholders(built["workflow"])
    if unset:
        problems.append(f"placeholders not set: {', '.join(unset)}")
    copy_wf = deploy_copy(filled_wf, name, "<run-id>", user, repo, branch)[0]
    functions = {k.split("/", 1)[1]: v for k, v in built["files"].items() if k.startswith("functions/")}
    content_hash = ids.draft_content_hash(vdir)
    try:
        dep = deployment_state(gh, user, repo, branch, copy_wf, functions, content_hash)
    except StepError as e:
        return {**base, "ok": False, "problems": [str(e)], "error": "nothing was written"}
    marker = dep.get("marker")
    if dep["state"] == "unreadable":
        problems.append(dep["problem"])
    elif marker is None:
        problems.append(f"{marker_path(name)} is not in {user}/{repo}: there is no deployment of {wid} to update "
                        "(deploying a run of it writes one)")
    elif dep["state"] == "different":
        problems.append(f"the deployment {name!r} in {user}/{repo} was made from another version "
                        f"({marker.get('workflow_id')}), not {wid}; update-deployment only re-pushes the same "
                        "version. Tear it down from the project that made it, or deploy under another name with "
                        "CURATE_DEPLOY_NAME_SUFFIX")
    expected = expected_deployment(functions, copy_wf)
    push = sorted(set(dep["differs"]) | set(dep["missing"]))
    plan = {"what": f"re-push {len(push)} file(s) of the deployment {name} so they match version {wid}"
                    if push else f"nothing to do: the deployment {name} already matches version {wid}",
            "paths": push + ([marker_path(name)] if push else [])}
    base["plan"] = plan
    if problems:
        return {**base, "ok": False, "problems": problems, "error": "nothing was written"}
    if not push:
        return {**base, "ok": True, "message": plan["what"]}
    if not confirmed:
        return {**base, "ok": False, "needs_confirmation": True,
                "error": f"update-deployment writes to {user}/{repo} — ask the user first; only after they say yes, "
                         "run it again with --confirmed"}
    extra = {"gh_token": token} if token else None
    written = []
    try:
        texts = {p: _checked(expected[p], p, extra) for p in push}  # all checked before the first push
        for p in sorted(push, key=lambda x: not x.startswith("functions/")):
            put_file(gh, user, repo, p, texts[p], f"Update {p.rsplit('/', 1)[1]} to version {wid} (curate-skills)", branch)
            written.append(p)
        marker = {**marker, "files": {k: blob_sha(v) for k, v in sorted(expected.items())}, "updated_at": _now_iso(),
                  "projects": sorted({*marker.get("projects", []), ids.project_id(project)})}
        _write_marker(gh, user, repo, branch, marker, extra)
        written.append(marker_path(name))
    except StepError as e:
        return {**base, "ok": False, "failed": True, "written": written,
                "error": leakcheck.redact(str(e), extra_values=extra) + "; run update-deployment again"}
    return {**base, "ok": True, "written": written,
            "message": f"Updated the deployment {name}: {len(push)} file(s) re-pushed to match version {wid}."}


# ── list and teardown ────────────────────────────────────────────────────────

def _when(ts: str | None) -> str:
    """2026-11-05T06:44:37+00:00 → 2026-11-05 06:44 UTC."""
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return ts


RUN_STATUSES = ("active", "finished", "not invoked", "torn down")


def _run_status(st: dict) -> tuple[str, str | None]:
    """A run of a deployment: torn down (its payload and S3 files deleted; with the date), not
    invoked, finished (monitored to a verdict) or active (invoked, no verdict yet)."""
    dep = st.get("deploy") or {}
    gone = dep.get("teardown") or dep.get("deployment_teardown")
    if gone:
        return "torn down", gone.get("ts")
    if not ((dep.get("steps") or {}).get("invoke") or {}).get("ok"):
        return "not invoked", None
    mon = st.get("monitor") or {}
    if mon.get("finished") or mon.get("result") in ("success", "success_unconfirmed", "failure") \
            or st.get("stage") in ("finalize", "done"):
        return "finished", None
    return "active", None


def _first_ok(steps: dict) -> str | None:
    return min((steps[x]["ts"] for x in STEPS if steps.get(x, {}).get("ok") and steps[x].get("ts")), default=None)


def list_deployments(project: str | Path) -> dict:
    """Deployments recorded in this project's runs. One deployment per version: the version's
    deployment (name, repo, when deployed, the projects its marker listed when a run last saw it,
    whether `teardown --all` removed it) with its runs underneath, each with its status (active,
    finished, not invoked, torn down) and invocation. Older date-suffixed deployments (one per
    run): by the session that made them. Reads only this project's state; no repo or S3."""
    import ids
    import runstate
    project = Path(project).expanduser().resolve()
    # every session (with its current run's records) and every run not used through a session
    states = [st for d in ids.session_dirs(project) for st in [_read_state(d)] if st]
    states += [st for d in ids.execution_dirs(project) if runstate.resolve_run(d) == d.resolve()
               for st in [_read_state(d)] if st]
    rows: dict[tuple, dict] = {}
    for st in states:
        dep = st.get("deploy") or {}
        steps = dep.get("steps") or {}
        if not dep.get("deploy_name") or not any(steps.get(x, {}).get("ok") for x in STEPS):
            continue
        version = dep.get("scheme") == SCHEME
        key = (dep.get("repo"), dep["deploy_name"])
        row = rows.setdefault(key, {"name": dep["deploy_name"], "repo": dep.get("repo"),
                                    "scheme": "version" if version else "legacy", "deployed": None,
                                    "torn_down": None, "projects": None, "projects_seen": None, "runs": [],
                                    **({} if version else {"session": None, "also_invoked_by": []})})
        inv = steps.get("invoke") or {}
        status, when = _run_status(st)
        made = not (dep.get("reused_from") or dep.get("deployment") == "reused")
        run = {"run": runstate.handle(st) if version else st.get("run_id"),
               "run_id": (st.get("execution") or {}).get("run_id") or st.get("run_id"),
               "status": status, **({"torn_down": when} if when else {}),
               "invocation_id": dep.get("invocation_id") if inv.get("ok") else None,
               "invoked": inv.get("ts") if inv.get("ok") else None, "made_deployment": made}
        row["runs"].append(run)
        if version:
            marker = dep.get("marker") or {}
            if marker.get("seen") and (row["projects_seen"] or "") < marker["seen"]:
                row["projects"], row["projects_seen"] = marker.get("projects") or [], marker["seen"]
            first = marker.get("deployed_at") or (_first_ok(steps) if made else None)
            if first:
                row["deployed"] = min(filter(None, [row["deployed"], first]))
            gone = (dep.get("deployment_teardown") or {}).get("ts")
            if gone:
                row["torn_down"] = max(filter(None, [row["torn_down"], gone]))
        elif made:
            row["session"] = st.get("run_id")
            row["deployed"] = _first_ok(steps)
            row["torn_down"] = (dep.get("teardown") or {}).get("ts")
        else:
            row["also_invoked_by"].append(st.get("run_id"))
    for row in rows.values():
        row["runs"].sort(key=lambda r: (r["invoked"] or "~", r["run_id"] or ""))
        # deployed again after a teardown --all: the deployment is there again
        if row["scheme"] == "version" and row["torn_down"] and any(
                r["made_deployment"] and r["status"] != "torn down" for r in row["runs"]):
            row["torn_down"] = None
    out = sorted(rows.values(), key=lambda r: r["deployed"] or min((x["invoked"] or "" for x in r["runs"]), default=""))
    md = []
    for r in out:  # one block per deployment: long names and run ids don't fit a table row
        md += [f"**`{r['name']}`**" + (" — torn down" if r["torn_down"] else ""), f"- Repo: {r['repo']}",
               f"- Deployed: {_when(r['deployed'])}"]
        if r["scheme"] == "version":
            md.append("- Projects: " + (f"{', '.join(r['projects']) or '—'} (as of {_when(r['projects_seen'])})"
                                        if r["projects"] is not None else "not recorded (deployed before this was kept)"))
        else:
            md.append(f"- Session: {r['session'] or '(not in this project)'}")
            invoked = sorted(x["invoked"] for x in r["runs"] if x["invoked"])
            if invoked:
                md.append(f"- Last invoked: {_when(invoked[-1])} ({len(invoked)} invocation(s))")
            if r["also_invoked_by"]:
                md.append(f"- Also invoked by: {', '.join(r['also_invoked_by'])}")
        if r["torn_down"]:
            md.append(f"- Torn down: {_when(r['torn_down'])}")
        if r["scheme"] == "version":
            md.append("- Runs:")
            for x in r["runs"]:
                st_txt = x["status"] + (f" {_when(x['torn_down'])}" if x.get("torn_down") else "")
                inv = f" · invocation `{x['invocation_id']}`, {_when(x['invoked'])}" if x["invoked"] else ""
                md.append(f"  - `{x['run']}` — {st_txt}{inv}")
        md.append("")
    return {"ok": True, "project": str(project), "deployments": out,
            "markdown": "\n".join(md).rstrip() if out else "No deployments recorded in this project's runs."}


def _expected_repo_contents(run_p: Path, s: dict, bundle: Path) -> dict[str, str]:
    """For deploy logs without shas (written before teardown existed): what this run deployed,
    rebuilt from its own files. Empty when that can't be trusted (bundle changed, no deploy copy)."""
    copy_path = files_dir(run_p) / "deploy" / "workflow.json"
    if not copy_path.is_file() or tree_hash(bundle) != (s.get("deploy") or {}).get("bundle_hash"):
        return {}
    copy_wf = json.loads(copy_path.read_text())
    new = copy_wf["WorkflowName"]
    out = {f"functions/{new}/{b}": (bundle / "functions" / b).read_text()
           for _, b in function_files(copy_wf) if (bundle / "functions" / b).is_file()}
    out[f"workflows/{new}.json"] = json.dumps(copy_wf, indent=2)
    try:
        out.update(expected_actions_files(copy_wf))
    except Exception:  # noqa: BLE001
        pass
    return out


OUTPUT_CHOICES = {"keep": "keep the outputs: delete only the deploy files and the FaaSr logs (the default)",
                  "delete": "delete everything listed, outputs included",
                  "download": "download the outputs first (to --to, default <project>/teardown-outputs/<deploy name>), "
                              "then delete everything listed"}


def _run_workflow(run_p: Path, bundle: Path) -> dict:
    """The deploy copy this run uploaded, else the approved bundle with its placeholders filled."""
    copy_path = files_dir(run_p) / "deploy" / "workflow.json"
    return json.loads(copy_path.read_text()) if copy_path.is_file() else \
        fill_placeholders(json.loads((bundle / "workflow.json").read_text()))[0]


def _s3_client(wf: dict, bucket: str, s3_factory):
    cfg = next((c for c in (wf.get("DataStores") or {}).values() if c.get("Bucket") == bucket), None)
    if cfg is None:
        raise StepError(f"bucket {bucket!r} is not in this workflow; its S3 objects are not touched")
    keys, _, problems = s3_keys(cfg)
    if not keys:
        raise StepError("S3 cannot be read: " + "; ".join(problems))
    return s3_factory(cfg, *keys)


def _s3_client_for(run_p: Path, bundle: Path, bucket: str, s3_factory):
    return _s3_client(_run_workflow(run_p, bundle), bucket, s3_factory)


def _list_keys(client, bucket: str, prefix: str) -> list[str]:
    keys, token = [], None
    while True:
        r = client.list_objects_v2(Bucket=bucket, Prefix=prefix, **({"ContinuationToken": token} if token else {}))
        keys += [o["Key"] for o in r.get("Contents") or []]
        if not r.get("IsTruncated"):
            return keys
        token = r.get("NextContinuationToken")


def _has_faasr_deploy(s: dict) -> bool:
    return any(((s.get("deploy") or {}).get("steps") or {}).get(x, {}).get("ok") for x in STEPS)


def no_deployment_problem(run: str) -> str | None:
    """Why `run` has nothing for teardown --run, naming the runs or sessions that do hold this
    version's FaaSr deployment; None when it has one."""
    import runstate
    if not (Path(run) / "state.json").is_file():
        return (f"{run} is not a run: it has no state.json, and no session links it as its run "
                "(spec.py runs lists this project's sessions and runs)")
    s = runstate.load(run)
    if _has_faasr_deploy(s):
        return None
    me = runstate.handle(s) if runstate.is_execution(s) else (runstate.current_run(s) or s["run_id"])
    pegasus = s.get("deploy_choice") == "pegasus" or (s.get("execution") or {}).get("target") == "pegasus"
    why = "it ran on Pegasus" if pegasus else "no FaaSr deploy step of it ran"
    held = []
    for d, st in project_runs(run):
        if d.resolve() == Path(run).resolve() or not _has_faasr_deploy(st) or (st.get("deploy") or {}).get("teardown"):
            continue
        ref = runstate.handle(st) if runstate.is_execution(st) else (runstate.current_run(st) or st["run_id"])
        kind = "run" if runstate.is_execution(st) or runstate.current_run(st) else "session"
        held.append((st.get("workflow_id") == s.get("workflow_id"), f"{(st.get('deploy') or {}).get('deploy_name')} "
                     f"(the {kind} {ref}'s: teardown --run {ref})"))
    same = [h for same_v, h in held if same_v] or [h for _, h in held]
    return (f"{me} has no FaaSr deployment ({why}); nothing of it is on GitHub or S3. "
            + (f"This project's FaaSr deployment(s) not torn down: {'; '.join(same)}" if same else
               "This project has no other FaaSr deployment that is not torn down (deploy.py list shows them all)"))


def teardown_plan(run: str, gh: GitHub | None, s3_factory=boto3_s3) -> dict:
    """Read-only: exactly what teardown would delete — the repo files and the S3 objects under the
    prefixes this run's deploy log records — or why it refuses."""
    run_p = Path(run)
    if (problem := no_deployment_problem(run)):
        return {"ok": False, "run": str(run_p), "error": problem}
    s, bundle = load_run(run)
    dep = s.get("deploy") or {}
    base = {"run": str(run_p.resolve()), "run_id": s.get("run_id"), "deploy_name": dep.get("deploy_name")}
    if dep.get("reused_from"):
        return {**base, "ok": False, "error": f"this run reused run {dep['reused_from']}'s deployment; tear that "
                f"run down instead (/curate-skills:curate-deploy --teardown --run {dep['reused_from']})"}
    if dep.get("teardown"):
        return {**base, "ok": False, "error": f"already torn down ({dep['teardown'].get('ts')})"}
    entries, bad = read_deploy_log(run)
    mine = [e for e in entries if e.get("run_id") == s.get("run_id") and e.get("deploy_name") == dep.get("deploy_name")
            and e.get("step") != "teardown"]
    version_scheme = dep.get("scheme") == SCHEME
    if version_scheme:  # one deployment per version: a run's teardown removes only what is the run's own
        mine = [e for e in mine if not e.get("shared")
                and (not e.get("path") or e["path"] == dep.get("payload"))]
    if not deploy_log_path(run_p).is_file() or not mine:
        return {**base, "ok": False, "error": f"no deploy log for this run ({DEPLOY_LOG}): nothing is known to be "
                "this run's, so nothing is deleted"}
    problems = list(bad)
    files: dict[tuple, dict] = {}
    for e in mine:
        if e.get("path") and e.get("repo") and e.get("branch"):
            files[(e["repo"], e["branch"], e["path"])] = e  # the last line for a path wins
    prefixes = sorted({(e["bucket"], e["prefix"]) for e in mine if e.get("bucket") and e.get("prefix")})
    name, sfx = dep.get("deploy_name") or "", dep.get("suffix") or ""
    expected = None
    repo_files = []
    if files and gh is None:
        problems.append("no GitHub token: the repo files could not be checked")
    for (repo, branch, path), e in sorted(files.items()):
        row = {"repo": repo, "branch": branch, "path": path}
        if name not in path:
            problems.append(f"{path} is not under the deploy name {name!r}; refusing")
        if gh is None:
            repo_files.append(row)
            continue
        st, _, body = gh.get(f"/repos/{repo}/contents/{path}", ref=branch)
        if st == 404:
            repo_files.append({**row, "state": "already gone"})
            continue
        if st != 200 or not isinstance(body, dict) or not body.get("sha"):
            problems.append(f"{repo}/{path}: could not read it (HTTP {st})")
            continue
        want = e.get("sha")
        if not want:
            if expected is None:
                expected = _expected_repo_contents(run_p, s, bundle)
            want = blob_sha(expected[path]) if path in expected else None
        if want is None:
            problems.append(f"{path}: its content at deploy time is not known, so a change can't be ruled out; "
                            "refusing")
        elif body["sha"] != want:
            problems.append(f"{path} changed since this run deployed it; refusing")
        repo_files.append({**row, "state": "delete", "sha": body["sha"]})
    s3_objects, shared = [], owned_prefixes_elsewhere(run, s.get("run_id"))
    log_root = logs_prefix(_run_workflow(run_p, bundle), name)
    for bucket, prefix in prefixes:
        if not prefix.endswith("/") or not (prefix.rstrip("/").endswith(sfx) or f"/{name}/" in f"/{prefix}"):
            problems.append(f"s3://{bucket}/{prefix} is not a prefix of this deployment; refusing")
            continue
        if (bucket, prefix) in shared:
            problems.append(f"s3://{bucket}/{prefix} is also recorded by run {shared[(bucket, prefix)]}'s own "
                            "deployment; refusing")
            continue
        try:
            client = _s3_client_for(run_p, bundle, bucket, s3_factory)
            keys = _list_keys(client, bucket, prefix)
        except Exception as e:  # noqa: BLE001
            problems.append(f"could not list s3://{bucket}/{prefix}: {e if isinstance(e, StepError) else _s3_error(e)}")
            continue
        kind = "logs" if prefix.startswith(log_root) else "outputs"
        s3_objects += [{"bucket": bucket, "prefix": prefix, "key": k, "kind": kind} for k in keys]
    to_delete = [f for f in repo_files if f.get("state") == "delete"]
    plan_id = hashlib.sha256(json.dumps([[f["repo"], f["branch"], f["path"], f.get("sha")] for f in to_delete]
                                        + [[o["bucket"], o["key"]] for o in s3_objects]).encode()).hexdigest()[:12]
    reused_by = [st.get("run_id") for _, st in project_runs(run)
                 if (st.get("deploy") or {}).get("reused_from") == s.get("run_id")]
    title = (f"**Teardown of run `{run_id_of(s)}` of `{name}`** (its payload, outputs and logs; from its deploy log; "
             "nothing deleted yet)" if version_scheme else
             f"**Teardown of `{name}`** (run {s.get('run_id')}; from its deploy log; nothing deleted yet)")
    lines = [title, "", "**Deploy files** (repo):"]
    lines += [f"- `{f['repo']}` ({f['branch']}): `{f['path']}`" + (" — already gone" if f.get("state") == "already gone" else "")
              for f in repo_files] or ["- none"]
    for kind, title in (("logs", "**FaaSr logs** (S3)"), ("outputs", "**Outputs** (S3, what the workflow wrote)")):
        lines += ["", title + ":"]
        shown = False
        for bucket, prefix in prefixes:
            keys = [o["key"] for o in s3_objects if (o["bucket"], o["prefix"]) == (bucket, prefix) and o["kind"] == kind]
            if keys or (kind == "logs") == prefix.startswith(log_root):
                lines.append(f"- `s3://{bucket}/{prefix}`: {len(keys)} object(s)")
                lines += [f"  - `{k}`" for k in keys]
                shown = True
        if not shown:
            lines.append("- none")
    lines.append("")
    if reused_by:
        lines.append(f"- Runs that invoked this deployment too (their records stay): {', '.join(reused_by)}")
    if version_scheme:
        lines.append(f"- Not deleted: the version's deployment `{name}` (its function and Actions files; "
                     f"teardown --workflow {s.get('workflow_id')} --all removes it), this run's local folder and "
                     "its outputs/, the GitHub Actions run history, anything not listed above.")
    else:
        lines.append("- Not deleted: the GitHub Actions run history, anything not listed above.")
    lines += [f"- **Problem:** {x}" for x in problems]
    return {**base, "ok": not problems, "scheme": dep.get("scheme"), "problems": problems, "repo_files": repo_files, "s3_objects": s3_objects,
            "counts": {"repo_files": len(to_delete), "s3_objects": len(s3_objects),
                       "log_objects": sum(o["kind"] == "logs" for o in s3_objects),
                       "output_objects": sum(o["kind"] == "outputs" for o in s3_objects)},
            "outputs_choices": OUTPUT_CHOICES, "plan_id": plan_id,
            "summary": "\n".join(lines)}


def owned_prefixes_elsewhere(run: str, run_id: str) -> dict[tuple, str]:
    """(bucket, prefix) → run id, for prefixes other runs' own (not reused, not torn down) deploy logs record."""
    out = {}
    for d, st in project_runs(run):
        dep = st.get("deploy") or {}
        if st.get("run_id") == run_id or dep.get("reused_from") or dep.get("teardown"):
            continue
        for e in read_deploy_log(str(d))[0]:
            if e.get("run_id") == st.get("run_id") and e.get("bucket") and e.get("prefix"):
                out[(e["bucket"], e["prefix"])] = st.get("run_id")
    return out


def _download(wf: dict, objects: list[dict], dest: Path, s3_factory, run_p: Path | None = None,
              strip: str = "", deliver: dict | None = None, ident: dict | None = None) -> dict:
    """Copy output objects to a local folder, with a manifest. Nothing is deleted here; a file
    holding a secret value is not saved (and so not deleted either). With `strip` (the run's own
    S3 folder), dest is the run's outputs/ and is filled in (outputs_dir: same bytes kept, missing
    added, a hand-edited file never overwritten), as `monitor.py download-outputs --all` fills it:
    every object under _all/, and each deliverable (`deliver`: key -> deliver_as) at its
    deliver_as path too; the manifest keeps the run's identity (`ident`). Otherwise dest must be an
    empty folder outside the run, and objects keep their S3 keys."""
    import outputs_dir as O
    dest = dest.expanduser().resolve()
    earlier = {}
    if strip:
        earlier = O.read_manifest(dest)
    else:
        run_abs = run_p.resolve() if run_p else None
        if run_abs and (dest == run_abs or run_abs in dest.parents):
            raise StepError(f"{dest} is inside the run folder; choose a folder outside it")
        if dest.exists() and any(dest.iterdir()):
            raise StepError(f"{dest} is not empty; nothing is overwritten")
    files, skipped, clients = [], [], {}
    for o in objects:
        if o["bucket"] not in clients:
            clients[o["bucket"]] = _s3_client(wf, o["bucket"], s3_factory)
        data = clients[o["bucket"]].get_object(Bucket=o["bucket"], Key=o["key"])["Body"].read()
        leaked = leakcheck.find_leaks(data.decode("utf-8", "ignore"))
        if leaked:
            skipped.append({"key": o["key"], "reason": f"contains secret values ({', '.join(leaked)})"})
            continue
        rel = o["key"][len(strip):] if strip and o["key"].startswith(strip) else o["key"]
        places = [(f"_all/{rel}", "all")] if strip else [(rel, "file")]
        if strip and (deliver or {}).get(o["key"]):
            places.append((deliver[o["key"]], "deliverable"))
        for where, kind in places:
            if O.place(dest, where, data) == "differs":
                skipped.append({"key": o["key"], "reason": f"{dest / where} differs from S3; not overwritten"})
                continue
            files.append({"key": o["key"], "kind": kind, "bytes": len(data), "sha256": O.sha256(data),
                          "saved_as": str(dest / where)})
    now = _now_iso()
    keep = {k: v for k, v in {**earlier, **(ident or {})}.items() if k in ("run", "target", "invocation_id", "bucket")}
    path = O.write_manifest(dest, {**keep, "downloaded_at": now, "by": "teardown", "files": files,
                                   "skipped": skipped, "copies": [*(earlier.get("copies") or []), now]})
    return {"to": str(dest), "files": len({f["key"] for f in files} - {x["key"] for x in skipped}),
            "skipped": skipped, "manifest": str(path)}


def teardown(run: str, gh: GitHub | None, token: str | None, s3_factory=boto3_s3, confirmed: bool = False,
             plan_id: str | None = None, outputs: str = "keep", to: str | None = None) -> dict:
    """Delete what teardown_plan lists, only with --confirmed and the plan id the user saw.
    outputs: keep (default: delete only the deploy files and FaaSr logs) / delete / download
    (save them first; if any can't be saved, nothing is deleted)."""
    if outputs not in OUTPUT_CHOICES:
        raise InputError(f"--outputs must be one of {', '.join(OUTPUT_CHOICES)}")
    plan = teardown_plan(run, gh, s3_factory)
    if not plan["ok"]:
        return {**plan, "error": plan.get("error") or "teardown refused; nothing was deleted"}
    if not confirmed:
        return {**plan, "ok": False, "needs_confirmation": True,
                "error": "teardown deletes the files above — ask the user once (keep outputs / delete all / "
                         "download outputs first); only after they choose, run it again with --confirmed --plan "
                         f"{plan['plan_id']} --outputs keep|delete|download"}
    if plan_id != plan["plan_id"]:
        return {**plan, "ok": False, "error": "what would be deleted changed since it was shown (or no --plan "
                "given); show the new list and ask again"}
    run_p, extra = Path(run), ({"gh_token": token} if token else None)
    s, bundle = load_run(run)
    deleted = {"repo_files": [], "s3_objects": []}
    out_objs = [o for o in plan["s3_objects"] if o["kind"] == "outputs"]
    kept = out_objs if outputs == "keep" else []
    saved = None
    if outputs == "download" and out_objs:
        own = plan.get("scheme") == SCHEME and not to
        dest = (files_dir(run_p, s) / "outputs" if own else
                Path(to) if to else _project_of(run_p) / "teardown-outputs" / plan["deploy_name"])
        strip = f"{plan['deploy_name']}/{run_id_of(s)}/" if own else ""
        try:
            rwf = _run_workflow(run_p, bundle)
            deliver, ident = None, None
            if own:
                import monitor
                deliver = {d["key"]: d["deliver_as"] for d in monitor.built_deliverables(run_p, s, rwf) or []
                           if d["deliver_as"] not in monitor.RESERVED
                           and d["deliver_as"].split("/", 1)[0] != monitor.ALL_DIR}
                ident = {"run": s.get("run_id"), "target": "faasr",
                         "invocation_id": (s.get("deploy") or {}).get("invocation_id")}
            saved = _download(rwf, out_objs, dest, s3_factory, run_p, strip, deliver, ident)
        except Exception as e:  # noqa: BLE001
            err = str(e) if isinstance(e, StepError) else _s3_error(e)
            return {**plan, "ok": False, "error": f"downloading the outputs failed ({err}); nothing was deleted"}
        if saved["skipped"]:
            return {**plan, "ok": False, "downloaded": saved,
                    "error": "some outputs could not be saved (" + "; ".join(f"{x['key']}: {x['reason']}"
                             for x in saved["skipped"]) + "); nothing was deleted"}
    to_delete_s3 = [o for o in plan["s3_objects"] if o not in kept]
    try:
        files = sorted((f for f in plan["repo_files"] if f.get("state") == "delete"),
                       key=lambda f: not f["path"].startswith(".github/"))  # Actions files first
        for f in files:
            st, _, _ = gh.delete(f"/repos/{f['repo']}/contents/{f['path']}",
                                 {"message": f"Remove {f['path']} (curate-skills teardown)", "sha": f["sha"],
                                  "branch": f["branch"]})
            if st not in (200, 204):
                raise StepError(f"GitHub refused to delete {f['path']} (HTTP {st})")
            _log_written(run_p, {"run_id": s["run_id"], "deploy_name": plan["deploy_name"], "step": "teardown",
                                 "repo": f["repo"], "branch": f["branch"], "deleted": f["path"]})
            deleted["repo_files"].append(f["path"])
        clients = {}
        for o in to_delete_s3:
            if o["bucket"] not in clients:
                clients[o["bucket"]] = _s3_client_for(run_p, bundle, o["bucket"], s3_factory)
            clients[o["bucket"]].delete_object(Bucket=o["bucket"], Key=o["key"])
            deleted["s3_objects"].append(f"s3://{o['bucket']}/{o['key']}")
        for bucket, prefix in sorted({(o["bucket"], o["prefix"]) for o in to_delete_s3}):
            _log_written(run_p, {"run_id": s["run_id"], "deploy_name": plan["deploy_name"], "step": "teardown",
                                 "bucket": bucket, "deleted_prefix": prefix,
                                 "objects": sum(1 for o in to_delete_s3 if o["prefix"] == prefix)})
    except Exception as e:  # noqa: BLE001
        err = leakcheck.redact(str(e) if isinstance(e, StepError) else _s3_error(e), extra_values=extra)
        _update_state(run_p, lambda st: st["deploy"].update(teardown_partial={"ts": _now_iso(), "error": err,
                                                                               **deleted}))
        return {**plan, "ok": False, "failed": True, "deleted": deleted, "error": f"teardown stopped: {err}; "
                "what was deleted is in the deploy log; run teardown again to see what is left"}
    kept_prefixes = sorted({f"s3://{o['bucket']}/{o['prefix']}" for o in kept})
    _update_state(run_p, lambda st: st["deploy"].update(teardown={
        "ts": _now_iso(), "outputs": outputs, "repo_files": len(deleted["repo_files"]),
        "s3_objects": len(deleted["s3_objects"]), "outputs_kept": kept_prefixes,
        **({"outputs_saved_to": saved["to"]} if saved else {})}))
    msg = (f"Torn down {plan['deploy_name']}: {len(deleted['repo_files'])} repo file(s), "
           f"{len(deleted['s3_objects'])} S3 object(s) deleted.")
    if kept:
        msg += f" Outputs kept ({len(kept)} object(s)): {', '.join(kept_prefixes)}."
    if saved:
        msg += f" Outputs saved first to {saved['to']} ({saved['files']} file(s), manifest.json)."
    return {**plan, "ok": True, "deleted": deleted, "outputs": outputs, "kept": [o["key"] for o in kept],
            **({"downloaded": saved} if saved else {}), "message": msg}


# ── teardown of a version's whole deployment (--workflow <wid> --all) ───────

def _version_runs(project: Path, wid: str, name: str) -> list[tuple[Path, dict]]:
    """Every session and run of this project that deployed version `wid` as `name` (one deployment
    per version)."""
    import ids
    import runstate
    out = [(d, st) for d in ids.session_dirs(project) for st in [_read_state(d)] if st]
    out += [(d, st) for d in ids.execution_dirs(project) if runstate.resolve_run(d) == d.resolve()
            for st in [_read_state(d)] if st]
    return [(d, st) for d, st in out if st.get("workflow_id") == wid
            and (st.get("deploy") or {}).get("scheme") == SCHEME and st["deploy"].get("deploy_name") == name]


def in_flight(st: dict) -> bool:
    """Invoked, not torn down, and the monitor has not seen it finish: its Actions may still run."""
    dep = st.get("deploy") or {}
    return bool(((dep.get("steps") or {}).get("invoke") or {}).get("ok")) and not dep.get("teardown") \
        and not (st.get("monitor") or {}).get("finished")


def in_flight_problem(wid: str, runs: list[tuple[Path, dict]]) -> str | None:
    """Refuse while a run of the version may still be running: FaaSr reads the deployment's
    wrappers, Actions files and the run's payload from the branch at every action's start."""
    flying = [st.get("execution", {}).get("reference") or st.get("run_id") for _, st in runs if in_flight(st)]
    if not flying:
        return None
    return (f"run(s) of {wid} may still be running: {', '.join(flying)} (FaaSr reads the deployment's files from the "
            "branch at every action's start); refusing until the monitor has seen each one finish "
            "(/curate-skills:curate-monitor --run <run>). For a run that is stuck or timed out, tear that run down "
            "first: " + "; ".join(f"/curate-skills:curate-deploy --teardown --run {r}" for r in flying))


def _version_workflow(project: Path, wid: str, runs: list[tuple[Path, dict]]) -> dict:
    """A deploy copy of the version (for its data stores): a run's, else built from the version."""
    for d, st in runs:
        copy_path = files_dir(d, st) / "deploy" / "workflow.json"
        if copy_path.is_file():
            return json.loads(copy_path.read_text())
    return fill_placeholders(_version_build(project, wid)[0]["workflow"])[0]


def teardown_version_plan(project: str | Path, wid: str, override: str | None, gh: GitHub | None,
                          s3_factory=boto3_s3) -> dict:
    """Read-only: what teardown --workflow <wid> --all would delete — the deployment (its marker's
    files, the marker), every run's payload in workflows/<Name>/, and S3 under <Name>/ and
    FaaSrLog/<Name>/ — or why it refuses. Local run folders (and their outputs/) are never deleted."""
    import ids
    project = Path(project).expanduser().resolve()
    vdir = ids.version_dir(project, wid)
    if not (vdir / "workflow.yml").is_file():
        raise InputError(f"no version {wid} in {project}")
    name = wid + (override or "")
    user, repo, branch = (os.environ.get("FAASR_GH_USERNAME", "").strip(),
                          os.environ.get("FAASR_ACTION_REPO", "").strip(),
                          os.environ.get("FAASR_ACTION_BRANCH", "").strip() or "main")
    full = f"{user}/{repo}"
    base = {"workflow_id": wid, "deploy_name": name, "repo": full, "branch": branch, "all": True}
    if gh is None or not user or not repo:
        return {**base, "ok": False, "error": "needs a GitHub token and FAASR_GH_USERNAME / FAASR_ACTION_REPO"}
    runs = _version_runs(project, wid, name)
    problems, warnings = [], []
    if (p := in_flight_problem(wid, runs)):
        problems.append(p)
    marker, problem = read_marker(gh, user, repo, branch, name)
    if problem:
        problems.append(problem)
    elif marker is None:
        problems.append(f"{marker_path(name)} is not in {full}: nothing is known to be {wid}'s deployment, so "
                        "nothing is deleted")
    elif marker.get("content_hash") != ids.draft_content_hash(vdir):
        problems.append(f"the deployment {name!r} in {full} was made from another version "
                        f"({marker.get('workflow_id')}); refusing — tear it down from the project that made it")
    marker = marker or {}
    pid = ids.project_id(project)
    others = sorted(set(marker.get("projects") or []) - {pid})
    if others:
        warnings.append(f"the deployment's marker lists {len(others)} other project(s) using it ({', '.join(others)}); "
                        "their runs could not start again on it")
    repo_files = []
    for path, want in sorted((marker.get("files") or {}).items()):
        if not path.startswith((f"functions/{name}/", f".github/workflows/{name}-")):
            problems.append(f"{path} (in the marker) is not under the deploy name {name!r}; refusing")
            continue
        st, _, body = gh.get(f"/repos/{full}/contents/{path}", ref=branch)
        if st == 404:
            repo_files.append({"path": path, "state": "already gone", "kind": "deployment"})
        elif st != 200 or not isinstance(body, dict):
            problems.append(f"{path}: could not read it (HTTP {st})")
        else:
            if body.get("sha") != want:
                problems.append(f"{path} changed since the deployment was written (not the marker's sha); refusing")
            repo_files.append({"path": path, "state": "delete", "sha": body.get("sha"), "kind": "deployment"})
    st, _, listing = gh.get(f"/repos/{full}/contents/workflows/{name}", ref=branch)
    if st == 200 and isinstance(listing, list):
        for item in sorted(listing, key=lambda x: x.get("path") or ""):
            path = item.get("path") or ""
            if path == marker_path(name):
                continue
            if not path.endswith(".json") or item.get("type", "file") != "file":
                problems.append(f"{path} in workflows/{name}/ is not a run payload; refusing")
                continue
            s2, _, body = gh.get(f"/repos/{full}/contents/{path}", ref=branch)
            try:
                wname = json.loads(base64.b64decode((body or {}).get("content") or "")).get("WorkflowName")
            except (ValueError, AttributeError):
                wname = None
            if s2 != 200 or wname != name:
                problems.append(f"{path} is not a payload of {name!r} (WorkflowName {wname!r}); refusing")
                continue
            repo_files.append({"path": path, "state": "delete", "sha": body.get("sha"), "kind": "payload"})
    elif st not in (200, 404):
        problems.append(f"could not list workflows/{name}/ (HTTP {st})")
    if marker.get("_sha"):
        repo_files.append({"path": marker_path(name), "state": "delete", "sha": marker["_sha"], "kind": "marker"})
    s3_objects = []
    try:
        wf = _version_workflow(project, wid, runs)
    except InputError as e:
        wf = {}
        problems.append(f"S3 not checked: {e}")
    store = (wf.get("DataStores") or {}).get(wf.get("DefaultDataStore") or "S3") or {}
    bucket = store.get("Bucket")
    prefixes = [(bucket, f"{name}/", "outputs"), (bucket, logs_prefix(wf, name), "logs")] if bucket else []
    for b, prefix, kind in prefixes:
        try:
            keys = _list_keys(_s3_client(wf, b, s3_factory), b, prefix)
        except Exception as e:  # noqa: BLE001
            problems.append(f"could not list s3://{b}/{prefix}: {e if isinstance(e, StepError) else _s3_error(e)}")
            continue
        s3_objects += [{"bucket": b, "prefix": prefix, "key": k, "kind": kind} for k in keys]
    to_delete = [f for f in repo_files if f.get("state") == "delete"]
    plan_id = hashlib.sha256(json.dumps([[f["path"], f.get("sha")] for f in to_delete]
                                        + [[o["bucket"], o["key"]] for o in s3_objects] + [others]).encode()).hexdigest()[:12]
    lines = [f"**Teardown of the deployment `{name}`** (version {wid}, all its runs; nothing deleted yet)", "",
             f"**Deployment files** (`{full}`, {branch}):"]
    lines += [f"- `{f['path']}`" + (" — already gone" if f["state"] == "already gone" else "")
              for f in repo_files if f["kind"] != "payload"] or ["- none"]
    lines += ["", "**Run payloads** (repo):"] + ([f"- `{f['path']}`" for f in repo_files if f["kind"] == "payload"]
                                                 or ["- none"])
    for kind, title in (("logs", "**FaaSr logs** (S3)"), ("outputs", "**Outputs** (S3, every run's)")):
        keys = [o for o in s3_objects if o["kind"] == kind]
        lines += ["", title + ":"] + ([f"- `s3://{b}/{x}`: {len(keys)} object(s)" for b, x, k in prefixes if k == kind]
                                      or ["- none"])
    lines += ["", f"- Runs of {wid} in this project (their local folders and outputs/ stay): "
              + (", ".join(st.get("execution", {}).get("reference") or st.get("run_id") for _, st in runs) or "none"),
              "- Not deleted: local run folders, the GitHub Actions run history, anything not listed above."]
    lines += [f"- **Warning:** {x}" for x in warnings] + [f"- **Problem:** {x}" for x in problems]
    return {**base, "ok": not problems, "problems": problems, "warnings": warnings, "other_projects": others,
            "repo_files": repo_files, "s3_objects": s3_objects, "runs": [str(d) for d, _ in runs],
            "counts": {"repo_files": len(to_delete), "s3_objects": len(s3_objects),
                       "log_objects": sum(o["kind"] == "logs" for o in s3_objects),
                       "output_objects": sum(o["kind"] == "outputs" for o in s3_objects)},
            "outputs_choices": OUTPUT_CHOICES, "plan_id": plan_id, "summary": "\n".join(lines),
            "_wf": wf}


def teardown_version(project: str | Path, wid: str, override: str | None, gh: GitHub | None, token: str | None,
                     s3_factory=boto3_s3, confirmed: bool = False, plan_id: str | None = None,
                     outputs: str = "keep", to: str | None = None, other_projects: bool = False) -> dict:
    """Delete what teardown_version_plan lists, only with --confirmed and the plan id the user saw
    (and --other-projects when the marker lists other projects)."""
    if outputs not in OUTPUT_CHOICES:
        raise InputError(f"--outputs must be one of {', '.join(OUTPUT_CHOICES)}")
    project = Path(project).expanduser().resolve()
    plan = teardown_version_plan(project, wid, override, gh, s3_factory)
    wf = plan.pop("_wf", {})
    if not plan["ok"]:
        return {**plan, "error": plan.get("error") or "teardown refused; nothing was deleted"}
    if not confirmed:
        return {**plan, "ok": False, "needs_confirmation": True,
                "error": "teardown deletes the files above — ask the user once (keep outputs / delete all / download "
                         "outputs first)" + (" and, separately, whether the other projects listed may lose this "
                                             "deployment" if plan["other_projects"] else "")
                         + f"; only after they choose, run it again with --confirmed --plan {plan['plan_id']} "
                           "--outputs keep|delete|download" + (" --other-projects" if plan["other_projects"] else "")}
    if plan_id != plan["plan_id"]:
        return {**plan, "ok": False, "error": "what would be deleted changed since it was shown (or no --plan given); "
                "show the new list and ask again"}
    if plan["other_projects"] and not other_projects:
        return {**plan, "ok": False, "needs_confirmation": True,
                "error": "the marker lists other projects using this deployment; ask the user whether to remove it "
                         "anyway, and only after they say yes add --other-projects"}
    extra = {"gh_token": token} if token else None
    out_objs = [o for o in plan["s3_objects"] if o["kind"] == "outputs"]
    kept = out_objs if outputs == "keep" else []
    saved = None
    if outputs == "download" and out_objs:
        dest = Path(to) if to else project / "teardown-outputs" / plan["deploy_name"]
        try:
            saved = _download(wf, out_objs, dest, s3_factory)
        except Exception as e:  # noqa: BLE001
            err = str(e) if isinstance(e, StepError) else _s3_error(e)
            return {**plan, "ok": False, "error": f"downloading the outputs failed ({err}); nothing was deleted"}
        if saved["skipped"]:
            return {**plan, "ok": False, "downloaded": saved,
                    "error": "some outputs could not be saved (" + "; ".join(f"{x['key']}: {x['reason']}"
                             for x in saved["skipped"]) + "); nothing was deleted"}
    order = {"deployment": 0, "payload": 1, "marker": 2}  # Actions files first, the marker last
    files = sorted((f for f in plan["repo_files"] if f["state"] == "delete"),
                   key=lambda f: (order[f["kind"]], not f["path"].startswith(".github/"), f["path"]))
    deleted = {"repo_files": [], "s3_objects": []}
    try:
        for f in files:
            st, _, _ = gh.delete(f"/repos/{plan['repo']}/contents/{f['path']}",
                                 {"message": f"Remove {f['path']} (curate-skills teardown)", "sha": f["sha"],
                                  "branch": plan["branch"]})
            if st not in (200, 204):
                raise StepError(f"GitHub refused to delete {f['path']} (HTTP {st})")
            deleted["repo_files"].append(f["path"])
        clients = {}
        for o in [o for o in plan["s3_objects"] if o not in kept]:
            if o["bucket"] not in clients:
                clients[o["bucket"]] = _s3_client(wf, o["bucket"], s3_factory)
            clients[o["bucket"]].delete_object(Bucket=o["bucket"], Key=o["key"])
            deleted["s3_objects"].append(f"s3://{o['bucket']}/{o['key']}")
    except Exception as e:  # noqa: BLE001
        err = leakcheck.redact(str(e) if isinstance(e, StepError) else _s3_error(e), extra_values=extra)
        return {**plan, "ok": False, "failed": True, "deleted": deleted,
                "error": f"teardown stopped: {err}; run it again to see what is left"}
    ts = _now_iso()
    record = {"ts": ts, "outputs": outputs, "repo_files": len(deleted["repo_files"]),
              "s3_objects": len(deleted["s3_objects"]), **({"outputs_saved_to": saved["to"]} if saved else {})}
    for d in plan["runs"]:
        _update_state(Path(d), lambda st: st["deploy"].update(
            deployment_teardown=record, teardown=st["deploy"].get("teardown") or {**record, "by": "--all"}))
    msg = (f"Torn down the deployment {plan['deploy_name']}: {len(deleted['repo_files'])} repo file(s), "
           f"{len(deleted['s3_objects'])} S3 object(s) deleted.")
    if kept:
        msg += f" Outputs kept ({len(kept)} object(s)) under s3://{kept[0]['bucket']}/{plan['deploy_name']}/."
    if saved:
        msg += f" Outputs saved first to {saved['to']} ({saved['files']} file(s), manifest.json)."
    return {**plan, "ok": True, "deleted": deleted, "outputs": outputs, "message": msg,
            **({"downloaded": saved} if saved else {})}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print(obj: dict, extra_values: dict | None = None) -> int:
    text = json.dumps(obj, indent=2)
    leaked = leakcheck.find_leaks(text, extra_values=extra_values)
    if leaked:
        text = json.dumps({"ok": False, "error": "output withheld: contains secret values",
                           "leaked_secret_names": leaked})
        print(text)
        return 1
    print(text)
    return 0 if obj.get("ok") else 1


def main(argv=None, opener=None, s3_factory=boto3_s3) -> int:
    p = argparse.ArgumentParser(prog="deploy.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    suffix_help = ("appended to the deploy name, the version's workflow id (default: $CURATE_DEPLOY_NAME_SUFFIX, "
                   "else none)")
    for name, help_ in [("preflight", "read-only check of what deploying would do"),
                        *[(x, STEP_WHAT[x] + " (needs --confirmed)") for x in STEPS]]:
        q = sub.add_parser(name, help=help_)
        q.add_argument("--run", required=True, help=RUN_HELP)
        q.add_argument("--name-suffix", default=None, help=suffix_help)
        q.add_argument("--overwrite-own-deploy", action="store_true",
                       help="allow existing paths only if this run's own deploy log records them (and, in "
                            "the repo, they still carry its mark); everything else is still refused")
        if name in STEPS:
            q.add_argument("--confirmed", action="store_true",
                           help="the user said yes to this step (passed only after asking them)")
    sub.add_parser("status", help="which deploy steps are done").add_argument("--run", required=True, help=RUN_HELP)
    q = sub.add_parser("list", help="deployments recorded in this project's runs (read-only)")
    q.add_argument("--project", default=".", help="the project folder (default: the current one)")
    q = sub.add_parser("update-deployment", help="re-push the version's function and Actions files where the repo's "
                                                 "differ, after checking every main() against the version (needs "
                                                 "--confirmed; the same version only)")
    q.add_argument("--workflow", required=True, help="the version's workflow id")
    q.add_argument("--project", default=".", help="the project folder (default: the current one)")
    q.add_argument("--name-suffix", default=None, help=suffix_help)
    q.add_argument("--confirmed", action="store_true")
    q = sub.add_parser("teardown", help="delete one run's payload, outputs and logs (--run), or a version's whole "
                                        "deployment and every run's (--workflow <wid> --all); needs --confirmed and "
                                        "the --plan id shown")
    q.add_argument("--run", help=RUN_HELP)
    q.add_argument("--workflow", help="with --all: the version whose deployment goes")
    q.add_argument("--all", action="store_true", help="with --workflow: the deployment and every run's files")
    q.add_argument("--project", default=".", help="with --workflow: the project folder (default: the current one)")
    q.add_argument("--name-suffix", default=None, help=suffix_help)
    q.add_argument("--confirmed", action="store_true")
    q.add_argument("--other-projects", action="store_true",
                   help="with --all: the user agreed to remove a deployment other projects are listed as using")
    q.add_argument("--plan", help="the plan_id of the list the user saw")
    q.add_argument("--outputs", default="keep", choices=sorted(OUTPUT_CHOICES),
                   help="keep (default): delete only deploy files and FaaSr logs; delete: everything; "
                        "download: save the outputs first, then delete everything")
    q.add_argument("--to", help="with --outputs download: a folder instead of the default (a run: its own outputs/; "
                                "--all: <project>/teardown-outputs/<deploy name>)")
    args = p.parse_args(argv)
    if args.cmd == "teardown" and not args.run and not (args.workflow and args.all):
        print(json.dumps({"ok": False, "error": "teardown takes --run <workflow-id>/<run-id>, or --workflow "
                                                "<workflow-id> --all"}))
        return 2
    if args.cmd == "teardown" and args.run and args.workflow:
        print(json.dumps({"ok": False, "error": "teardown takes --run or --workflow --all, not both"}))
        return 2
    import runstate  # --run: <workflow-id>/<run-id>, a run id or folder; a session: its current run
    if runstate.resolve_args(args, "run", note=True):
        return 2
    envfile.load()  # ~/.config/curate/.env fills deploy variables the environment leaves unset
    if args.cmd in ("status", "list"):
        try:
            return _print(deploy_status(args.run) if args.cmd == "status" else list_deployments(args.project))
        except InputError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
    token, source = get_token()
    extra = {"gh_token": token} if token else None
    override = args.name_suffix if args.name_suffix is not None else os.environ.get("CURATE_DEPLOY_NAME_SUFFIX")
    try:
        gh = GitHub(token, opener) if token else None
        if args.cmd == "update-deployment":
            return _print(update_deployment(args.project, args.workflow, override or None, gh, token, args.confirmed),
                          extra)
        if args.cmd == "teardown" and args.workflow:
            return _print(teardown_version(args.project, args.workflow, override or None, gh, token, s3_factory,
                                           args.confirmed, args.plan, args.outputs, args.to, args.other_projects), extra)
        if args.cmd == "teardown":
            return _print(teardown(args.run, gh, token, s3_factory, args.confirmed, args.plan, args.outputs,
                                   args.to), extra)
        name = deploy_name(args.run, override or None)
        if args.cmd == "preflight":
            out = preflight(args.run, name, gh, source, s3_factory, args.overwrite_own_deploy)
            out.pop("_copy_wf", None)
        else:
            out = deploy_step(args.run, args.cmd, name, gh, token, source, s3_factory,
                              args.confirmed, args.overwrite_own_deploy)
        return _print(out, extra)
    except InputError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
