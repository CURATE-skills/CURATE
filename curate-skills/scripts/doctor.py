#!/usr/bin/env python3
"""curate-doctor: is this machine ready to run curate-skills? Read-only.

  python3 doctor.py [--deploy [--online]] [--plugin-root DIR] [--catalog-dir DIR]
                    [--config-catalog-dir VALUE] [--json] [--skip-background-check]

--skip-background-check (curate-setup's final run only) leaves out that one check, shown as "skip";
every other core check still runs.

Core checks (always): uv; the python3 the write-guard hook runs (>= 3.9); uv can provide
Python 3.13 with the stage packages and FaaSr_py; CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1;
the plugin is not loaded twice; the catalog folder is writable; `.curate/` is git-ignored (warn).
--deploy adds: the deploy variables are set (names only) and a GitHub token is available
(GH_PAT, else `gh auth token`). --online adds: the action repo and branch are readable with that
token, and the S3 bucket answers head_bucket (read-only; run through uv with deploy.py's client).

Prints one line per check: ok / warn / FAIL. Exit 1 if any check that ran FAILed (core, or
deploy when asked for), else 0. Never prints a credential value: every line goes through
leakcheck.redact, with the token doctor fetched itself added.

Runs on the system python3 (3.9+), standard library only, so it works before uv is installed.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import leakcheck  # noqa: E402  (standard library only)

PLUGIN_NAME = "curate-skills"
FOREGROUND_VAR = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
MIN_HOOK_PY = (3, 9)
FAASR_PY = "FaaSr_py==0.1.13"  # as deploy.py / monitor.py / faasr_validate.py
DEPLOY_VARS = ("FAASR_GH_USERNAME", "FAASR_ACTION_REPO", "FAASR_S3_ENDPOINT", "FAASR_S3_BUCKET",
               "FAASR_S3_REGION")  # emit.PLACEHOLDER_VARS: filled into the deploy copy
S3_KEY_VARS = ("S3_AccessKey", "S3_SecretKey")  # or their upper-case spelling, as deploy.env_key
UV_TIMEOUT = 600  # first run downloads Python 3.13 and packages
LAUNCH = f"{FOREGROUND_VAR}=1 claude --plugin-dir <path to curate-skills>"
CLASSIC_HOWTO = ("Create a classic token (GitHub → Settings → Developer settings → Personal access tokens → "
                 "Tokens (classic)) with scopes repo and workflow, and put it in GH_PAT")
SKIP_BACKGROUND_WHY = ("not checked here (curate-setup): the `curate` and `curate-deploy` aliases both set it, "
                       "and setup and deploy spawn no subagents")


class Report:
    def __init__(self):
        self.rows: list[dict] = []
        self.secrets: dict[str, str] = {}  # extra values to redact (a token doctor fetched)

    def add(self, group: str, name: str, status: str, msg: str):
        self.rows.append({"group": group, "check": name, "status": status,
                          "message": leakcheck.redact(msg, extra_values=self.secrets)})

    def failed(self) -> bool:
        return any(r["status"] == "fail" for r in self.rows)


def _run(cmd, timeout=60, **kw):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout}s"
    except OSError as e:
        return None, "", f"{type(e).__name__}: {e}"


def _tail(text: str, n=3) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return " | ".join(lines[-n:]) if lines else "no output"


# ── core ─────────────────────────────────────────────────────────────────────

def check_uv(r: Report) -> str | None:
    uv = shutil.which("uv")
    if not uv:
        r.add("core", "uv", "fail", "uv not found on PATH. Every curate script runs through uv. Install it "
              "(https://docs.astral.sh/uv/getting-started/installation/), open a new terminal, relaunch.")
        return None
    code, out, err = _run([uv, "--version"], timeout=20)
    r.add("core", "uv", "ok", f"{(out or err).strip() or 'uv'} at {uv}")
    return uv


def check_hook_python(r: Report):
    py = shutil.which("python3")
    if not py:
        r.add("core", "python3 (hook)", "fail", "python3 not found on PATH. The write-guard hook runs "
              "`python3`; without it Claude Code BLOCKS EVERY Write, Edit and Bash call while this plugin "
              "is enabled. Install Python 3.9+ (macOS: `xcode-select --install`), or disable the plugin.")
        return
    code, out, err = _run([py, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"], timeout=30)
    ver = out.strip()
    try:
        ok = code == 0 and tuple(int(x) for x in ver.split(".")[:2]) >= MIN_HOOK_PY
    except ValueError:
        ok = False
    if ok:
        r.add("core", "python3 (hook)", "ok", f"Python {ver} at {py}")
    else:
        r.add("core", "python3 (hook)", "fail", f"python3 at {py} is {ver or 'not runnable'} ({_tail(err)}); "
              "the write-guard hook needs 3.9+, and if it cannot run, Claude Code blocks every Write, Edit "
              "and Bash call while this plugin is enabled. Put Python 3.9+ first on PATH.")


def check_uv_envs(r: Report, uv: str | None):
    if not uv:
        r.add("core", "Python 3.13 + packages", "fail", "not checked: uv is missing (see above).")
        return
    # The stage scripts (pydantic, rank-bm25) and the FaaSr_py validator, each in its own uv env,
    # exactly as the skills run them. Both are read-only; the validator gets an empty workflow.
    code, out, err = _run([uv, "run", "--quiet", str(SCRIPT_DIR / "spec.py"), "--help"], timeout=UV_TIMEOUT)
    stage_ok = code == 0
    code2, out2, err2 = _run([uv, "run", "--quiet", str(SCRIPT_DIR / "faasr_validate.py")], timeout=UV_TIMEOUT,
                             input="{}")
    try:
        res = json.loads(out2.strip().splitlines()[-1])
        val_ok = not res.get("unavailable")
    except (IndexError, ValueError, AttributeError):
        res, val_ok = {}, False
    if stage_ok and val_ok:
        r.add("core", "Python 3.13 + packages", "ok", "uv provides Python 3.13 with pydantic, rank-bm25 "
              f"and {FAASR_PY} (cached for later runs)")
        return
    what = ([] if stage_ok else [f"stage scripts: {_tail(err)}"]) + \
           ([] if val_ok else [f"FaaSr_py: {res.get('error') or _tail(err2)}"])
    r.add("core", "Python 3.13 + packages", "fail", "uv could not set up Python 3.13 and the pinned "
          "packages (" + "; ".join(what) + "). It needs network access the first time (PyPI and "
          "python-build-standalone); check your connection or proxy, then run doctor again.")


def check_foreground(r: Report, env):
    val = env.get(FOREGROUND_VAR)
    if val == "1":
        r.add("core", FOREGROUND_VAR, "ok", "set to 1: generation waits for each curate-mga subagent")
    elif env.get("CLAUDECODE") != "1":
        r.add("core", FOREGROUND_VAR, "warn", "not inside Claude Code, so this session's value can't be "
              "checked. Run /curate-skills:curate-doctor from Claude Code started with the `curate` alias.")
    else:
        r.add("core", FOREGROUND_VAR, "fail", f"{'not set' if val is None else 'set to ' + repr(val)} in this "
              "Claude Code session: generation would stall after every function. Quit and relaunch with "
              f"the `curate` alias: {LAUNCH}")


def _installed_entries(claude_dir: Path, project: Path) -> tuple[list[str], list[str]]:
    """`curate-skills@<marketplace>` installs that apply to this project: user-scope installs (no
    projectPath) everywhere, project/local-scope ones only here. Returns (enabled, unknown):
    enabled = enabledPlugins says true in the user, project or local settings (later wins);
    unknown = installed but not listed in any of them."""
    try:
        data = json.loads((claude_dir / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return [], []
    enabled: dict = {}
    for f in (claude_dir / "settings.json", project / ".claude" / "settings.json",
              project / ".claude" / "settings.local.json"):
        try:
            enabled.update(json.loads(f.read_text()).get("enabledPlugins") or {})
        except (OSError, ValueError, AttributeError):
            pass
    on, unknown = [], []
    for key, entries in (data.get("plugins") or {}).items():
        if not key.startswith(PLUGIN_NAME + "@"):
            continue
        for e in entries if isinstance(entries, list) else [entries]:
            e = e or {}
            pp = e.get("projectPath")
            if pp and Path(pp).resolve() != project.resolve():
                continue  # a project/local-scope install for another project
            label = f"{key} ({e.get('scope') or ('project' if pp else 'user')} scope)"
            if enabled.get(key) is True:
                on.append(label)
            elif key not in enabled:
                unknown.append(label)
            break
    return on, unknown


def check_loaded_once(r: Report, plugin_root: str | None, claude_dir: Path, project: Path):
    installed, unknown = _installed_entries(claude_dir, project)
    root = plugin_root if plugin_root and not plugin_root.startswith("${") else None
    fix = "Uninstall it (/plugin uninstall <name>) or drop --plugin-dir from the alias."
    if root is None:
        both = installed + unknown
        if both:
            r.add("core", "loaded once", "warn", "can't tell how the plugin is loaded outside Claude Code; it is "
                  f"also installed as {', '.join(both)}. If your alias uses --plugin-dir: {fix}")
        else:
            r.add("core", "loaded once", "ok", "no installed copy; loaded only via --plugin-dir")
        return
    cache = (claude_dir / "plugins").resolve()
    via_dir = cache not in Path(root).resolve().parents
    if via_dir and installed:
        r.add("core", "loaded once", "fail", f"loaded twice: --plugin-dir {root} and the enabled install "
              f"{', '.join(installed)}. Skills, the subagent and the hook would run twice under one name. {fix}")
    elif via_dir and unknown:
        r.add("core", "loaded once", "warn", f"--plugin-dir {root}, and also installed as {', '.join(unknown)} "
              "(not listed in enabledPlugins, so it may be enabled). Check /plugin; if it is enabled: " + fix)
    else:
        r.add("core", "loaded once", "ok", f"{'--plugin-dir' if via_dir else 'installed'}: {root}")


def resolve_catalog(flag, env, config) -> tuple[Path, str]:
    """As catalog.py (without a run): --catalog-dir > CURATE_CATALOG_DIR > plugin setting > default."""
    for value, source in ((flag, "--catalog-dir"), (env.get("CURATE_CATALOG_DIR"), "CURATE_CATALOG_DIR"),
                          (config, "plugin setting")):
        if value and str(value).strip() and "${" not in str(value):
            return Path(str(value).strip()).expanduser().resolve(), source
    return (Path.home() / ".curate" / "catalog").resolve(), "default"


def check_catalog(r: Report, flag, env, config):
    path, source = resolve_catalog(flag, env, config)
    if path.exists():
        if not path.is_dir():
            r.add("core", "catalog folder", "fail", f"{path} (from {source}) exists but is not a folder.")
        elif os.access(path, os.W_OK | os.X_OK):
            r.add("core", "catalog folder", "ok", f"{path} (from {source}), writable")
        else:
            r.add("core", "catalog folder", "fail", f"{path} (from {source}) is not writable; finalize "
                  "saves verified functions there. Fix its permissions or set CURATE_CATALOG_DIR.")
        return
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if os.access(parent, os.W_OK | os.X_OK):
        r.add("core", "catalog folder", "ok", f"{path} (from {source}) will be created on first use")
    else:
        r.add("core", "catalog folder", "fail", f"{path} (from {source}) can't be created: {parent} is not "
              "writable. Set CURATE_CATALOG_DIR to a writable folder.")


def check_gitignore(r: Report, project: Path):
    git = shutil.which("git")
    if not git:
        r.add("core", ".curate/ ignored", "warn", "git not found; if this project is a git repo, add "
              "`.curate/` to its .gitignore.")
        return
    code, out, _ = _run([git, "-C", str(project), "rev-parse", "--is-inside-work-tree"], timeout=20)
    if code != 0 or out.strip() != "true":
        r.add("core", ".curate/ ignored", "ok", f"{project} is not a git repo")
        return
    code, _, _ = _run([git, "-C", str(project), "check-ignore", "-q", ".curate/"], timeout=20)
    if code == 0:
        r.add("core", ".curate/ ignored", "ok", "git ignores .curate/")
    else:
        r.add("core", ".curate/ ignored", "warn", f"git in {project} does not ignore .curate/ (run state, "
              "subagent locks). Add a line `.curate/` to .gitignore.")


# ── deploy ───────────────────────────────────────────────────────────────────

def _key_set(env, name) -> bool:
    return bool(env.get(name) or env.get(name.upper()))


def check_deploy_vars(r: Report, env) -> bool:
    missing = [n for n in DEPLOY_VARS if not str(env.get(n, "")).strip()]
    missing += [f"{n} (or {n.upper()})" for n in S3_KEY_VARS if not _key_set(env, n)]
    branch = str(env.get("FAASR_ACTION_BRANCH", "")).strip()
    if missing:
        r.add("deploy", "variables", "fail", f"not set: {', '.join(missing)}. Run /curate-skills:curate-setup, "
              "fill in ~/.config/curate/.env, and launch with the deploy alias it prints.")
    else:
        r.add("deploy", "variables", "ok", f"set: {', '.join(DEPLOY_VARS + S3_KEY_VARS)}; branch "
              f"{branch or 'main (FAASR_ACTION_BRANCH unset)'} (values not shown)")
    for n in ("GITHUB_REPOSITORY", "GITHUB_REF_NAME"):
        if env.get(n):
            r.add("deploy", "variables", "fail", f"{n} is set here; CURATE's deploy code would use it instead of "
                  "FAASR_GH_USERNAME / FAASR_ACTION_REPO / FAASR_ACTION_BRANCH. Unset it.")
    return not missing


def gh_cli_token() -> str | None:
    gh = shutil.which("gh")
    if not gh:
        return None
    code, out, _ = _run([gh, "auth", "token"], timeout=20)
    return out.strip() if code == 0 and out.strip() else None


def check_token(r: Report, env) -> str | None:
    tok = str(env.get("GH_PAT", "")).strip()
    if tok:
        r.secrets["GH_PAT"] = tok
        r.add("deploy", "GitHub token", "ok", "from GH_PAT (value not shown)")
        return tok
    tok = gh_cli_token()
    if tok:
        r.secrets["gh auth token"] = tok
        r.add("deploy", "GitHub token", "ok", "from `gh auth token` (GH_PAT unset; value not shown)")
        return tok
    r.add("deploy", "GitHub token", "fail", "no token: GH_PAT is not set and `gh auth token` gave none. "
          f"{CLASSIC_HOWTO} (in ~/.config/curate/.env).")
    return None


def _gh_get(url, token, opener=None):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}",
                                               "Accept": "application/vnd.github+json",
                                               "User-Agent": "curate-skills-doctor"})
    try:
        with (opener or urllib.request.urlopen)(req, timeout=20) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), None
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, {}, f"{type(e).__name__}"


# What deploy and monitor do with the token, as fine-grained repository permissions.
FINE_GRAINED_NEEDS = "Contents, Workflows and Actions: read and write; Secrets: read"
# Read-only probes for the read half of those (path under /repos/<user>/<repo>, permission).
FINE_GRAINED_READ_PROBES = (("/contents/?ref={branch}", "Contents: read"),
                            ("/actions/workflows?per_page=1", "Actions: read"))


def token_kind(token: str, headers: dict) -> str:
    """classic (incl. gh's OAuth token): GitHub sends X-OAuth-Scopes. fine-grained: github_pat_…
    and no scope header. other: no scope header and an unknown prefix (e.g. a GitHub App token)."""
    if "x-oauth-scopes" in headers:
        return "classic"
    return "fine-grained" if token.startswith("github_pat_") else "other"


def check_repo_online(r: Report, env, token, opener=None):
    user, repo = str(env.get("FAASR_GH_USERNAME", "")).strip(), str(env.get("FAASR_ACTION_REPO", "")).strip()
    branch = str(env.get("FAASR_ACTION_BRANCH", "")).strip() or "main"
    if not (user and repo and token):
        r.add("deploy", "action repo", "fail", "not checked: FAASR_GH_USERNAME, FAASR_ACTION_REPO or the "
              "token is missing (see above).")
        return
    base = f"https://api.github.com/repos/{user}/{repo}"
    status, headers, body = _gh_get(base, token, opener)
    headers = {k.lower(): v for k, v in headers.items()}
    if status != 200:
        why = {401: f"the token was rejected (expired or revoked?). {CLASSIC_HOWTO}",
               404: "not found, or the token can't see it"}.get(status, f"HTTP {status or body}")
        r.add("deploy", "action repo", "fail", f"{user}/{repo}: {why}. Create the repo (see curate-setup) "
              "or fix FAASR_GH_USERNAME / FAASR_ACTION_REPO / the token.")
        return

    # The repo itself: push allowed, branch exists and is the default branch (as deploy's preflight).
    perms = (body or {}).get("permissions") or {}
    notes = []
    if perms and not perms.get("push"):
        notes.append("you can read it but not push (deploy commits the workflow files and functions)")
    status_b, _, _ = _gh_get(f"{base}/branches/{branch}", token, opener)
    if status_b != 200:
        notes.append(f"branch {branch!r} not found (HTTP {status_b}); create it or set FAASR_ACTION_BRANCH")
    default = (body or {}).get("default_branch")
    if default and default != branch:
        notes.append(f"its default branch is {default!r} but FAASR_ACTION_BRANCH is {branch!r}; deploy needs "
                     "them to be the same")
    if notes:
        r.add("deploy", "action repo", "fail", f"{user}/{repo}: " + "; ".join(notes) + ".")
    else:
        r.add("deploy", "action repo", "ok", f"{user}/{repo} branch {branch}: readable, push allowed, default branch")

    # The token's own permissions.
    kind = token_kind(token, headers)
    if kind == "classic":
        scopes = {x.strip() for x in headers["x-oauth-scopes"].split(",") if x.strip()}
        lack = sorted({"repo", "workflow"} - scopes)
        if lack:
            r.add("deploy", "token permissions", "fail", f"classic token with scopes {sorted(scopes)}: lacks "
                  f"{lack}. Deploy needs repo and workflow (workflow to write .github/workflows/). "
                  f"{CLASSIC_HOWTO}.")
        else:
            r.add("deploy", "token permissions", "ok", "classic token with scopes repo and workflow")
        return
    label = "fine-grained token" if kind == "fine-grained" else "token without a scope header (not classic)"
    denied = []
    for path, perm in FINE_GRAINED_READ_PROBES:
        st, _, _ = _gh_get(base + path.format(branch=branch), token, opener)
        if st in (401, 403, 404):
            denied.append(f"{perm} (HTTP {st})")
    if denied:
        r.add("deploy", "token permissions", "fail", f"{label}: not granted on {user}/{repo}: "
              f"{', '.join(denied)}. Recommended: use a classic token (repo + workflow) instead. "
              f"{CLASSIC_HOWTO}. (A fine-grained token needs {FINE_GRAINED_NEEDS} on this repository.)")
    else:
        r.add("deploy", "token permissions", "warn", f"can't verify scopes ({label}): Contents and Actions "
              "read access confirmed. Recommended: use a classic token (repo + workflow); fine-grained write "
              f"access can't be checked. {CLASSIC_HOWTO}. Otherwise make sure this token has "
              f"{FINE_GRAINED_NEEDS} on {user}/{repo}.")


def check_s3_online(r: Report, uv: str | None, env):
    if not uv:
        r.add("deploy", "S3 bucket", "fail", "not checked: uv is missing.")
        return
    code, out, err = _run([uv, "run", "--quiet", "--no-project", "--python", "3.13", "--with", FAASR_PY,
                           "python", str(Path(__file__).resolve()), "_s3probe"], timeout=UV_TIMEOUT, env=dict(env))
    try:
        res = json.loads(out.strip().splitlines()[-1])
    except (IndexError, ValueError):
        res = {"ok": False, "error": f"probe did not run ({_tail(err)})"}
    bucket = env.get("FAASR_S3_BUCKET", "")
    if res.get("ok"):
        r.add("deploy", "S3 bucket", "ok", f"{bucket} answers head_bucket at {env.get('FAASR_S3_ENDPOINT')}")
    else:
        r.add("deploy", "S3 bucket", "fail", f"{bucket or '(FAASR_S3_BUCKET unset)'}: {res.get('error')}. Check "
              "FAASR_S3_ENDPOINT / FAASR_S3_REGION / the bucket name and that S3_AccessKey / S3_SecretKey "
              "belong to it.")


def s3_probe() -> int:
    """Run under uv (Python 3.13 + FaaSr_py, which pins boto3): head_bucket with deploy.py's client."""
    D = None
    try:
        import deploy as D
        cfg = {"Endpoint": os.environ.get("FAASR_S3_ENDPOINT", "").strip(),
               "Bucket": os.environ.get("FAASR_S3_BUCKET", "").strip(),
               "Region": os.environ.get("FAASR_S3_REGION", "").strip() or None}
        if not cfg["Bucket"]:
            print(json.dumps({"ok": False, "error": "FAASR_S3_BUCKET not set"}))
            return 0
        keys, _, problems = D.s3_keys(cfg)
        if problems:
            print(json.dumps({"ok": False, "error": "; ".join(problems)}))
            return 0
        D.boto3_s3(cfg, *keys).head_bucket(Bucket=cfg["Bucket"])
        print(json.dumps({"ok": True}))
    except Exception as e:  # report the class and S3 error code only, never a message that could hold a value
        err = D._s3_error(e) if D is not None else type(e).__name__
        print(json.dumps({"ok": False, "error": f"head_bucket failed: {err}"}))
    return 0


# ── main ─────────────────────────────────────────────────────────────────────

def run_checks(args, env=None, claude_dir: Path | None = None, opener=None) -> Report:
    env = os.environ if env is None else env
    claude_dir = claude_dir or Path.home() / ".claude"
    project = Path(env.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    r = Report()
    uv = check_uv(r)
    check_hook_python(r)
    check_uv_envs(r, uv)
    if getattr(args, "skip_background_check", False):
        r.add("core", FOREGROUND_VAR, "skip", SKIP_BACKGROUND_WHY)
    else:
        check_foreground(r, env)
    check_loaded_once(r, args.plugin_root, claude_dir, project)
    check_catalog(r, args.catalog_dir, env, args.config_catalog_dir)
    check_gitignore(r, project)
    if args.deploy or args.online:
        check_deploy_vars(r, env)
        token = check_token(r, env)
        if args.online:
            check_repo_online(r, env, token, opener)
            check_s3_online(r, uv, env)
    return r


def render(r: Report) -> str:
    label = {"ok": "ok  ", "warn": "warn", "fail": "FAIL", "skip": "skip"}
    w = max((len(x["check"]) for x in r.rows), default=0)
    lines = [f"{label[x['status']]}  {x['group']:6s}  {x['check']:{w}s}  {x['message']}" for x in r.rows]
    n = {s: sum(x["status"] == s for x in r.rows) for s in ("ok", "warn", "fail", "skip")}
    lines.append(f"\n{n['ok']} ok, {n['warn']} warn, {n['fail']} FAIL" + (f", {n['skip']} skipped" if n["skip"] else "")
                 + (" — fix the FAIL lines above, then run doctor again." if n["fail"] else ""))
    return "\n".join(lines)


def main(argv=None) -> int:
    if (argv if argv is not None else sys.argv[1:])[:1] == ["_s3probe"]:
        return s3_probe()
    p = argparse.ArgumentParser(description="Check that curate-skills can run here (read-only).")
    p.add_argument("--deploy", action="store_true", help="also check deploy variables and a GitHub token")
    p.add_argument("--online", action="store_true", help="with --deploy: also read the action repo and "
                   "head_bucket the S3 bucket (implies --deploy)")
    p.add_argument("--plugin-root", help="the plugin folder as loaded (the skill passes ${CLAUDE_PLUGIN_ROOT})")
    p.add_argument("--catalog-dir", help="as /curate --catalog")
    p.add_argument("--config-catalog-dir", help="plugin setting value; empty or unsubstituted = unset")
    p.add_argument("--json", action="store_true", help="print the rows as JSON")
    p.add_argument("--skip-background-check", action="store_true",
                   help=f"for curate-setup only: don't check {FOREGROUND_VAR} (every other check still runs)")
    args = p.parse_args(argv)
    r = run_checks(args)
    print(json.dumps({"ok": not r.failed(), "checks": r.rows}, indent=2) if args.json else render(r))
    return 1 if r.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
