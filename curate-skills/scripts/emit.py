# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7"]
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Derived from CURATE-prototype src/faasr_agents/faasr/emit.py and
# src/faasr_agents/agents/wda.py (_unique_out_dir, _save_artifacts)
# (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). See scripts/VENDORED.md.
"""WorkflowSpec -> FaaSr workflow JSON, validation, and output bundle.

Usage (prints JSON {"ok": ...}; exit 0 ok, 1 check failed, 2 bad input):
  emit.py emit            (--spec FILE | --run DIR)
  emit.py external-inputs (--spec FILE | --run DIR)
  emit.py validate        (--spec FILE | --run DIR | --workflow FILE) [--level auto|faasr_py|structural]
  emit.py bundle          (--spec FILE | --run DIR) [--out DIR] [--level ...]
                          → <out>/<workflow>-<run_id>/ (with --run), never overwriting

Unset FAASR_* variables are emitted as "<FAASR_NAME>" placeholders. Secret
values are never written: only secret names appear in any output, and the
bundle is checked for leaked values of known secret env vars before it is kept.
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

from curate_models import WorkflowSpec, clean_dependencies
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


def emit_faasr_json(spec: WorkflowSpec) -> dict:
    """Convert a WorkflowSpec into a FaaSr workflow JSON dict.

    ActionList keys are kebab-case (FaaSr schema constraint); FunctionName
    values are the original snake_case Python function names.
    """
    action_list = {}
    function_git_repo = {}

    action_name = {node.name: _to_action_name(node.name) for node in spec.nodes}
    node_rank = {node.name: max(1, int(node.rank or 1)) for node in spec.nodes}

    def _invoke_target(to_node: str) -> str:
        # A ranked target is invoked as "name(N)" → N parallel instances.
        target = action_name[to_node]
        n = node_rank.get(to_node, 1)
        return f"{target}({n})" if n > 1 else target

    invoke_next: dict[str, list] = {node.name: [] for node in spec.nodes}
    for edge in spec.edges:
        if edge.condition is None:
            invoke_next[edge.from_node].append(_invoke_target(edge.to_node))
        else:
            existing = invoke_next[edge.from_node]
            cond_entry = next((e for e in existing if isinstance(e, dict)), None)
            if cond_entry is None:
                cond_entry = {}
                existing.append(cond_entry)
            cond_entry.setdefault(edge.condition, []).append(_invoke_target(edge.to_node))

    gh_user = _env("FAASR_GH_USERNAME")
    gh_repo = _env("FAASR_ACTION_REPO")
    default_container = os.environ.get("FAASR_DEFAULT_CONTAINER", "").strip() or DEFAULT_CONTAINER

    for node in spec.nodes:
        aname = action_name[node.name]
        args: dict[str, str] = {"folder": spec.folder}
        for i, inp in enumerate(node.inputs, 1):
            args[f"input{i}"] = inp.name
        for i, out in enumerate(node.outputs, 1):
            args[f"output{i}"] = out.name

        # Always emit InvokeNext (empty list for terminal nodes): FaaSr_py's
        # check_dag reads it unconditionally.
        action_list[aname] = {
            "FaaSServer": spec.compute_target,
            "Type": "Python",
            "FunctionName": node.name,
            "Arguments": args,
            "InvokeNext": invoke_next.get(node.name, []),
        }
        # FaaSr resolves .py paths as "owner/repo/branch/path/to/file.py".
        function_git_repo[node.name] = f"{gh_user}/{gh_repo}/main/functions/{node.name}.py"

    entry_python = spec.entry or (spec.nodes[0].name if spec.nodes else "")
    entry_action = action_name.get(entry_python, _to_action_name(entry_python))

    compute_servers = {
        spec.compute_target: {
            "FaaSType": "GitHubActions",
            "UserName": gh_user,
            "ActionRepoName": gh_repo,
            "Branch": "main",
            "UseSecretStore": True,
        }
    }
    data_stores = {
        spec.data_store: {
            "Endpoint": _env("FAASR_S3_ENDPOINT"),
            "Bucket": _env("FAASR_S3_BUCKET"),
            "Region": _env("FAASR_S3_REGION"),
            # NEVER embed real credentials: FaaSr's convention is to store the
            # secret *names* here; values are injected at runtime from the
            # GitHub Actions secret store.
            "AccessKey": f"{spec.data_store}_AccessKey",
            "SecretKey": f"{spec.data_store}_SecretKey",
            "Writable": "TRUE",
        }
    }

    action_containers = {aname: default_container for aname in action_list}

    pypi_packages = {}
    for node in spec.nodes:
        deps = clean_dependencies(node.dependencies)
        if deps:
            pypi_packages[node.name] = deps

    result: dict = {
        "FunctionInvoke": entry_action,
        "WorkflowName": _sanitize_workflow_name(spec.name),
        "DefaultDataStore": spec.data_store,
        "LoggingDataStore": spec.data_store,
        "FaaSrLog": "FaaSrLog",
        "InvocationIDFromDate": "%Y-%m-%d-%H-%M-%S",
        "ActionList": action_list,
        "ActionContainers": action_containers,
        "ComputeServers": compute_servers,
        "DataStores": data_stores,
        "FunctionGitRepo": function_git_repo,
    }
    if pypi_packages:
        result["PyPIPackageDownloads"] = pypi_packages

    # Workflow-wide secret names = union of per-function secrets.
    secrets = sorted({s for node in spec.nodes for s in node.secrets})
    if secrets:
        result["Secrets"] = secrets
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


# ── bundle (from agents/wda.py) ──────────────────────────────────────────────

def _unique_out_dir(base: Path) -> Path:
    """Return `base` if it's free (missing or empty), else `base-2`, `base-3`, …"""
    candidate = base
    n = 2
    while candidate.is_dir() and any(candidate.iterdir()):
        candidate = Path(f"{base}-{n}")
        n += 1
    return candidate


def _bundle_readme(spec: WorkflowSpec, wf: dict, ext: list[dict], validation: dict,
                   placeholders: list[str]) -> str:
    lines = [f"# {wf['WorkflowName']}", "", spec.description or "", "",
             "Generated by curate-skills. Not deployed.", ""]
    lines += ["## Validation", "",
              f"- level: `{validation['level']}`",
              f"- ok: `{validation['ok']}`"]
    if validation.get("error"):
        lines.append(f"- error: {validation['error']}")
    if validation.get("fallback_reason"):
        lines.append(f"- note: {validation['fallback_reason']}; structural checks only")
    lines += ["", "## Functions", ""]
    for n in spec.nodes:
        rank = f" ×{n.rank}" if n.rank > 1 else ""
        deps = ", ".join(clean_dependencies(n.dependencies)) or "none"
        lines.append(f"- `{n.name}`{rank} ({n.source}) — deps: {deps}")
    lines += ["", "## Secrets to set (names only)", ""]
    lines += [f"- `{s}`" for s in secret_names(spec)]
    lines += ["", "## Files to upload before running", ""]
    lines += [f"- `{e['s3_path']}` — {e['description']}" for e in ext] or ["- none"]
    lines += ["", "## Placeholders to fill in workflow.json", ""]
    lines += [f"- `{_placeholder(v)}` — set `{v}` and re-run bundle" for v in placeholders] or ["- none"]
    return "\n".join(lines) + "\n"


def write_bundle(spec: WorkflowSpec, out_root: Path, level: str = "auto",
                 run_id: str | None = None) -> dict:
    """Write the bundle to a new folder: <out_root>/<workflow>[-<run_id>][-2, -3, …].

    With a run id, two runs of the same workflow never share a folder; an existing,
    non-empty folder is never overwritten (a numbered suffix is added instead).
    """
    wf = emit_faasr_json(spec)
    validation = validate(wf, level)
    ext = external_inputs(spec)
    placeholders = placeholders_used()

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", spec.name)
    if run_id:
        safe_name += "-" + re.sub(r"[^a-zA-Z0-9_-]", "_", run_id)
    out_dir = _unique_out_dir(out_root / safe_name)
    fn_dir = out_dir / "functions"
    fn_dir.mkdir(parents=True, exist_ok=True)

    files: dict[Path, str] = {
        out_dir / "workflow.json": json.dumps(wf, indent=2) + "\n",
        out_dir / "spec.json": spec.model_dump_json(indent=2) + "\n",
        out_dir / "external_inputs.json": json.dumps(ext, indent=2) + "\n",
        out_dir / "README.md": _bundle_readme(spec, wf, ext, validation, placeholders),
    }
    missing_code = []
    for node in spec.nodes:
        if not node.code:
            missing_code.append(node.name)
            continue
        files[fn_dir / f"{node.name}.py"] = (
            f"# Generated by curate-skills\n# Workflow: {spec.name}\n"
            f"# Function: {node.name}\n\n{node.code}"
        )
        deps = clean_dependencies(node.dependencies)
        files[fn_dir / f"{node.name}.deps.txt"] = "".join(f"{d}\n" for d in deps)

    names = secret_names(spec)
    leaked = sorted({n for text in files.values() for n in find_leaked_secrets(text, names)})
    if leaked:
        shutil.rmtree(out_dir, ignore_errors=True)
        return {"ok": False, "error": "secret values found in bundle content; nothing written",
                "leaked_secret_names": leaked}

    for path, text in files.items():
        path.write_text(text)
    return {
        "ok": validation["ok"] and not missing_code,
        "bundle_dir": str(out_dir),
        "validation": validation,
        "missing_code": missing_code,
        "placeholders": placeholders,
        "external_inputs": ext,
        "secret_names": names,
    }


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
    for name in ("emit", "external-inputs", "validate", "bundle"):
        sp = sub.add_parser(name)
        src = sp.add_mutually_exclusive_group()
        src.add_argument("--spec", help="WorkflowSpec JSON file")
        src.add_argument("--run", help="run dir containing state.json")
        if name == "validate":
            src.add_argument("--workflow", help="already-emitted FaaSr workflow JSON")
        if name in ("validate", "bundle"):
            sp.add_argument("--level", choices=["auto", "faasr_py", "structural"], default="auto")
        if name == "bundle":
            sp.add_argument("--out", default="faasr_output")
    args = p.parse_args(argv)

    try:
        if args.cmd == "validate" and args.workflow:
            wf = json.loads(Path(args.workflow).read_text())
            return _print(validate(wf, args.level), BASE_SECRET_NAMES)
        spec = load_spec(args)
    except (InputError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    names = secret_names(spec)

    if args.cmd == "emit":
        return _print({"ok": True, "workflow": emit_faasr_json(spec),
                       "placeholders": placeholders_used()}, names)
    if args.cmd == "external-inputs":
        return _print({"ok": True, "external_inputs": external_inputs(spec)}, names)
    if args.cmd == "validate":
        result = validate(emit_faasr_json(spec), args.level)
        update_state(args.run, validation={**{k: result[k] for k in ("ok", "level", "error")},
                                           "spec_hash": spec_hash(spec.model_dump(mode="json"))})
        return _print(result, names)
    run_id = json.loads((Path(args.run) / "state.json").read_text()).get("run_id") if args.run else None
    result = write_bundle(spec, Path(args.out), args.level, run_id=run_id)
    if "bundle_dir" in result:
        update_state(args.run, bundle_dir=result["bundle_dir"], stage="gate4",
                     validation={**{k: result["validation"][k] for k in ("ok", "level", "error")},
                                 "spec_hash": spec_hash(spec.model_dump(mode="json"))})
    return _print(result, names)


if __name__ == "__main__":
    sys.exit(main())
