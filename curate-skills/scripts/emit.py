# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "cwl-utils==0.47", "pyyaml==6.0.2"]
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Derived from CURATE-prototype src/faasr_agents/faasr/emit.py
# (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). See scripts/VENDORED.md.
"""The workflow draft for Gate 4, FaaSr workflow JSON validation, and the FaaSr document helpers.

Usage (prints JSON {"ok": ...}; exit 0 ok, 1 check failed, 2 bad input):
  emit.py bundle          --run DIR [--level auto|faasr_py|structural]
                          → <run>/draft/ (workflow.yml, inputs.yml, functions/), from the spec by
                          spec_cwl; validated with what both builders make of it
  emit.py external-inputs (--spec FILE | --run DIR)   the files the spec reads but no step writes
  emit.py validate        --workflow FILE [--level ...]  a FaaSr workflow JSON (e.g. a bundle's)

The FaaSr bundle itself is built from the approved version after Gate 4 (scripts/cwl_faasr.py,
which uses the document helpers here). Unset FAASR_* variables are written as "<FAASR_NAME>"
placeholders. Secret values are never written: only secret names appear in any output.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from pydantic import ValidationError

import faasr_wrapper
from curate_models import WorkflowSpec
from runstate import run_lock, spec_hash, write_json_atomic

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONTAINER = "ghcr.io/faasr/github-actions-python:latest"
# Deployment-identity settings that have no safe default: emitted as placeholders.
PLACEHOLDER_VARS = (
    "FAASR_GH_USERNAME",
    "FAASR_ACTION_REPO",
    "FAASR_S3_ENDPOINT",
    "FAASR_S3_BUCKET",
    "FAASR_S3_REGION",
)
# Credentials the FaaSr deployment reads from secret stores. Values must never
# appear in emitted files.
BASE_SECRET_NAMES = ("GH_PAT", "S3_AccessKey", "S3_SecretKey")
_ACTION_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9-]*$")


def _placeholder(var: str) -> str:
    return f"<{var}>"


def _env(var: str) -> str:
    return os.environ.get(var, "").strip() or _placeholder(var)


def placeholders_used() -> list[str]:
    return [v for v in PLACEHOLDER_VARS if not os.environ.get(v, "").strip()]


# ── vendored from faasr/emit.py ──────────────────────────────────────────────

def _to_action_name(python_name: str) -> str:
    """Convert snake_case Python name to kebab-case FaaSr action name.
    FaaSr ActionList keys must match ^[a-zA-Z][a-zA-Z0-9-]*$
    """
    kebab = python_name.replace("_", "-")
    kebab = re.sub(r'[^a-zA-Z0-9-]', '-', kebab)
    kebab = re.sub(r'-+', '-', kebab).strip('-')
    if not kebab or not kebab[0].isalpha():
        kebab = 'fn-' + kebab
    return kebab


def _sanitize_workflow_name(name: str) -> str:
    """FaaSr WorkflowName must match ^[a-zA-Z][a-zA-Z0-9-]*$"""
    sanitized = re.sub(r'[^a-zA-Z0-9-]', '-', name)
    sanitized = re.sub(r'-+', '-', sanitized).strip('-')
    if not sanitized or not sanitized[0].isalpha():
        sanitized = 'workflow-' + sanitized
    return sanitized


def external_inputs(spec: WorkflowSpec) -> list[dict]:
    """Inputs that no node produces — files the user must place in S3 beforehand.

    Returns one entry per distinct external file:
    {file, node, s3_path, type, description}. The description prefers the input's
    own IOSpec description and falls back to the consuming node's description.
    """
    # Match produced files in a {rank}-tolerant way: a templated input
    # (shuffle_shard_{rank}.json) must be recognised as produced even when the
    # producer records concrete shard names (shuffle_shard_1.json, ...) and
    # vice-versa. Only {rank} is widened to \d+.
    def _base(n: str) -> str:
        return n.rsplit("/", 1)[-1]

    def _rank_pattern(base: str) -> re.Pattern:
        return re.compile("^" + re.escape(base).replace(r"\{rank\}", r"\d+") + "$")

    # A file counts as produced — and so NOT external — only when some node OTHER
    # than the consumer outputs it (validate-and-forward nodes don't mask inputs).
    produced_by: dict[str, set[str]] = {}
    patterns_by: list[tuple[re.Pattern, str]] = []
    for node in spec.nodes:
        for o in node.outputs:
            b = _base(o.name)
            produced_by.setdefault(b, set()).add(node.name)
            if "{rank}" in b:
                patterns_by.append((_rank_pattern(b), node.name))

    def _produced_by_other(name: str, consumer: str) -> bool:
        b = _base(name)
        if any(n != consumer for n in produced_by.get(b, ())):
            return True
        if any(prod != consumer and pat.match(b) for pat, prod in patterns_by):
            return True
        if "{rank}" in b:
            pat = _rank_pattern(b)
            if any(
                prod != consumer and pat.match(lit)
                for lit, prods in produced_by.items()
                for prod in prods
            ):
                return True
        return False

    out: list[dict] = []
    seen: set[str] = set()
    for node in spec.nodes:
        for inp in node.inputs:
            if not _produced_by_other(inp.name, node.name) and inp.name not in seen:
                seen.add(inp.name)
                desc = (inp.description or "").strip() or (node.description or "").strip()
                out.append({
                    "file": inp.name,
                    "node": node.name,
                    "s3_path": f"{spec.folder}/{inp.name}" if spec.folder else inp.name,
                    "type": (inp.type or "").strip(),
                    "description": desc,
                })
    return out


def faasr_action(compute_target: str, function_name: str, arguments: dict, invoke_next: list) -> dict:
    return {"FaaSServer": compute_target, "Type": "Python", "FunctionName": function_name,
            "Arguments": arguments, "InvokeNext": invoke_next}


def function_repo_paths(fn: str) -> list[str]:
    """FaaSr resolves .py paths as "owner/repo/branch/path/to/file.py". Every step ships its
    FaaSr wrapper and its unchanged function (faasr_wrapper.py); FaaSr fetches both."""
    repo_path = f"{_env('FAASR_GH_USERNAME')}/{_env('FAASR_ACTION_REPO')}/main/functions/"
    return [repo_path + f"{fn}.py", repo_path + faasr_wrapper.main_file(fn)]


def faasr_document(name: str, compute_target: str, data_store: str, action_list: dict,
                   function_git_repo: dict, entry_action: str, pypi_packages: dict, secrets: list) -> dict:
    """The FaaSr workflow JSON around an ActionList: servers, data store, containers (for the CWL
    FaaSr builder, scripts/cwl_faasr.py)."""
    gh_user = _env("FAASR_GH_USERNAME")
    gh_repo = _env("FAASR_ACTION_REPO")
    default_container = os.environ.get("FAASR_DEFAULT_CONTAINER", "").strip() or DEFAULT_CONTAINER
    compute_servers = {
        compute_target: {
            "FaaSType": "GitHubActions",
            "UserName": gh_user,
            "ActionRepoName": gh_repo,
            "Branch": "main",
            "UseSecretStore": True,
        }
    }
    data_stores = {
        data_store: {
            "Endpoint": _env("FAASR_S3_ENDPOINT"),
            "Bucket": _env("FAASR_S3_BUCKET"),
            "Region": _env("FAASR_S3_REGION"),
            # NEVER embed real credentials: FaaSr's convention is to store the
            # secret *names* here; values are injected at runtime from the
            # GitHub Actions secret store.
            "AccessKey": f"{data_store}_AccessKey",
            "SecretKey": f"{data_store}_SecretKey",
            "Writable": "TRUE",
        }
    }
    result: dict = {
        "FunctionInvoke": entry_action,
        "WorkflowName": _sanitize_workflow_name(name),
        "DefaultDataStore": data_store,
        "LoggingDataStore": data_store,
        "FaaSrLog": "FaaSrLog",
        "InvocationIDFromDate": "%Y-%m-%d-%H-%M-%S",
        "ActionList": action_list,
        "ActionContainers": {aname: default_container for aname in action_list},
        "ComputeServers": compute_servers,
        "DataStores": data_stores,
        "FunctionGitRepo": function_git_repo,
    }
    if pypi_packages:
        result["PyPIPackageDownloads"] = pypi_packages
    if secrets:
        result["Secrets"] = list(secrets)
    return result


# ── validation ───────────────────────────────────────────────────────────────

def _strip_rank(target: str) -> str:
    return re.sub(r"\(\d+\)$", "", target)


def _targets(invoke_next) -> list[str]:
    out: list[str] = []
    for item in invoke_next or []:
        if isinstance(item, str):
            out.append(_strip_rank(item))
        elif isinstance(item, dict):
            for branch in item.values():
                out.extend(_strip_rank(t) for t in branch or [])
    return out


def structural_validate(wf: dict) -> tuple[bool, str | None]:
    """Fallback checks used when FaaSr_py's validator cannot run.

    Covers naming, references, acyclicity and reachability — not the full
    FaaSr JSON schema.
    """
    errors: list[str] = []
    actions = wf.get("ActionList") or {}
    if not actions:
        return False, "ActionList is empty"
    name = wf.get("WorkflowName", "")
    if not _ACTION_NAME_RE.match(name or ""):
        errors.append(f"WorkflowName {name!r} must match ^[a-zA-Z][a-zA-Z0-9-]*$")
    for a in actions:
        if not _ACTION_NAME_RE.match(a):
            errors.append(f"ActionList key {a!r} must match ^[a-zA-Z][a-zA-Z0-9-]*$")
    entry = wf.get("FunctionInvoke")
    if entry not in actions:
        errors.append("FunctionInvoke does not refer to a valid function")

    servers = wf.get("ComputeServers") or {}
    stores = wf.get("DataStores") or {}
    containers = wf.get("ActionContainers") or {}
    repos = wf.get("FunctionGitRepo") or {}
    for a, spec in actions.items():
        if spec.get("FaaSServer") not in servers:
            errors.append(f"{a}: FaaSServer {spec.get('FaaSServer')!r} not in ComputeServers")
        if a not in containers:
            errors.append(f"{a}: missing ActionContainers entry")
        if spec.get("FunctionName") not in repos:
            errors.append(f"{a}: FunctionName {spec.get('FunctionName')!r} not in FunctionGitRepo")
        for t in _targets(spec.get("InvokeNext")):
            if t not in actions:
                errors.append(f"{a}: InvokeNext target {t!r} is not an action")
    for key in ("DefaultDataStore", "LoggingDataStore"):
        if wf.get(key) not in stores:
            errors.append(f"{key} {wf.get(key)!r} not in DataStores")
    if errors:
        return False, "; ".join(errors)

    # Cycle + reachability from the entry.
    graph = {a: _targets(s.get("InvokeNext")) for a, s in actions.items()}
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        state[n] = 1
        for m in graph[n]:
            if state.get(m) == 1 or (m not in state and not visit(m)):
                return False
        state[n] = 2
        return True

    if not visit(entry):
        return False, "workflow contains a cycle"
    unreachable = sorted(set(actions) - set(state))
    if unreachable:
        return False, f"actions unreachable from {entry}: {unreachable}"
    return True, None


def faasr_py_validate(wf: dict, timeout: int = 300) -> tuple[bool, str | None] | None:
    """Run faasr_validate.py in its own uv env. None means it could not run."""
    uv = shutil.which("uv")
    if not uv:
        return None
    try:
        proc = subprocess.run(
            [uv, "run", "--quiet", str(SCRIPT_DIR / "faasr_validate.py")],
            input=json.dumps(wf), capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        result = json.loads(proc.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return None
    if result.get("unavailable"):
        return None
    return bool(result.get("ok")), result.get("error")


def validate(wf: dict, level: str = "auto") -> dict:
    """Return {"ok", "level", "error", "fallback_reason"}."""
    fallback_reason = None
    if level in ("auto", "faasr_py"):
        res = faasr_py_validate(wf)
        if res is not None:
            return {"ok": res[0], "level": "faasr_py", "error": res[1], "fallback_reason": None}
        if level == "faasr_py":
            return {"ok": False, "level": "faasr_py", "error": "FaaSr_py validator could not run",
                    "fallback_reason": None}
        fallback_reason = "FaaSr_py validator could not run (uv missing, install failed, or timeout)"
    ok, err = structural_validate(wf)
    return {"ok": ok, "level": "structural", "error": err, "fallback_reason": fallback_reason}


# ── secrets guard ────────────────────────────────────────────────────────────

def secret_names(spec: WorkflowSpec) -> list[str]:
    names = set(BASE_SECRET_NAMES)
    names.update({f"{spec.data_store}_AccessKey", f"{spec.data_store}_SecretKey"})
    names.update(s for n in spec.nodes for s in n.secrets)
    return sorted(names)


def find_leaked_secrets(text: str, names) -> list[str]:
    """Names of env secrets whose *values* occur in text. Values never returned."""
    # Matched without regard to case: S3_ACCESSKEY (GitHub's spelling) is S3_AccessKey.
    want = {n.upper() for n in names}
    return [k for k, v in sorted(os.environ.items())
            if k.upper() in want and len(v) >= 6 and v in text]


# ── CLI ──────────────────────────────────────────────────────────────────────

class InputError(Exception):
    pass


def load_spec(args) -> WorkflowSpec:
    if args.spec:
        path = Path(args.spec)
        data = json.loads(path.read_text())
    elif args.run:
        path = Path(args.run) / "state.json"
        data = json.loads(path.read_text()).get("spec")
        if not data:
            raise InputError(f"{path} has no 'spec' yet")
    else:
        raise InputError("pass --spec FILE or --run DIR")
    try:
        spec = WorkflowSpec.model_validate(data)
    except ValidationError as e:
        raise InputError(f"{path}: invalid WorkflowSpec:\n{e}") from e
    names = {n.name for n in spec.nodes}
    bad = [f"{e.from_node}->{e.to_node}" for e in spec.edges
           if e.from_node not in names or e.to_node not in names]
    if bad:
        raise InputError(f"edges reference unknown nodes: {bad}")
    return spec


def update_state(run: str | None, **fields) -> None:
    if not run:
        return
    path = Path(run) / "state.json"
    with run_lock(run):
        state = json.loads(path.read_text())
        state.update(fields)
        write_json_atomic(path, state)


def _print(obj: dict, names=()) -> int:
    text = json.dumps(obj, indent=2)
    leaked = find_leaked_secrets(text, names)
    if leaked:
        text = json.dumps({"ok": False, "error": "output withheld: contains secret values",
                           "leaked_secret_names": leaked})
        print(text)
        return 1
    print(text)
    return 0 if obj.get("ok") else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="emit.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bundle", help="write the run's workflow draft for Gate 4")
    b.add_argument("--run", required=True, help="run dir containing state.json")
    b.add_argument("--level", choices=["auto", "faasr_py", "structural"], default="auto")
    e = sub.add_parser("external-inputs")
    src = e.add_mutually_exclusive_group(required=True)
    src.add_argument("--spec", help="WorkflowSpec JSON file")
    src.add_argument("--run", help="run dir containing state.json")
    v = sub.add_parser("validate", help="validate a FaaSr workflow JSON")
    v.add_argument("--workflow", required=True, help="a FaaSr workflow JSON file")
    v.add_argument("--level", choices=["auto", "faasr_py", "structural"], default="auto")
    args = p.parse_args(argv)

    try:
        if args.cmd == "validate":
            wf = json.loads(Path(args.workflow).read_text())
            return _print(validate(wf, args.level), BASE_SECRET_NAMES)
        if args.cmd == "external-inputs":
            spec = load_spec(args)
            return _print({"ok": True, "external_inputs": external_inputs(spec)}, secret_names(spec))
        args.spec = None
        spec = load_spec(args)
    except (InputError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    # Gate 4 approves the workflow draft, validated with what the builders make of it.
    draft = write_draft(spec, Path(args.run), args.level)
    sh = spec_hash(spec.model_dump(mode="json"))
    fields = {"draft_dir": draft.get("dir"), "draft_problems": draft.get("problems") or None,
              "draft_spec_hash": sh if draft["ok"] else None,
              "validation": {**draft["validation"], "spec_hash": sh}}
    if draft["ok"]:
        fields["stage"] = "gate4"
    update_state(args.run, **fields)
    return _print({"ok": bool(draft["ok"] and draft["validation"]["ok"]), "draft": draft,
                   "validation": draft["validation"]}, secret_names(spec))


def write_draft(spec: WorkflowSpec, run: Path, level: str = "auto") -> dict:
    """The workflow's draft for Gate 4: <run>/draft/ (workflow.yml, inputs.yml, functions/), built from
    the spec by spec_cwl. An earlier, unapproved draft is replaced; Gate 4 approves the draft by its
    tree hash, and only then is it published as a version (ids.publish).

    `validation` is what Gate 4 checks: the draft passes the CWL validator, and the FaaSr workflow
    the builder makes of it (in memory) passes the same workflow.json validation emit's bundle had.
    The Pegasus builder runs in memory too, and its result is recorded next to FaaSr's. A target
    that refuses the draft does not fail validation (the other target, or no deploy, may still
    take it); Gate 4 shows both results, and approving a refused target is refused."""
    import ids
    import spec_cwl
    target = ids.draft_dir(run)
    failed = {"ok": False, "level": "cwl", "error": None, "source": "draft"}
    try:
        built = spec_cwl.build(spec)
    except spec_cwl.BuildError as e:
        return {"ok": False, "dir": None, "problems": e.problems,
                "validation": {**failed, "error": "; ".join(e.problems)}}
    texts = [json.dumps(built["workflow"]), json.dumps(built["inputs"]), *built["functions"].values()]
    leaked = sorted({n for x in texts for n in find_leaked_secrets(x, secret_names(spec))})
    if leaked:  # nothing is written
        problems = [f"secret values found in the workflow draft ({', '.join(leaked)}); nothing written"]
        return {"ok": False, "dir": None, "problems": problems, "leaked_secret_names": leaked,
                "validation": {**failed, "error": problems[0]}}
    if target.exists():
        shutil.rmtree(target)
    spec_cwl.write(built, target)
    return {"ok": True, "dir": str(target), "hash": ids.tree_hash(target), "problems": [],
            "validation": validate_draft(spec, target, level)}


def validate_draft(spec: WorkflowSpec, draft: Path, level: str = "auto") -> dict:
    import cwl_faasr
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "curate_ir"))
    from cwl.validate import validate as cwl_validate
    wf, errors, _ = cwl_validate(draft / "workflow.yml", draft / "inputs.yml")
    if errors:
        return {"ok": False, "level": "cwl", "error": "; ".join(errors), "source": "draft"}
    import cwl_pegasus
    try:
        built = cwl_faasr.build(wf, spec.name, spec.folder, spec.compute_target, spec.data_store, level)
        faasr = {k: built["validation"][k] for k in ("ok", "level", "error")}
        faasr["problems"] = [] if faasr["ok"] else [faasr["error"]]
    except cwl_faasr.BuildError as e:
        faasr = {"ok": False, "level": None, "error": "; ".join(e.problems), "problems": e.problems}
    try:
        cwl_pegasus.build(wf, "validate")
        pegasus = {"ok": True, "error": None, "problems": []}
    except cwl_pegasus.BuildError as e:
        pegasus = {"ok": False, "error": "; ".join(e.problems), "problems": e.problems}

    def word(t):
        return (f"passed ({t['level']})" if t.get("level") else "passed") if t["ok"] else f"refused ({t['error']})"
    return {"ok": True, "level": f"cwl: passed, faasr: {word(faasr)}, pegasus: {word(pegasus)}", "error": None,
            "source": "draft", "faasr": faasr, "pegasus": pegasus}


if __name__ == "__main__":
    sys.exit(main())
