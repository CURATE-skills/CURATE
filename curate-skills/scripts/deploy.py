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

Every curate-skills deployment gets its own name, WorkflowName + suffix (default "-cs"), so
CURATE's deployment of the same workflow is never touched:
  functions/<Name>-cs/<function>.py        (FunctionGitRepo points only there)
  workflows/<Name>-cs.json
  .github/workflows/<Name>-cs-<action>.yml
  S3: <folder>-cs/... (each action's "folder" argument) and FaaSrLog/<Name>-cs/...
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
import leakcheck  # noqa: E402

NATIVE_CONTAINERS = SCRIPT_DIR / "vendor" / "curate_deploy" / "native_containers.txt"
DEFAULT_SUFFIX = "-cs"
SUFFIX_RX = re.compile(r"^[-_A-Za-z0-9]+$")
COMMIT_TAG = "(curate-skills)"  # in every commit message curate-deploy makes
DEPLOY_LOG = Path("deploy") / "deploy-log.jsonl"  # in the run; one JSON line per path a deploy step wrote
# The only fields in which the deploy copy may differ from the approved bundle (besides fields that
# held an emit placeholder). Anything else is refused: the deploy must run what Gate 4 approved.
ALLOWED_DIFF = [("WorkflowName", "suffix"),
                (r"ActionList\.[^.]+\.Arguments\.folder", "suffix (S3 folder)"),
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
    gh = shutil.which("gh")
    if gh:
        try:
            p = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return None, "none"
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip(), "gh auth token"
    return None, "none"


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


def gate4_problem(s: dict) -> str | None:
    hist = s.get("history") or []
    approvals = [i for i, h in enumerate(hist) if h.get("gate") == 4 and h.get("decision") == "approve"]
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


def own_paths(entries: list[dict], run_id: str, deploy_name: str, repo: str, branch: str
              ) -> tuple[set[str], set[tuple[str, str]]]:
    """Repo paths and (bucket, prefix) pairs this run's deploy log says it wrote under this name."""
    mine = [e for e in entries if e.get("run_id") == run_id and e.get("deploy_name") == deploy_name]
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
              require_hash: bool = False) -> dict:
    """Read-only. done_steps: deploy steps this run already finished; their paths must exist,
    carry curate-skills' mark and be in this run's deploy log (they are not written again).
    require_hash: refuse without a bundle hash recorded at Gate 4 (every deploy step does)."""
    if not suffix or not SUFFIX_RX.match(suffix):
        raise InputError(f"deploy name suffix {suffix!r} must be non-empty letters, digits, '-' or '_'")
    s, bundle = load_run(run)
    wf = json.loads((bundle / "workflow.json").read_text())
    problems, warnings = [], []
    if (p := gate4_problem(s)):
        problems.append(p)
    bhash = tree_hash(bundle)
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
                        + f"); the deploy copy's renamed {suffix!r} folder would not have them")
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
    req_secrets = required_secret_names(wf)
    diff_rows, diff_problems = deploy_diff(wf, copy_wf, filled)
    problems += diff_problems
    log_entries, bad_lines = read_deploy_log(run)
    warnings += bad_lines
    own_files, own_s3 = own_paths(log_entries, s.get("run_id"), copy_wf["WorkflowName"],
                                  f"{user}/{repo}", branch)
    out = {"run": str(Path(run).resolve()), "bundle": str(bundle), "bundle_hash": bhash,
           "approved_name": wf.get("WorkflowName"), "deploy_name": copy_wf["WorkflowName"],
           "suffix": suffix, "repo": f"{user}/{repo}" if user and repo else None, "branch": branch,
           "token_source": token_source, "targets": targets, "function_git_repo": copy_wf["FunctionGitRepo"],
           "filled_placeholders": {k: ", ".join(f"${x}" for x in v) + f" = {_dig(copy_wf, k)}" for k, v in filled.items()
                                   if not k.startswith("FunctionGitRepo")},
           "s3_targets": s3_targets, "secrets": {"required": req_secrets}, "curate_files": [],
           "github_checked": False, "deploy_diff": diff_rows,
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
                out["token_scopes"] = "fine-grained (needs Contents and Workflows: read and write)"
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
             f"(token: {p['token_source']}; GitHub {'checked' if p['github_checked'] else 'not checked'})"]
    by_step: dict[str, list[str]] = {}
    for t in p["targets"]:
        if t.get("written"):
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
                 "exists, in this run's deploy log — will be overwritten (--overwrite-own-deploy)"
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
        lines += [f"| {d['what']} (derived from the name) | `{d['approved']}` | `{d['deploy']}` | suffix |"
                  for d in p.get("derived_changes") or []]
    return "\n".join(lines)


# ── the four deploy steps ────────────────────────────────────────────────────
# Each one writes only after the user said yes to it (--confirmed), re-runs the preflight first
# (bundle hash, deploy-copy rule, names, existing paths), records each path it wrote in the deploy
# log as soon as it is written, and records the step in state.json. A finished step is skipped.

STEPS = ("publish", "upload", "register", "invoke")
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

    pf = preflight(run, suffix, gh, token_source, s3_factory, overwrite_own, done_steps=done, require_hash=True)
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
                _log_written(run_p, {**log_base, "repo": f"{user}/{repo}", "branch": branch, "path": path})
            detail = {"written": paths}
        elif step == "upload":
            path = f"workflows/{new}.json"
            text = _checked(json.dumps(copy_wf, indent=2), path, extra)
            (run_p / "deploy").mkdir(exist_ok=True)
            (run_p / "deploy" / "workflow.json").write_text(text + "\n")
            put_file(gh, user, repo, path, text, f"Add/update workflow payload {new} (curate-skills)", branch)
            _log_written(run_p, {**log_base, "repo": f"{user}/{repo}", "branch": branch, "path": path})
            detail = {"written": [path], "local_copy": str(run_p / "deploy" / "workflow.json")}
        elif step == "register":
            log = RUNNERS["register"](copy_wf, token, extra)
            for path in paths:
                _log_written(run_p, {**log_base, "repo": f"{user}/{repo}", "branch": branch, "path": path})
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
            # Until curate-monitor exists (D2), the run stops here: saving to the catalog comes only
            # after the user has checked that the deployed run worked.
            st.update(stage="stopped", stopped_from="finalize", begun=None)
    s = _update_state(run_p, finish)
    done = [x for x in STEPS if s["deploy"]["steps"].get(x, {}).get("ok")]
    nxt = next((x for x in STEPS if x not in done), None)
    out = {**base, "ok": True, "done": done, "next_step": nxt, **detail, "next_stage": s["stage"]}
    if step == "invoke":
        resume = f"/curate-skills:curate --resume {s['run_id']}"
        out.update(stopped=True, resume_command=resume,
                   resume_stage_command=f"/curate-skills:wda-finalize --run {s['run_id']}",
                   message=f"Deployed: {new}, invocation {detail['invocation_id']}. The run is stopped. Check "
                           f"Actions and S3; resume with {resume} to finalize.")
    return out


def deploy_status(run: str) -> dict:
    s, _ = load_run(run)
    rec = s.get("deploy") or {}
    steps = rec.get("steps") or {}
    done = [x for x in STEPS if steps.get(x, {}).get("ok")]
    failed = {x: steps[x].get("error") for x in STEPS if x in steps and not steps[x].get("ok") and steps[x].get("error")}
    return {"ok": True, "stage": s.get("stage"), "deploy_choice": s.get("deploy_choice"), "done": done,
            "failed": failed, "next_step": next((x for x in STEPS if x not in done), None),
            "deploy_name": rec.get("deploy_name"), "invocation_id": rec.get("invocation_id"),
            "deploy_log": str(Path(run) / DEPLOY_LOG) if (Path(run) / DEPLOY_LOG).is_file() else None}


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
                       help=f"deploy name suffix (default: $CURATE_DEPLOY_NAME_SUFFIX or {DEFAULT_SUFFIX!r})")
        q.add_argument("--overwrite-own-deploy", action="store_true",
                       help="allow existing paths only if this run's own deploy log records them (and, in "
                            "the repo, they still carry its mark); everything else is still refused")
        if name in STEPS:
            q.add_argument("--confirmed", action="store_true",
                           help="the user said yes to this step (passed only after asking them)")
    sub.add_parser("status", help="which deploy steps are done").add_argument("--run", required=True)
    args = p.parse_args(argv)
    if args.cmd == "status":
        try:
            return _print(deploy_status(args.run))
        except InputError as e:
            print(json.dumps({"ok": False, "error": str(e)}))
            return 2
    suffix = args.name_suffix if args.name_suffix is not None else \
        (os.environ.get("CURATE_DEPLOY_NAME_SUFFIX") or DEFAULT_SUFFIX)
    token, source = get_token()
    extra = {"gh_token": token} if token else None
    try:
        gh = GitHub(token, opener) if token else None
        if args.cmd == "preflight":
            out = preflight(args.run, suffix, gh, source, s3_factory, args.overwrite_own_deploy)
            out.pop("_copy_wf", None)
        else:
            out = deploy_step(args.run, args.cmd, suffix, gh, token, source, s3_factory,
                              args.confirmed, args.overwrite_own_deploy)
        return _print(out, extra)
    except InputError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
