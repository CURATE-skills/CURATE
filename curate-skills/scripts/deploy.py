# /// script
# requires-python = "==3.13.*"
# dependencies = []
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# The deploy steps follow CURATE-prototype src/faasr_agents/agents/wda.py 56-157 and the
# vendored deploy/ modules (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). See scripts/VENDORED.md.
"""curate-deploy: put an approved bundle on FaaSr (GitHub Actions) the way CURATE does.

D0 has only `preflight`, which is read-only: it reads the run, its bundle and (with a token)
the GitHub repo, and reports what deploying would write. Nothing is written anywhere.

Every curate-skills deployment gets its own name, WorkflowName + suffix (default "-cs"), so
CURATE's deployment of the same workflow is never touched:
  functions/<Name>-cs/<function>.py        (FunctionGitRepo points only there)
  workflows/<Name>-cs.json
  .github/workflows/<Name>-cs-<action>.yml
The approved bundle is not changed; the renamed copy exists only in the deploy step.

Credentials come only from environment variables (CURATE's names) or `gh auth token`, read
inside this script; they are never printed, written or passed on a command line.
Prints JSON with "ok"; exit 1 = needs attention, 2 = bad input.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import leakcheck  # noqa: E402

NATIVE_CONTAINERS = SCRIPT_DIR / "vendor" / "curate_deploy" / "native_containers.txt"
DEFAULT_SUFFIX = "-cs"
SUFFIX_RX = re.compile(r"^[-_A-Za-z0-9]+$")
COMMIT_TAG = "(curate-skills)"  # in every commit message curate-deploy makes
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
    """Read-only GitHub REST calls. `get` returns (status, headers, parsed body or None)."""

    def __init__(self, token: str, opener=None):
        self._token = token
        self._open = opener or urllib.request.urlopen

    def get(self, api_path: str, **params):
        url = API + api_path + ("?" + urllib.parse.urlencode(params) if params else "")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "curate-skills"})
        try:
            with self._open(req, timeout=30) as r:
                return r.status, dict(r.headers), json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers or {}), None


# ── the run and its bundle ───────────────────────────────────────────────────

def tree_hash(folder: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
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

def deploy_copy(wf: dict, suffix: str, user: str, repo: str, branch: str) -> tuple[dict, list[dict]]:
    """The workflow as curate-deploy would upload it, and every repo path it would write."""
    name = wf["WorkflowName"]
    new = name + suffix
    out = copy.deepcopy(wf)
    out["WorkflowName"] = new
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
    return out, targets


def name_problems(copy_wf: dict, targets: list[dict], suffix: str, user: str, repo: str,
                  branch: str) -> list[str]:
    """Every written path and every FunctionGitRepo entry must belong to the suffixed name."""
    new = copy_wf["WorkflowName"]
    out = []
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


def preflight(run: str, suffix: str, gh: GitHub | None = None, token_source: str = "none") -> dict:
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
    elif not s.get("bundle_hash"):
        warnings.append("this run has no recorded bundle hash (approved before deploy existed); "
                        "the bundle is checked as it is now")
    if (ph := placeholders(wf)):
        problems.append("the bundle still has placeholders (set the FAASR_* variables and re-run "
                        "curate-emit): " + ", ".join(ph))
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
    if missing_env:
        problems.append(f"environment variables not set: {', '.join(missing_env)}")
    for sname, cs in servers.items():
        for field, want in (("UserName", user), ("ActionRepoName", repo), ("Branch", branch)):
            if want and cs.get(field) not in (want, None) and "<FAASR_" not in str(cs.get(field)):
                problems.append(f"bundle ComputeServers.{sname}.{field} = {cs.get(field)!r} but the "
                                f"deploy target is {want!r}")
    local_s3 = [n for n in ("S3_AccessKey", "S3_SecretKey") if not os.environ.get(n)]
    if local_s3:
        warnings.append(f"{', '.join(local_s3)} not set here: deploying works, but curate-monitor "
                        "needs them to read the run's logs from S3")

    copy_wf, targets = deploy_copy(wf, suffix, user or "<user>", repo or "<repo>", branch)
    problems += name_problems(copy_wf, targets, suffix, user or "<user>", repo or "<repo>", branch)
    req_secrets = required_secret_names(wf)
    out = {"run": str(Path(run).resolve()), "bundle": str(bundle), "bundle_hash": bhash,
           "approved_name": wf.get("WorkflowName"), "deploy_name": copy_wf["WorkflowName"],
           "suffix": suffix, "repo": f"{user}/{repo}" if user and repo else None, "branch": branch,
           "token_source": token_source, "targets": targets, "function_git_repo": copy_wf["FunctionGitRepo"],
           "secrets": {"required": req_secrets}, "curate_files": [], "github_checked": False}

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
                have = sorted(x["name"] for x in (sec or {}).get("secrets", []))
                out["secrets"].update(present=[n for n in req_secrets if n in have],
                                      missing=[n for n in req_secrets if n not in have])
                if out["secrets"]["missing"]:
                    problems.append(f"GitHub secrets missing in {user}/{repo}: "
                                    f"{', '.join(out['secrets']['missing'])} (add with `gh secret set NAME`)")
            else:
                warnings.append(f"could not list the repo's secret names (HTTP {st}); check by hand: "
                                + ", ".join(req_secrets))
            new = copy_wf["WorkflowName"]
            for t in targets:
                t.update(_owner_of(gh, user, repo, branch, t, new))
                if t.get("exists") and not t.get("ours"):
                    problems.append(f"{t['path']} already exists and does not belong to a curate-skills "
                                    f"deployment named {new!r} ({t.get('why')}); refusing to overwrite it")
            name = wf["WorkflowName"]
            for p in [f"workflows/{name}.json"] + [f"functions/{fn}.py" for fn in sorted(copy_wf["FunctionGitRepo"])] \
                     + [f".github/workflows/{name}-{a}.yml" for a in sorted(wf.get("ActionList") or {})]:
                st, _, _ = gh.get(f"/repos/{user}/{repo}/contents/{p}", ref=branch)
                if st == 200:
                    out["curate_files"].append(p)
    out.update(ok=not problems, problems=problems, warnings=warnings)
    out["summary"] = preflight_summary(out)
    return out


def preflight_summary(p: dict) -> str:
    lines = [f"**Deploy preflight — {'ready' if p['ok'] else 'not ready'}** (read-only; nothing was written)",
             "", f"- Approved bundle: `{p['bundle']}` (workflow `{p['approved_name']}`)",
             f"- Deploys as: `{p['deploy_name']}` to `{p['repo']}` branch `{p['branch']}` "
             f"(token: {p['token_source']}; GitHub {'checked' if p['github_checked'] else 'not checked'})"]
    by_step: dict[str, list[str]] = {}
    for t in p["targets"]:
        if t.get("exists") is None:
            state = "not checked"
        elif not t["exists"]:
            state = "new"
        else:
            state = "exists, ours — overwritten" if t.get("ours") else "EXISTS, NOT OURS — refused"
        by_step.setdefault(t["step"], []).append(f"`{t['path']}` ({state})")
    for step in ("publish", "upload", "register"):
        if step in by_step:
            lines.append(f"- {step}: " + "; ".join(by_step[step]))
    lines.append("- invoke: workflow_dispatch of the entry action (FaaSr Scheduler), as CURATE")
    if p["curate_files"]:
        lines.append(f"- CURATE's own files found and left untouched: {', '.join(p['curate_files'])}")
    sec = p["secrets"]
    lines.append(f"- GitHub secrets needed: {', '.join(sec['required'])}"
                 + (f" — missing: {', '.join(sec['missing'])}" if sec.get("missing") else
                    " — all present" if "present" in sec else " — not checked"))
    lines += [f"- **Problem:** {x}" for x in p["problems"]]
    lines += [f"- Note: {x}" for x in p["warnings"]]
    return "\n".join(lines)


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


def main(argv=None, opener=None) -> int:
    p = argparse.ArgumentParser(prog="deploy.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    pf = sub.add_parser("preflight", help="read-only check of what deploying would do")
    pf.add_argument("--run", required=True)
    pf.add_argument("--name-suffix", default=None,
                    help=f"deploy name suffix (default: $CURATE_DEPLOY_NAME_SUFFIX or {DEFAULT_SUFFIX!r})")
    args = p.parse_args(argv)
    suffix = args.name_suffix if args.name_suffix is not None else \
        (os.environ.get("CURATE_DEPLOY_NAME_SUFFIX") or DEFAULT_SUFFIX)
    token, source = get_token()
    extra = {"gh_token": token} if token else None
    try:
        gh = GitHub(token, opener) if token else None
        return _print(preflight(args.run, suffix, gh, source), extra)
    except InputError as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
