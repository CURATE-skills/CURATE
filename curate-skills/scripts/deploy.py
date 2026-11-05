# /// script
# requires-python = "==3.13.*"
# dependencies = ["FaaSr_py==0.1.13", "PyGithub>=2.8.1", "requests"]  # FaaSr_py pins boto3
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# The deploy steps follow CURATE-prototype src/faasr_agents/agents/wda.py 56-157 and the
# vendored deploy/ modules (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). See scripts/VENDORED.md.
"""curate-deploy: put an approved bundle on FaaSr (GitHub Actions) the way CURATE does.

`preflight` is read-only: it reads the run, its bundle, (with a token) the GitHub repo and (with
S3 keys) the S3 bucket, and reports what deploying would write. Nothing is written anywhere.
The four steps — publish, upload, register, invoke (CURATE's order) — each write only with
--confirmed, which the stage file passes after the user said yes to that step; each re-runs the
preflight first and records every path it writes in <run>/deploy/deploy-log.jsonl. Every path deploying would write must carry the suffix and must not exist yet
(--overwrite-own-deploy also allows paths this run's own deploy log recorded, to retry a partial
deploy). The deploy copy may differ from the approved bundle only in ALLOWED_DIFF fields and
placeholder fields; the summary shows the difference field by field.

Every curate-skills deployment gets its own name, WorkflowName + suffix, so CURATE's deployment
of the same workflow and other runs' deployments are never touched. The suffix (resolve_suffix) is
"-<MMDD-HHMM>" of the run that approved Gate 4 (seconds added if that name is taken), inherited
from the source run for a deploy-only run, $CURATE_DEPLOY_NAME_SUFFIX / --name-suffix if set,
and pinned once a deploy step ran (older runs keep their "-cs"):
  functions/<Name><sfx>/<function>.py        (FunctionGitRepo points only there)
  workflows/<Name><sfx>.json
  .github/workflows/<Name><sfx>-<action>.yml
  S3: <folder><sfx>/... (each action's "folder" argument) and FaaSrLog/<Name><sfx>/...
`list` shows the deployments recorded in this project's runs; `teardown` deletes exactly the
repo files and S3 objects one run's deploy log recorded, after the user's yes.
The deploy copy also fills emit's <FAASR_...> placeholders from the environment (non-secret
values: user, repo, S3 endpoint/bucket/region). The approved bundle is not changed; the renamed,
filled copy exists only in the deploy step.

Credentials come only from environment variables (CURATE's names) or `gh auth token`, read
inside this script; they are never printed, written or passed on a command line.
Prints JSON with "ok"; exit 1 = needs attention, 2 = bad input.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import fcntl
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

NATIVE_CONTAINERS = SCRIPT_DIR / "vendor" / "curate_deploy" / "native_containers.txt"
SUFFIX_RX = re.compile(r"^[-_A-Za-z0-9]+$")
RUN_TS_RX = re.compile(r"^\d{4}(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})")  # run ids: YYYYMMDD-HHMMSS-<name>
MAX_PATH_PART = 255  # bytes per file name in a git tree / most file systems
COMMIT_TAG = "(curate-skills)"  # in every commit message curate-deploy makes
DEPLOY_LOG = Path("deploy") / "deploy-log.jsonl"  # in the run; one JSON line per path a deploy step wrote
# The only fields in which the deploy copy may differ from the approved bundle (besides fields that
# held an emit placeholder). Anything else is refused: the deploy must run what Gate 4 approved.
ALLOWED_DIFF = [("WorkflowName", "deploy name"),
                (r"ActionList\.[^.]+\.Arguments\.folder", "deploy name (S3 folder)"),
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
    """sha256 over the bundle's files (relative path + bytes), ignoring OS_METADATA."""
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file() and not OS_METADATA.search(p.relative_to(folder).as_posix()):
            h.update(str(p.relative_to(folder)).encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def load_run(run: str) -> tuple[dict, Path]:
    path = Path(run) / "state.json"
    if not path.is_file():
        raise InputError(f"{path} not found — is --run a run directory?")
    s = json.loads(path.read_text())
    if s.get("schema_version", 1) < 2:
        raise InputError("this run was made by curate-skills v1, which does not deploy")
    if not s.get("bundle_dir"):
        raise InputError("this run has no bundle yet — run curate-emit and approve Gate 4 first")
    bundle = Path(s["bundle_dir"])
    if not (bundle / "workflow.json").is_file():
        raise InputError(f"{bundle}/workflow.json not found")
    return s, bundle


def gate4_problem(s: dict, bundle_hash: str | None = None) -> str | None:
    """None if the bundle is Gate 4-approved: approved in this run, or carried over from another
    run's approval by `start --at deploy|finalize --from-run` — then only if the hash recorded with
    the carried-over approval matches the bundle as it is now."""
    hist = s.get("history") or []
    approvals = [i for i, h in enumerate(hist)
                 if h.get("gate") == 4 and h.get("decision") in ("approve", "copied")]
    if not approvals:
        return "Gate 4 was never approved in this run; only an approved bundle can be deployed"
    last = hist[approvals[-1]]
    if last["decision"] == "copied":
        if not (last.get("from_run") and last.get("snapshot") and last.get("bundle_hash")):
            return ("this run's Gate 4 approval was carried over from another run without a bundle hash; "
                    "start again with --start-at deploy --from-run <run_id>")
        if bundle_hash is not None and last["bundle_hash"] != bundle_hash:
            return (f"the bundle no longer matches the Gate 4 approval carried over from run "
                    f"{last['from_run']} ({last['snapshot']}); it cannot be deployed")
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

def deploy_copy(wf: dict, suffix: str, user: str, repo: str, branch: str
                ) -> tuple[dict, list[dict], list[dict]]:
    """The workflow as curate-deploy would upload it, every repo path it would write, and every
    S3 prefix the invoked run would write (outputs under each renamed folder, and its logs)."""
    name = wf["WorkflowName"]
    new = name + suffix
    out = copy.deepcopy(wf)
    out["WorkflowName"] = new
    default_store = wf.get("DefaultDataStore") or "S3"
    s3_targets, folders = [], []
    for action, a in sorted((out.get("ActionList") or {}).items()):
        args = a.get("Arguments") or {}
        if not args.get("folder"):
            s3_targets.append({"store": default_store, "prefix": None, "kind": "outputs", "action": action})
            continue
        if not args["folder"].endswith(suffix):
            args["folder"] += suffix
        if args["folder"] not in folders:
            folders.append(args["folder"])
            s3_targets.append({"store": default_store, "prefix": args["folder"] + "/", "kind": "outputs"})
    s3_targets.append({"store": wf.get("LoggingDataStore") or default_store,
                       "prefix": f"{wf.get('FaaSrLog') or 'FaaSrLog'}/{new}/", "kind": "logs"})
    for t in s3_targets:
        t["bucket"] = ((wf.get("DataStores") or {}).get(t["store"]) or {}).get("Bucket")
    folder = f"functions/{new}"
    fns = sorted({a["FunctionName"] for a in (wf.get("ActionList") or {}).values()})
    out["FunctionGitRepo"] = {fn: f"{user}/{repo}/{branch}/{folder}/{fn}.py" for fn in fns}
    targets = [{"step": "publish", "path": f"{folder}/{fn}.py", "kind": "function"} for fn in fns]
    targets.append({"step": "upload", "path": f"workflows/{new}.json", "kind": "workflow"})
    servers = wf.get("ComputeServers") or {}
    for action, a in sorted((wf.get("ActionList") or {}).items()):
        if (servers.get(a.get("FaaSServer")) or {}).get("FaaSType") == "GitHubActions":
            targets.append({"step": "register", "path": f".github/workflows/{new}-{action}.yml",
                            "kind": "actions", "action": action})
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
            problems.append(f"the deploy copy differs from the approved bundle at {path}, which is not an "
                            "allowed deploy change (refusing: the deploy must run what Gate 4 approved)")
        rows.append({"field": path, "approved": old, "deploy": new, "reason": reason or "NOT ALLOWED"})
    return rows, problems


def derived_changes(wf: dict, copy_wf: dict) -> list[dict]:
    """Locations that change only because the name changes (not fields of workflow.json)."""
    old, new = wf["WorkflowName"], copy_wf["WorkflowName"]
    log = wf.get("FaaSrLog") or "FaaSrLog"
    return [{"what": "S3 log prefix (FaaSr)", "approved": f"{log}/{old}/", "deploy": f"{log}/{new}/"},
            {"what": "workflow JSON in the repo", "approved": f"workflows/{old}.json", "deploy": f"workflows/{new}.json"},
            {"what": "function files in the repo", "approved": "functions/<fn>.py", "deploy": f"functions/{new}/<fn>.py"},
            {"what": "Actions files in the repo", "approved": f".github/workflows/{old}-<action>.yml",
             "deploy": f".github/workflows/{new}-<action>.yml"}]


def read_deploy_log(run: str) -> tuple[list[dict], list[str]]:
    """This run's own deploy log: lines curate-deploy appended for each path a step wrote.
    Repo lines: {run_id, deploy_name, step, repo, branch, path}; S3 lines (written at invoke for
    the prefixes the run writes): {run_id, deploy_name, step, bucket, prefix}."""
    path = Path(run) / DEPLOY_LOG
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


def name_problems(copy_wf: dict, targets: list[dict], s3_targets: list[dict], suffix: str,
                  user: str, repo: str, branch: str) -> list[str]:
    """Every written path (repo and S3) and every FunctionGitRepo entry must carry the suffix."""
    new = copy_wf["WorkflowName"]
    out = []
    for t in s3_targets:
        if t["prefix"] is None:
            out.append(f"action {t['action']} has no \"folder\" argument, so its S3 outputs cannot be "
                       f"given the {suffix!r} suffix")
        elif t["kind"] == "outputs" and not t["prefix"].rstrip("/").endswith(suffix):
            out.append(f"S3 folder {t['prefix']} does not end with {suffix!r}")
        elif t["kind"] == "logs" and f"/{new}/" not in f"/{t['prefix']}":
            out.append(f"S3 log prefix {t['prefix']} is not under the deploy name {new!r}")
    if not new.endswith(suffix):
        out.append(f"deploy name {new!r} does not end with {suffix!r}")
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
    try:
        return json.loads((run_dir / "state.json").read_text())
    except (OSError, ValueError):
        return None


def project_runs(run: str | Path) -> list[tuple[Path, dict]]:
    """Every run in the same .curate/runs folder (this one included), with its state."""
    out = []
    for d in sorted(Path(run).resolve().parent.iterdir()):
        st = _read_state(d) if d.is_dir() else None
        if st:
            out.append((d, st))
    return out


def lineage(run: str | Path, s: dict) -> list[tuple[str, dict | None]]:
    """This run, then the run its Gate 4 approval was copied from (--start-at deploy --from-run X),
    and so on: the last entry is the run that approved Gate 4. A source not in this project ends
    the chain (its state is None)."""
    parent = Path(run).resolve().parent
    chain, seen, cur = [(s.get("run_id"), s)], {s.get("run_id")}, s
    while cur:
        first = (cur.get("history") or [{}])[0]
        src = first.get("from_run") if first.get("gate") == 4 and first.get("decision") == "copied" else None
        if not src or src in seen:
            break
        seen.add(src)
        cur = _read_state(parent / src)
        chain.append((src, cur))
    return chain


def owned_deployments(run: str | Path, exclude_ids=()) -> dict[str, str]:
    """{deploy name: run id} for deployments made (not reused) by runs of this project, except the
    given runs and torn-down ones."""
    out = {}
    for d, st in project_runs(run):
        dep = st.get("deploy") or {}
        if st.get("run_id") in exclude_ids or not dep.get("deploy_name") or dep.get("reused_from") \
                or dep.get("teardown"):
            continue
        if any((dep.get("steps") or {}).get(x, {}).get("ok") for x in STEPS):
            out[dep["deploy_name"]] = st.get("run_id")
    return out


def _name_taken(run: str, ids: set, name: str, gh: GitHub | None) -> str | None:
    other = owned_deployments(run, ids).get(name)
    if other:
        return f"run {other} deployed it"
    user, repo = os.environ.get("FAASR_GH_USERNAME", "").strip(), os.environ.get("FAASR_ACTION_REPO", "").strip()
    branch = os.environ.get("FAASR_ACTION_BRANCH", "").strip() or "main"
    if gh is not None and user and repo:
        st, _, _ = gh.get(f"/repos/{user}/{repo}/contents/workflows/{name}.json", ref=branch)
        if st == 200:
            return f"workflows/{name}.json exists in {user}/{repo}"
    return None


def resolve_suffix(run: str, override: str | None = None, gh: GitHub | None = None) -> dict:
    """The deploy name suffix for this run, and where it came from. In order: pinned (a deploy step
    already ran with it); --name-suffix / $CURATE_DEPLOY_NAME_SUFFIX; the deployment of the run this
    one was copied from (so invoke only finds it, old "-cs" names included); else "-<MMDD-HHMM>"
    of the run that approved Gate 4, "-<MMDD-HHMMSS>" if that name is taken."""
    s, bundle = load_run(run)
    rec = s.get("deploy") or {}
    if rec.get("suffix"):
        if override and override != rec["suffix"]:
            raise InputError(f"this deploy started with suffix {rec['suffix']!r}; keep it (got {override!r})")
        return {"suffix": rec["suffix"], "source": "pinned: this run's deploy started with it"}
    if override:
        return {"suffix": override, "source": "CURATE_DEPLOY_NAME_SUFFIX / --name-suffix"}
    chain = lineage(run, s)
    for rid, st in chain[1:]:
        if st and (st.get("deploy") or {}).get("suffix"):
            return {"suffix": st["deploy"]["suffix"], "source": f"run {rid}'s deployment (this run was started from it)"}
    approver = chain[-1][0]
    m = RUN_TS_RX.match(approver or "")
    if not m:
        raise InputError(f"cannot name the deployment: run id {approver!r} has no date and time; set "
                         "CURATE_DEPLOY_NAME_SUFFIX (e.g. -alice-1)")
    minute, second = f"-{m[1]}{m[2]}-{m[3]}{m[4]}", f"-{m[1]}{m[2]}-{m[3]}{m[4]}{m[5]}"
    name = json.loads((bundle / "workflow.json").read_text())["WorkflowName"]
    ids = {rid for rid, _ in chain}
    why = _name_taken(run, ids, name + minute, gh)
    if not why:
        return {"suffix": minute, "source": f"Gate 4 approval in run {approver}"}
    return {"suffix": second, "source": f"Gate 4 approval in run {approver}; {name}{minute} is taken ({why})"}


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


def preflight(run: str, suffix: str, gh: GitHub | None = None, token_source: str = "none",
              s3_factory=boto3_s3, overwrite_own: bool = False, done_steps=(),
              require_hash: bool = False, reuse_from: dict | None = None, name_source: str | None = None) -> dict:
    """Read-only. done_steps: deploy steps this run already finished; their paths must exist,
    carry curate-skills' mark and be in this run's deploy log (they are not written again).
    reuse_from ({run_id, run_dir}): the deployment was made by that run and is reused (invoke only);
    its deploy log counts too, and the S3 prefixes it recorded may be rewritten by the new invocation.
    require_hash: refuse without a bundle hash recorded at Gate 4 (every deploy step does)."""
    if not suffix or not SUFFIX_RX.match(suffix):
        raise InputError(f"deploy name suffix {suffix!r} must be non-empty letters, digits, '-' or '_'")
    s, bundle = load_run(run)
    wf = json.loads((bundle / "workflow.json").read_text())
    problems, warnings = [], []
    bhash = tree_hash(bundle)
    if (p := gate4_problem(s, bhash)):
        problems.append(p)
    if s.get("bundle_hash") and s["bundle_hash"] != bhash:
        problems.append("the bundle folder changed after Gate 4 approval; re-run curate-emit")
    elif require_hash and not s.get("bundle_hash"):
        problems.append("no bundle hash was recorded when Gate 4 was approved, so a changed bundle could "
                        "not be detected; approve Gate 4 again")
    elif not s.get("bundle_hash"):
        warnings.append("this run has no recorded bundle hash (approved before deploy existed); "
                        "the bundle is checked as it is now")
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
                        + f"); the deploy copy's renamed folder (+{suffix!r}) would not have them")
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

    copy_wf, targets, s3_targets = deploy_copy(filled_wf, suffix, user or "<user>", repo or "<repo>", branch)
    problems += name_problems(copy_wf, targets, s3_targets, suffix, user or "<user>", repo or "<repo>", branch)
    if copy_wf["WorkflowName"] == wf["WorkflowName"]:
        problems.append(f"the deploy name {wf['WorkflowName']!r} is the workflow's own name, which CURATE's "
                        "deployment uses; refusing")
    taken_by = owned_deployments(run, {rid for rid, _ in lineage(run, s)}
                                 | ({reuse_from["run_id"]} if reuse_from else set())).get(copy_wf["WorkflowName"])
    if taken_by:
        problems.append(f"the deploy name {copy_wf['WorkflowName']!r} is run {taken_by}'s deployment in this "
                        "project; refusing (choose another with CURATE_DEPLOY_NAME_SUFFIX)")
    req_secrets = required_secret_names(wf)
    diff_rows, diff_problems = deploy_diff(wf, copy_wf, filled)
    problems += diff_problems
    log_entries, bad_lines = read_deploy_log(run)
    warnings += bad_lines
    run_ids = {s.get("run_id")}
    if reuse_from:
        src_entries, src_bad = read_deploy_log(reuse_from["run_dir"])
        log_entries += src_entries
        warnings += [f"source run {reuse_from['run_id']}: {x}" for x in src_bad]
        run_ids.add(reuse_from["run_id"])
    own_files, own_s3 = own_paths(log_entries, run_ids, copy_wf["WorkflowName"], f"{user}/{repo}", branch)
    out = {"run": str(Path(run).resolve()), "bundle": str(bundle), "bundle_hash": bhash,
           "approved_name": wf.get("WorkflowName"), "deploy_name": copy_wf["WorkflowName"],
           "suffix": suffix, "name_source": name_source,
           "repo": f"{user}/{repo}" if user and repo else None, "branch": branch,
           "token_source": token_source, "targets": targets, "function_git_repo": copy_wf["FunctionGitRepo"],
           "filled_placeholders": {k: ", ".join(f"${x}" for x in v) + f" = {_dig(copy_wf, k)}" for k, v in filled.items()
                                   if not k.startswith("FunctionGitRepo")},
           "s3_targets": s3_targets, "secrets": {"required": req_secrets}, "curate_files": [],
           "github_checked": False, "deploy_diff": diff_rows, "reuse_from": (reuse_from or {}).get("run_id"),
           "derived_changes": derived_changes(wf, copy_wf), "overwrite_own": overwrite_own,
           "deploy_log": {"path": str(DEPLOY_LOG), "repo_paths": sorted(own_files),
                          "s3_prefixes": sorted(f"s3://{b}/{x}" for b, x in own_s3)}}

    if unset:
        out["s3"] = {"checked": False, "buckets": {}, "curate_prefixes": []}
    else:
        old_folders = sorted({(a.get("Arguments") or {}).get("folder") for a in (wf.get("ActionList") or {}).values()}
                             - {None, ""})
        curate_prefixes = [f + "/" for f in old_folders] + [f"{wf.get('FaaSrLog') or 'FaaSrLog'}/{wf['WorkflowName']}/"]
        out["s3"], s3_problems = s3_check(copy_wf, s3_targets, curate_prefixes, s3_factory)
        problems += s3_problems
        for t in s3_targets:
            if not t.get("exists"):
                continue
            if (overwrite_own or reuse_from) and (t["bucket"], t["prefix"]) in own_s3:
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
            for t in targets:
                t.update(_owner_of(gh, user, repo, branch, t, new))
                if t["step"] in done_steps:
                    t["written"] = True
                    if reuse_from:
                        t["reused_from"] = reuse_from["run_id"]
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
                whose = (f"from an earlier curate-skills deployment named {new!r}" if t.get("ours")
                         else f"does not belong to a curate-skills deployment named {new!r}")
                if overwrite_own and t["path"] in own_files and not t.get("ours"):
                    whose = "listed in this run's deploy log, but no longer carries its mark — changed since"
                problems.append(f"{t['path']} already exists ({whose}; {t.get('why')}); refusing — "
                                + _new_or_own(overwrite_own))
            name = wf["WorkflowName"]
            for p in [f"workflows/{name}.json"] + [f"functions/{fn}.py" for fn in sorted(copy_wf["FunctionGitRepo"])] \
                     + [f".github/workflows/{name}-{a}.yml" for a in sorted(wf.get("ActionList") or {})]:
                st, _, _ = gh.get(f"/repos/{user}/{repo}/contents/{p}", ref=branch)
                if st == 200:
                    out["curate_files"].append(p)
    out["existing_paths"] = [t["path"] for t in targets if t.get("exists") and not t.get("written")] + \
        [f"s3://{t['bucket']}/{t['prefix']}" for t in s3_targets if t.get("exists")]
    out["refused_paths"] = [t["path"] for t in targets
                            if t.get("exists") and not t.get("overwrite") and not t.get("written")] + \
        [f"s3://{t['bucket']}/{t['prefix']}" for t in s3_targets if t.get("exists") and not t.get("overwrite")]
    out["overwrite_paths"] = [t["path"] for t in targets if t.get("overwrite")] + \
        [f"s3://{t['bucket']}/{t['prefix']}" for t in s3_targets if t.get("overwrite")]
    out.update(ok=not problems, problems=problems, warnings=warnings)
    out["summary"] = preflight_summary(out)
    out["_copy_wf"] = copy_wf  # for the deploy steps; never printed
    return out


def _new_or_own(overwrite_own: bool) -> str:
    if overwrite_own:
        return ("only paths this run's own deploy log records (and that still carry its mark) may be "
                "overwritten")
    return ("every path the deploy writes must be new (to retry this run's own partial deploy, "
            "use --overwrite-own-deploy)")


def _cell(v) -> str:
    text = "(absent)" if v is None else v if isinstance(v, str) else json.dumps(v)
    return "`" + text.replace("|", "\\|").replace("`", "'") + "`"


def _dig(obj, path: str):
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        obj = obj[int(part[1:-1])] if part.startswith("[") else obj[part]
    return obj


def preflight_summary(p: dict) -> str:
    lines = [f"**Deploy preflight — {'ready' if p['ok'] else 'not ready'}** (read-only; nothing was written)",
             "", f"- Approved bundle: `{p['bundle']}` (workflow `{p['approved_name']}`)",
             f"- Deploys as: `{p['deploy_name']}` to `{p['repo']}` branch `{p['branch']}` "
             f"(token: {p['token_source']}; GitHub {'checked' if p['github_checked'] else 'not checked'})"
             + (f"\n- Name from: {p['name_source']}" if p.get("name_source") else "")]
    by_step: dict[str, list[str]] = {}
    for t in p["targets"]:
        if t.get("reused_from"):
            state = f"deployed by run {t['reused_from']}, reused"
        elif t.get("written"):
            state = "written by this deploy"
        elif t.get("exists") is None:
            state = "not checked"
        elif not t["exists"]:
            state = "new"
        elif t.get("overwrite"):
            state = "exists, in this run's deploy log — will be overwritten (--overwrite-own-deploy)"
        else:
            state = "EXISTS (earlier curate-skills deploy) — refused" if t.get("ours") else \
                "EXISTS, NOT OURS — refused"
        by_step.setdefault(t["step"], []).append(f"`{t['path']}` ({state})")
    for step in ("publish", "upload", "register"):
        if step in by_step:
            lines.append(f"- {step}: " + "; ".join(by_step[step]))
    lines.append("- invoke: workflow_dispatch of the entry action (FaaSr Scheduler), as CURATE")
    s3 = p.get("s3") or {}
    for t in p.get("s3_targets") or []:
        if t["prefix"] is None:
            continue
        state = ("not checked" if t.get("exists") is None else "new" if not t["exists"] else
                 ("exists (this deployment's earlier outputs) — the new invocation rewrites them"
                  if p.get("reuse_from") else
                  "exists, in this run's deploy log — will be overwritten (--overwrite-own-deploy)")
                 if t.get("overwrite") else "EXISTS — refused")
        lines.append(f"- S3 ({t['kind']}, written by the run): `s3://{t['bucket']}/{t['prefix']}` ({state})")
    if s3.get("key_vars"):
        lines.append("- S3 keys read from: " + ", ".join(f"`{v}`" for v in s3["key_vars"].values()))
    if p["curate_files"]:
        lines.append(f"- CURATE's own files found and left untouched: {', '.join(p['curate_files'])}")
    if s3.get("curate_prefixes"):
        lines.append(f"- CURATE's own S3 data found and left untouched: {', '.join(s3['curate_prefixes'])}")
    if p.get("refused_paths"):
        lines.append("- **Paths that already exist (refused):** " + ", ".join(f"`{x}`" for x in p["refused_paths"]))
    if p.get("overwrite_paths"):
        lines.append("- Paths from this run's own deploy log that will be overwritten: "
                     + ", ".join(f"`{x}`" for x in p["overwrite_paths"]))
    sec = p["secrets"]
    lines.append(f"- GitHub secrets needed: {', '.join(sec['required'])}"
                 + (f" — missing: {', '.join(sec['missing'])}" if sec.get("missing") else
                    " — all present" if "present" in sec else " — not checked"))
    lines += [f"- **Problem:** {x}" for x in p["problems"]]
    lines += [f"- Note: {x}" for x in p["warnings"]]
    if p.get("deploy_diff"):
        lines += ["", "**Deploy copy vs approved bundle** (the approved bundle is not changed)", "",
                  "| Field | Approved bundle | Deploy copy | Why |", "|---|---|---|---|"]
        lines += [f"| `{r['field']}` | {_cell(r['approved'])} | {_cell(r['deploy'])} | {r['reason']} |"
                  for r in p["deploy_diff"]]
        lines += [f"| {d['what']} (derived from the name) | `{d['approved']}` | `{d['deploy']}` | deploy name |"
                  for d in p.get("derived_changes") or []]
    return "\n".join(lines)


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
    "publish": "write the function files to the action repo",
    "upload": "write the deploy copy of workflow.json to the action repo",
    "register": "write the GitHub Actions files (CURATE's register_workflow)",
    "invoke": "start the workflow: a workflow_dispatch of the entry action (CURATE's invoke_workflow)",
}


class StepError(Exception):
    pass


@contextlib.contextmanager
def _state_lock(run: Path):
    """The same lock spec.py holds (<run>/state.lock) around every state.json change."""
    with open(run / "state.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _update_state(run: Path, change) -> dict:
    with _state_lock(run):
        path = run / "state.json"
        s = json.loads(path.read_text())
        change(s)
        text = json.dumps(s, indent=2) + "\n"
        if leakcheck.find_leaks(text):
            raise StepError("refusing to write state.json: it would contain a secret value")
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(text)
        tmp.replace(path)
        return s


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_written(run: Path, entry: dict) -> None:
    """Append one line to <run>/deploy/deploy-log.jsonl (what --overwrite-own-deploy trusts)."""
    (run / DEPLOY_LOG).parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": _now_iso(), **entry})
    if leakcheck.find_leaks(line):
        raise StepError("refusing to write the deploy log: the entry would contain a secret value")
    with open(run / DEPLOY_LOG, "a") as fh:
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
    return f"/curate-skills:curate-deploy --run {s['run_id']}"


def deploy_step(run: str, step: str, suffix: str, gh: GitHub | None, token: str | None,
                token_source: str, s3_factory=boto3_s3, confirmed: bool = False,
                overwrite_own: bool = False) -> dict:
    run_p = Path(run)
    s, bundle = load_run(run)
    rec = s.get("deploy") or {}
    done = [x for x in STEPS if (rec.get("steps") or {}).get(x, {}).get("ok")]
    base = {"run": str(run_p.resolve()), "step": step, "done": done}
    if s.get("stage") != "deploy":
        return {**base, "ok": False, "error": f"the run is at stage {s.get('stage')!r}, not deploy; "
                f"curate-deploy runs after Gate 4 is approved with Approve & deploy"}
    if s.get("deploy_choice") != "yes":
        return {**base, "ok": False, "error": "Gate 4 was approved without deploying"}
    if rec.get("suffix") and rec["suffix"] != suffix:
        return {**base, "ok": False, "error": f"this deploy started with suffix {rec['suffix']!r}; "
                f"keep it (got {suffix!r})"}
    if step in done:
        return {**base, "ok": True, "skipped": True, "detail": f"{step} already done in this run",
                "next_step": next((x for x in STEPS if x not in done), None)}
    missing = [x for x in STEPS[:STEPS.index(step)] if x not in done]
    if missing:
        return {**base, "ok": False, "error": f"run {missing[0]} first (steps go publish → upload → "
                f"register → invoke)", "next_step": missing[0]}

    try:
        reused = reuse_source(run, s) if rec.get("reused_from") else None
    except StepError as e:
        return {**base, "ok": False, "error": str(e)}
    pf = preflight(run, suffix, gh, token_source, s3_factory, overwrite_own, done_steps=done, require_hash=True,
                   reuse_from=reused)
    copy_wf = pf.pop("_copy_wf")
    user, repo, branch = (os.environ.get("FAASR_GH_USERNAME", "").strip(),
                          os.environ.get("FAASR_ACTION_REPO", "").strip(),
                          os.environ.get("FAASR_ACTION_BRANCH", "").strip() or "main")
    new = copy_wf["WorkflowName"]
    paths = [t["path"] for t in pf["targets"] if t["step"] == step]
    if step == "invoke":
        paths = [f"s3://{t['bucket']}/{t['prefix']}" for t in pf["s3_targets"] if t["prefix"]]
    plan = {"what": STEP_WHAT[step], "repo": pf["repo"], "branch": branch, "deploy_name": new,
            "paths": paths}
    base.update(plan=plan, preflight_summary=pf["summary"])
    if not pf["ok"]:
        return {**base, "ok": False, "problems": pf["problems"],
                "error": "the preflight found problems; nothing was written"}
    if not confirmed:
        return {**base, "ok": False, "needs_confirmation": True,
                "error": f"{step} writes to {pf['repo']} — ask the user first; only after they say yes, "
                         f"run it again with --confirmed"}

    extra = {"gh_token": token} if token else None
    log_base = {"run_id": s["run_id"], "deploy_name": new, "step": step}
    _update_state(run_p, lambda st: st.update(deploy={
        **(st.get("deploy") or {}), "deploy_name": new, "suffix": suffix, "repo": pf["repo"], "branch": branch,
        "bundle_hash": pf["bundle_hash"], "steps": {**((st.get("deploy") or {}).get("steps") or {}),
                                                    step: {"ok": False, "started": _now_iso()}}}))
    detail: dict = {}
    try:
        if step == "publish":
            # Every file is read and leak-checked before the first one is pushed.
            files = []
            for fn in sorted(copy_wf["FunctionGitRepo"]):
                src = bundle / "functions" / f"{fn}.py"
                if not src.is_file():
                    raise StepError(f"the bundle has no functions/{fn}.py")
                files.append((fn, f"functions/{new}/{fn}.py", _checked(src.read_text(), f"functions/{fn}.py", extra)))
            for fn, path, code in files:
                put_file(gh, user, repo, path, code, f"Add/update {fn} (curate-skills)", branch)
                _log_written(run_p, {**log_base, "repo": f"{user}/{repo}", "branch": branch, "path": path,
                                     "sha": blob_sha(code)})
            detail = {"written": paths}
        elif step == "upload":
            path = f"workflows/{new}.json"
            text = _checked(json.dumps(copy_wf, indent=2), path, extra)
            (run_p / "deploy").mkdir(exist_ok=True)
            (run_p / "deploy" / "workflow.json").write_text(text + "\n")
            put_file(gh, user, repo, path, text, f"Add/update workflow payload {new} (curate-skills)", branch)
            _log_written(run_p, {**log_base, "repo": f"{user}/{repo}", "branch": branch, "path": path,
                                 "sha": blob_sha(text)})
            detail = {"written": [path], "local_copy": str(run_p / "deploy" / "workflow.json")}
        elif step == "register":
            log = RUNNERS["register"](copy_wf, token, extra)
            try:  # what CURATE's generator wrote, for teardown's "changed since deploy" check
                shas = {k: blob_sha(v) for k, v in expected_actions_files(copy_wf).items()}
            except Exception:  # noqa: BLE001 — teardown then compares against regenerated files
                shas = {}
            for path in paths:
                _log_written(run_p, {**log_base, "repo": f"{user}/{repo}", "branch": branch, "path": path,
                                     **({"sha": shas[path]} if path in shas else {})})
            detail = {"written": paths, "log": log}
        elif step == "invoke":
            dispatched = _now_iso()
            got = RUNNERS["invoke"](f"workflows/{new}.json", token, extra)
            for t in pf["s3_targets"]:
                if t["prefix"]:
                    _log_written(run_p, {**log_base, "bucket": t["bucket"], "prefix": t["prefix"]})
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
        if step == "invoke":
            st["deploy"].update(invocation_id=detail["invocation_id"],
                                invocation_timestamp=detail["invocation_timestamp"],
                                dispatched_at=detail["dispatched_at"], workflow_name=new)
            st.setdefault("history", []).append({
                "gate": "deploy", "decision": "deployed", "text": f"{new} invocation {detail['invocation_id']}",
                "structural": None, "ts": _now_iso()})
            st.update(stage="monitor", begun=None)  # curate-monitor checks the run (read-only)
    s = _update_state(run_p, finish)
    done = [x for x in STEPS if s["deploy"]["steps"].get(x, {}).get("ok")]
    nxt = next((x for x in STEPS if x not in done), None)
    out = {**base, "ok": True, "done": done, "progress": progress_line(done), "next_step": nxt, **detail,
           "next_stage": s["stage"]}
    if step == "invoke":
        out.update(next_skill="curate-skills:curate-monitor",
                   message=f"Deployed: {new}, invocation {detail['invocation_id']}. Next: curate-monitor checks "
                           "the Actions runs, FaaSr logs and outputs (read-only).")
    return out


# ── invoke only: reuse an identical existing deployment ──────────────────────

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
                        "input, so the deployed Actions files cannot be verified byte for byte; reuse is refused")
    return first


def reuse_source(run: str, s: dict) -> dict:
    """The run whose deployment this run reuses: the run its Gate 4 approval was carried over from,
    in the same project, with all four deploy steps done for the same deployment and bundle."""
    first = (s.get("history") or [{}])[0]
    src_id = (s.get("deploy") or {}).get("reused_from") or first.get("from_run")
    if not (first.get("decision") == "copied" and first.get("gate") == 4 and src_id):
        raise StepError("invoke only needs a run started with --start-at deploy --from-run <the run that deployed>")
    src_dir = Path(run).resolve().parent / src_id
    try:
        src = json.loads((src_dir / "state.json").read_text())
    except (OSError, ValueError):
        raise StepError(f"the source run {src_id} is not in this project ({src_dir})") from None
    dep = src.get("deploy") or {}
    missing = [x for x in STEPS if not (dep.get("steps") or {}).get(x, {}).get("ok")]
    if missing:
        raise StepError(f"run {src_id} did not finish deploying (missing: {', '.join(missing)}); nothing to reuse")
    if dep.get("bundle_hash") != s.get("bundle_hash"):
        raise StepError(f"run {src_id} deployed a different bundle (hash) than the one approved here")
    if dep.get("teardown"):
        raise StepError(f"run {src_id}'s deployment was torn down ({dep['teardown'].get('ts')}); nothing to reuse")
    return {"run_id": src_id, "run_dir": str(src_dir), "deploy_name": dep.get("deploy_name"),
            "repo": dep.get("repo"), "branch": dep.get("branch")}


def verify_identical(gh: GitHub, user: str, repo: str, branch: str, bundle: Path, copy_wf: dict,
                     targets: list[dict]) -> tuple[list[str], list[str]]:
    """GET each deployed repo file and compare it, byte for byte, with what publish, upload and
    register would write for this run. Returns (identical paths, problems)."""
    new = copy_wf["WorkflowName"]
    expected = {f"functions/{new}/{fn}.py": (bundle / "functions" / f"{fn}.py").read_text()
                for fn in copy_wf["FunctionGitRepo"] if (bundle / "functions" / f"{fn}.py").is_file()}
    expected[f"workflows/{new}.json"] = json.dumps(copy_wf, indent=2)
    expected.update(expected_actions_files(copy_wf))
    same, problems = [], []
    for t in targets:
        if t["step"] == "invoke":
            continue
        want = expected.get(t["path"])
        st, _, body = gh.get(f"/repos/{user}/{repo}/contents/{t['path']}", ref=branch)
        if want is None:
            problems.append(f"{t['path']}: nothing to compare it with")
        elif st != 200 or not isinstance(body, dict) or body.get("encoding") not in (None, "base64"):
            problems.append(f"{t['path']}: could not read it from the repo (HTTP {st})")
        elif base64.b64decode(body.get("content") or "") != want.encode():
            problems.append(f"{t['path']} differs from what this run would deploy")
        else:
            same.append(t["path"])
    return same, problems


def reuse(run: str, suffix: str, gh: GitHub | None, token: str | None, token_source: str,
          s3_factory=boto3_s3, confirmed: bool = False) -> dict:
    """Invoke only: verify that the deployment made by the source run is byte-identical to what this
    run would deploy, record publish/upload/register as reused (nothing written to GitHub), then run
    the normal invoke step. Needs --confirmed, like every step."""
    run_p = Path(run)
    s, bundle = load_run(run)
    rec = s.get("deploy") or {}
    base = {"run": str(run_p.resolve()), "step": "reuse"}
    if s.get("stage") != "deploy" or s.get("deploy_choice") != "yes":
        return {**base, "ok": False, "error": "invoke only runs in the deploy stage, after the user confirmed "
                "starting the run (Start this run? → Yes)"}
    if any((rec.get("steps") or {}).get(x, {}).get("ok") for x in STEPS):
        return {**base, "ok": False, "error": "this run already started its own deploy steps; invoke only is "
                "for a run that has not written anything"}
    try:
        src = reuse_source(run, s)
    except StepError as e:
        return {**base, "ok": False, "error": str(e)}
    done = ("publish", "upload", "register")
    pf = preflight(run, suffix, gh, token_source, s3_factory, done_steps=done, require_hash=True, reuse_from=src)
    copy_wf = pf.pop("_copy_wf")
    new = copy_wf["WorkflowName"]
    user, repo, branch = (os.environ.get("FAASR_GH_USERNAME", "").strip(),
                          os.environ.get("FAASR_ACTION_REPO", "").strip(),
                          os.environ.get("FAASR_ACTION_BRANCH", "").strip() or "main")
    problems = list(pf["problems"])
    if (src["deploy_name"], src["repo"], src["branch"]) != (new, f"{user}/{repo}", branch):
        problems.append(f"run {src['run_id']} deployed {src['deploy_name']} to {src['repo']} ({src['branch']}); "
                        f"this run would deploy {new} to {user}/{repo} ({branch})")
    identical = []
    if gh is not None and not problems:
        try:
            identical, diff = verify_identical(gh, user, repo, branch, bundle, copy_wf, pf["targets"])
        except StepError as e:
            identical, diff = [], [str(e)]
        problems += diff
    plan = {"what": f"start the workflow again on the existing deployment {new}, made by run {src['run_id']} "
                    "(publish, upload and register are skipped: nothing is written to GitHub)",
            "repo": pf["repo"], "branch": branch, "deploy_name": new, "reused_from": src["run_id"],
            "identical": identical,
            "paths": [f"s3://{t['bucket']}/{t['prefix']}" for t in pf["s3_targets"] if t["prefix"]]}
    base.update(plan=plan, preflight_summary=pf["summary"])
    if problems:
        return {**base, "ok": False, "problems": problems,
                "error": "the existing deployment cannot be reused; nothing was written"}
    if not confirmed:
        return {**base, "ok": False, "needs_confirmation": True,
                "error": f"invoke only starts the workflow in {pf['repo']} — ask the user first; only after they "
                         "say yes, run it again with --confirmed"}
    text = json.dumps(copy_wf, indent=2)
    (run_p / "deploy").mkdir(exist_ok=True)
    (run_p / "deploy" / "workflow.json").write_text(_checked(text, "the deploy copy", {"gh_token": token}) + "\n")
    ts = _now_iso()
    _update_state(run_p, lambda st: st.update(deploy={
        "deploy_name": new, "suffix": suffix, "repo": pf["repo"], "branch": branch, "bundle_hash": pf["bundle_hash"],
        "reused_from": src["run_id"],
        "steps": {x: {"ok": True, "ts": ts, "reused_from": src["run_id"], "verified": "byte-identical",
                      "paths": [t["path"] for t in pf["targets"] if t["step"] == x]} for x in done}}))
    out = deploy_step(run, "invoke", suffix, gh, token, token_source, s3_factory, confirmed=True)
    return {**out, "reused_from": src["run_id"], "identical": identical}


def deploy_status(run: str) -> dict:
    s, _ = load_run(run)
    rec = s.get("deploy") or {}
    steps = rec.get("steps") or {}
    done = [x for x in STEPS if steps.get(x, {}).get("ok")]
    failed = {x: steps[x].get("error") for x in STEPS if x in steps and not steps[x].get("ok") and steps[x].get("error")}
    reuse_possible = None
    if not done and s.get("stage") == "deploy":
        try:
            reuse_possible = reuse_source(run, s)["run_id"]
        except StepError:
            pass
    return {"ok": True, "stage": s.get("stage"), "deploy_choice": s.get("deploy_choice"), "done": done,
            "progress": progress_line(done), "reuse_possible": reuse_possible,
            "failed": failed, "next_step": next((x for x in STEPS if x not in done), None),
            "deploy_name": rec.get("deploy_name"), "invocation_id": rec.get("invocation_id"),
            "deploy_log": str(Path(run) / DEPLOY_LOG) if (Path(run) / DEPLOY_LOG).is_file() else None}


# ── list and teardown ────────────────────────────────────────────────────────

def list_deployments(project: str | Path) -> dict:
    """Deployments recorded in this project's runs: name, the run that deployed it, when, the last
    invocation (by it or by runs that reused it), and whether it was torn down. Reads only."""
    runs_dir = Path(project).expanduser().resolve() / ".curate" / "runs"
    rows: dict[tuple, dict] = {}
    states = [st for d in sorted(runs_dir.iterdir()) if d.is_dir() for st in [_read_state(d)] if st] \
        if runs_dir.is_dir() else []
    for st in states:
        dep = st.get("deploy") or {}
        steps = dep.get("steps") or {}
        if not dep.get("deploy_name") or not any(steps.get(x, {}).get("ok") for x in STEPS):
            continue
        key = (dep.get("repo"), dep["deploy_name"])
        row = rows.setdefault(key, {"name": dep["deploy_name"], "repo": dep.get("repo"), "run": None,
                                    "deployed": None, "last_invoked": None, "invocations": 0,
                                    "reused_by": [], "torn_down": None})
        if dep.get("reused_from"):
            row["reused_by"].append(st.get("run_id"))
        else:
            row["run"] = st.get("run_id")
            row["deployed"] = min((steps[x]["ts"] for x in STEPS if steps.get(x, {}).get("ok") and steps[x].get("ts")),
                                  default=None)
            row["torn_down"] = (dep.get("teardown") or {}).get("ts")
        inv = steps.get("invoke") or {}
        if inv.get("ok"):
            row["invocations"] += 1
            row["last_invoked"] = max(filter(None, [row["last_invoked"], inv.get("ts")]), default=None)
    out = sorted(rows.values(), key=lambda r: r["deployed"] or r["last_invoked"] or "")
    md = ["| Deployment | Repo | Run | Deployed | Last invoked | Torn down |", "|---|---|---|---|---|---|"]
    for r in out:
        run = r["run"] or f"(reused by {', '.join(r['reused_by'])})"
        if r["run"] and r["reused_by"]:
            run += f"; reused by {', '.join(r['reused_by'])}"
        md.append(f"| `{r['name']}` | {r['repo']} | {run} | {r['deployed'] or '—'} | {r['last_invoked'] or '—'} "
                  f"({r['invocations']}×) | {r['torn_down'] or '—'} |")
    return {"ok": True, "project": str(runs_dir.parent.parent), "deployments": out,
            "markdown": "\n".join(md) if out else "No deployments recorded in this project's runs."}


def _expected_repo_contents(run_p: Path, s: dict, bundle: Path) -> dict[str, str]:
    """For deploy logs without shas (written before teardown existed): what this run deployed,
    rebuilt from its own files. Empty when that can't be trusted (bundle changed, no deploy copy)."""
    copy_path = run_p / "deploy" / "workflow.json"
    if not copy_path.is_file() or tree_hash(bundle) != (s.get("deploy") or {}).get("bundle_hash"):
        return {}
    copy_wf = json.loads(copy_path.read_text())
    new = copy_wf["WorkflowName"]
    out = {f"functions/{new}/{fn}.py": (bundle / "functions" / f"{fn}.py").read_text()
           for fn in copy_wf.get("FunctionGitRepo") or {} if (bundle / "functions" / f"{fn}.py").is_file()}
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
    copy_path = run_p / "deploy" / "workflow.json"
    return json.loads(copy_path.read_text()) if copy_path.is_file() else \
        fill_placeholders(json.loads((bundle / "workflow.json").read_text()))[0]


def _s3_client_for(run_p: Path, bundle: Path, bucket: str, s3_factory):
    wf = _run_workflow(run_p, bundle)
    cfg = next((c for c in (wf.get("DataStores") or {}).values() if c.get("Bucket") == bucket), None)
    if cfg is None:
        raise StepError(f"bucket {bucket!r} is not in this run's workflow; its S3 objects are not touched")
    keys, _, problems = s3_keys(cfg)
    if not keys:
        raise StepError("S3 cannot be read: " + "; ".join(problems))
    return s3_factory(cfg, *keys)


def _list_keys(client, bucket: str, prefix: str) -> list[str]:
    keys, token = [], None
    while True:
        r = client.list_objects_v2(Bucket=bucket, Prefix=prefix, **({"ContinuationToken": token} if token else {}))
        keys += [o["Key"] for o in r.get("Contents") or []]
        if not r.get("IsTruncated"):
            return keys
        token = r.get("NextContinuationToken")


def teardown_plan(run: str, gh: GitHub | None, s3_factory=boto3_s3) -> dict:
    """Read-only: exactly what teardown would delete — the repo files and the S3 objects under the
    prefixes this run's deploy log records — or why it refuses."""
    run_p = Path(run)
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
    if not (run_p / DEPLOY_LOG).is_file() or not mine:
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
    logs_prefix = f"{_run_workflow(run_p, bundle).get('FaaSrLog') or 'FaaSrLog'}/{name}/"
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
        kind = "logs" if prefix == logs_prefix else "outputs"
        s3_objects += [{"bucket": bucket, "prefix": prefix, "key": k, "kind": kind} for k in keys]
    to_delete = [f for f in repo_files if f.get("state") == "delete"]
    plan_id = hashlib.sha256(json.dumps([[f["repo"], f["branch"], f["path"], f.get("sha")] for f in to_delete]
                                        + [[o["bucket"], o["key"]] for o in s3_objects]).encode()).hexdigest()[:12]
    reused_by = [st.get("run_id") for _, st in project_runs(run)
                 if (st.get("deploy") or {}).get("reused_from") == s.get("run_id")]
    lines = [f"**Teardown of `{name}`** (run {s.get('run_id')}; from its deploy log; nothing deleted yet)", "",
             "**Deploy files** (repo):"]
    lines += [f"- `{f['repo']}` ({f['branch']}): `{f['path']}`" + (" — already gone" if f.get("state") == "already gone" else "")
              for f in repo_files] or ["- none"]
    for kind, title in (("logs", "**FaaSr logs** (S3)"), ("outputs", "**Outputs** (S3, what the workflow wrote)")):
        lines += ["", title + ":"]
        shown = False
        for bucket, prefix in prefixes:
            keys = [o["key"] for o in s3_objects if (o["bucket"], o["prefix"]) == (bucket, prefix) and o["kind"] == kind]
            if keys or (kind == "logs") == (prefix == logs_prefix):
                lines.append(f"- `s3://{bucket}/{prefix}`: {len(keys)} object(s)")
                lines += [f"  - `{k}`" for k in keys]
                shown = True
        if not shown:
            lines.append("- none")
    lines.append("")
    if reused_by:
        lines.append(f"- Runs that invoked this deployment too (their records stay): {', '.join(reused_by)}")
    lines.append("- Not deleted: the GitHub Actions run history, anything not listed above.")
    lines += [f"- **Problem:** {x}" for x in problems]
    return {**base, "ok": not problems, "problems": problems, "repo_files": repo_files, "s3_objects": s3_objects,
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


def _download(run_p: Path, bundle: Path, objects: list[dict], dest: Path, s3_factory) -> dict:
    """Copy output objects to an empty local folder outside the run, with a manifest. Nothing is
    deleted here; a file holding a secret value is not saved (and so not deleted either)."""
    run_abs, dest = run_p.resolve(), dest.expanduser().resolve()
    if dest == run_abs or run_abs in dest.parents:
        raise StepError(f"{dest} is inside the run folder; choose a folder outside it")
    if dest.exists() and any(dest.iterdir()):
        raise StepError(f"{dest} is not empty; nothing is overwritten")
    files, skipped, clients = [], [], {}
    for o in objects:
        if o["bucket"] not in clients:
            clients[o["bucket"]] = _s3_client_for(run_p, bundle, o["bucket"], s3_factory)
        data = clients[o["bucket"]].get_object(Bucket=o["bucket"], Key=o["key"])["Body"].read()
        leaked = leakcheck.find_leaks(data.decode("utf-8", "ignore"))
        if leaked:
            skipped.append({"key": o["key"], "reason": f"contains secret values ({', '.join(leaked)})"})
            continue
        target = dest / o["key"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files.append({"key": o["key"], "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "manifest.json").write_text(json.dumps({"downloaded_at": _now_iso(), "files": files,
                                                    "skipped": skipped}, indent=2) + "\n")
    return {"to": str(dest), "files": len(files), "skipped": skipped, "manifest": str(dest / "manifest.json")}


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
        dest = Path(to) if to else run_p.resolve().parents[2] / "teardown-outputs" / plan["deploy_name"]
        try:
            saved = _download(run_p, bundle, out_objs, dest, s3_factory)
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
    for name, help_ in [("preflight", "read-only check of what deploying would do"),
                        *[(x, STEP_WHAT[x] + " (needs --confirmed)") for x in STEPS]]:
        q = sub.add_parser(name, help=help_)
        q.add_argument("--run", required=True)
        q.add_argument("--name-suffix", default=None,
                       help="deploy name suffix (default: $CURATE_DEPLOY_NAME_SUFFIX, else -<MMDD-HHMM> of the "
                            "run that approved Gate 4)")
        q.add_argument("--overwrite-own-deploy", action="store_true",
                       help="allow existing paths only if this run's own deploy log records them (and, in "
                            "the repo, they still carry its mark); everything else is still refused")
        if name in STEPS:
            q.add_argument("--confirmed", action="store_true",
                           help="the user said yes to this step (passed only after asking them)")
    q = sub.add_parser("reuse", help="invoke only: reuse the identical deployment of the run this one was "
                                     "started from (needs --confirmed)")
    q.add_argument("--run", required=True)
    q.add_argument("--name-suffix", default=None)
    q.add_argument("--confirmed", action="store_true")
    sub.add_parser("status", help="which deploy steps are done").add_argument("--run", required=True)
    q = sub.add_parser("list", help="deployments recorded in this project's runs (read-only)")
    q.add_argument("--project", default=".", help="the project folder (default: the current one)")
    q = sub.add_parser("teardown", help="delete exactly what one run's deploy log recorded (needs --confirmed "
                                        "and the --plan id shown)")
    q.add_argument("--run", required=True)
    q.add_argument("--confirmed", action="store_true")
    q.add_argument("--plan", help="the plan_id of the list the user saw")
    q.add_argument("--outputs", default="keep", choices=sorted(OUTPUT_CHOICES),
                   help="keep (default): delete only deploy files and FaaSr logs; delete: everything; "
                        "download: save the outputs first (--to), then delete everything")
    q.add_argument("--to", help="with --outputs download: an empty folder outside the run "
                                "(default <project>/teardown-outputs/<deploy name>)")
    args = p.parse_args(argv)
    envfile.load()  # ~/.config/curate/.env fills deploy variables the environment leaves unset
    if args.cmd in ("status", "list"):
        try:
            return _print(deploy_status(args.run) if args.cmd == "status" else list_deployments(args.project))
        except InputError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
    token, source = get_token()
    extra = {"gh_token": token} if token else None
    try:
        gh = GitHub(token, opener) if token else None
        if args.cmd == "teardown":
            return _print(teardown(args.run, gh, token, s3_factory, args.confirmed, args.plan, args.outputs,
                                   args.to), extra)
        override = args.name_suffix if args.name_suffix is not None else os.environ.get("CURATE_DEPLOY_NAME_SUFFIX")
        name = resolve_suffix(args.run, override or None, gh)
        suffix = name["suffix"]
        if args.cmd == "preflight":
            out = preflight(args.run, suffix, gh, source, s3_factory, args.overwrite_own_deploy,
                            name_source=name["source"])
            out.pop("_copy_wf", None)
        elif args.cmd == "reuse":
            out = reuse(args.run, suffix, gh, token, source, s3_factory, args.confirmed)
        else:
            out = deploy_step(args.run, args.cmd, suffix, gh, token, source, s3_factory,
                              args.confirmed, args.overwrite_own_deploy)
        return _print(out, extra)
    except InputError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
