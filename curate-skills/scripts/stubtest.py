# /// script
# requires-python = "==3.13.*"
# dependencies = []
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Derived from CURATE-prototype src/faasr_agents/agents/fga.py (_run_stub_test,
# _module_missing_due_to_reported_dep) and src/faasr_agents/faasr/context_dir.py
# (signature_and_test_call) (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140).
# See scripts/VENDORED.md.
"""Stub-test one generated FaaSr function inside its run directory.

  stubtest.py run --run R --node N [--mode strict|parity] [--timeout 60]

Modes (default: the run's pinned state.stubtest_mode):
  strict  every rank 1..N; the node's deps.txt installed into an isolated uv env;
          no faasr imports allowed; own test_output cleared first and every
          declared output must be uploaded; ModuleNotFoundError fails.
  parity  original CURATE behaviour: rank 1 only, no dependency install, and a
          ModuleNotFoundError counts as a pass when deps.txt declares packages.

Prints JSON {"ok": ...}; exit 0 pass, 1 fail, 2 bad input. Always writes
functions/<node>/stubtest.log.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PYTHON = "3.13"
TAIL = 6000
ENV_SYNC_TIMEOUT = 600
# Deployment / provider credentials never reach a stub run. Secrets a function
# declares (faasr_secret) are passed through so its local test can use them.
SCRUBBED_ENV = ("GH_PAT", "S3_AccessKey", "S3_SecretKey", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "BEDROCK_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "AWS_BEARER_TOKEN_BEDROCK")
FAASR_IMPORT = re.compile(r"^\s*(import\s+(faasr|FaaSr_py|faasr_stubs)\b|from\s+(faasr|FaaSr_py|faasr_stubs)\b)",
                          re.MULTILINE)
NETWORK_HINTS = ("URLError", "ConnectionError", "ConnectTimeout", "gaierror", "NameResolutionError",
                 "Max retries exceeded", "Temporary failure in name resolution", "HTTPError")


class InputError(Exception):
    pass


def signature_and_test_call(node: dict) -> tuple[str, str]:
    ins, outs = node.get("inputs") or [], node.get("outputs") or []
    sig = (["folder: str"] + [f"input{i + 1}: str" for i in range(len(ins))]
           + [f"output{i + 1}: str" for i in range(len(outs))])
    signature = f"def {node['name']}({', '.join(sig)}) -> None"
    args = ["'test_folder'"] + [f"'{i['name']}'" for i in ins] + [f"'{o['name']}'" for o in outs]
    return signature, f"{node['name']}({', '.join(args)})"


def upstream_dirs(spec: dict, node: str, run: Path) -> list[str]:
    """Ancestors' test_output dirs, nearest first (BFS over reverse edges)."""
    parents: dict[str, list[str]] = {}
    for e in spec.get("edges") or []:
        parents.setdefault(e["to_node"], []).append(e["from_node"])
    order, seen, frontier = [], {node}, [node]
    while frontier:
        nxt = []
        for n in frontier:
            for p in parents.get(n, []):
                if p not in seen:
                    seen.add(p)
                    order.append(p)
                    nxt.append(p)
        frontier = nxt
    return [str(run / "functions" / p / "test_output") for p in order]


def ast_check(code: str, node: dict, strict: bool) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"Generated function is not valid Python: {exc}"
    outs = node.get("outputs") or []
    if outs:
        uploads = sum(1 for item in ast.walk(tree)
                      if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
                      and item.func.id == "faasr_put_file")
        if uploads < len(outs):
            return (f"The function declares {len(outs)} output file(s), but contains only {uploads} "
                    "faasr_put_file call(s). Write each output locally and upload it with "
                    "faasr_put_file so it survives the serverless invocation.")
    if strict:
        if FAASR_IMPORT.search(code):
            return ("The function imports faasr / FaaSr_py / faasr_stubs. The FaaSr runtime injects "
                    "faasr_get_file etc. as globals; remove every faasr import.")
        if not any(isinstance(n, ast.FunctionDef) and n.name == node["name"] for n in tree.body):
            return f"No top-level `def {node['name']}(...)` found."
    return None


def _declared_deps(deps_file: Path) -> list[str]:
    if not deps_file.exists():
        return []
    return [ln.strip() for ln in deps_file.read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _uv_cmd(deps_file: Path, with_deps: bool) -> list[str]:
    uv = shutil.which("uv")
    if not uv:
        raise InputError("uv is not on PATH")
    cmd = [uv, "run", "--no-project", "--quiet", "--python", PYTHON]
    if with_deps and _declared_deps(deps_file):
        cmd += ["--with-requirements", str(deps_file)]
    return cmd + ["python"]


def _env(rank: int | None, max_rank: int | None, upstream: list[str]) -> dict:
    scrubbed = {n.upper() for n in SCRUBBED_ENV}  # any spelling: S3_ACCESSKEY too
    env = {k: v for k, v in os.environ.items() if k.upper() not in scrubbed}
    env.pop("FAASR_RANK", None)
    env.pop("FAASR_MAX_RANK", None)
    if rank is not None:
        env["FAASR_RANK"] = str(rank)
        env["FAASR_MAX_RANK"] = str(max_rank)
    env["CURATE_UPSTREAM_DIRS"] = os.pathsep.join(upstream)
    env.setdefault("MPLBACKEND", "Agg")
    return env


def _expected_outputs(node: dict, rank: int) -> list[str]:
    return [o["name"].replace("{rank}", str(rank)) for o in node.get("outputs") or []]


def run_stubtest(run: Path, node_name: str, mode: str | None = None, timeout: int = 60) -> dict:
    state_path = run / "state.json"
    if not state_path.exists():
        raise InputError(f"{state_path} not found")
    state = json.loads(state_path.read_text())
    mode = mode or state.get("stubtest_mode") or "strict"
    if mode not in ("strict", "parity"):
        raise InputError(f"unknown mode {mode!r}")
    spec = state.get("spec") or {}
    node = next((n for n in spec.get("nodes") or [] if n["name"] == node_name), None)
    if node is None:
        raise InputError(f"node {node_name!r} is not in the run's spec")
    strict = mode == "strict"
    node_dir = run / "functions" / node_name
    impl = node_dir / f"{node_name}.py"
    deps_file = node_dir / f"{node_name}.deps.txt"
    log_path = node_dir / "stubtest.log"
    result = {"ok": False, "node": node_name, "mode": mode, "ranks": [], "module_missing_pass": False,
              "network_error_suspected": False, "missing_outputs": [], "tail": ""}

    def finish(tail: str) -> dict:
        result["tail"] = tail[-TAIL:]
        node_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps({k: v for k, v in result.items() if k != "tail"}, indent=2)
                            + "\n\n" + tail + "\n")
        return result

    if not impl.exists():
        return finish(f"{impl.relative_to(run)} does not exist")
    problem = ast_check(impl.read_text(), node, strict)
    if problem:
        return finish(problem)

    stubs = run / "stubs"
    _, test_call = signature_and_test_call(node)
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(stubs)!r})\n"
        "from faasr_stubs import faasr_get_file, faasr_put_file, faasr_log, "
        "faasr_secret, faasr_rank, faasr_get_folder_list\n"
        f"exec(compile(open({impl.name!r}).read(), {impl.name!r}, 'exec'))\n"
        f"{test_call}\n"
    )
    upstream = upstream_dirs(spec, node_name, run)
    cmd = _uv_cmd(deps_file, with_deps=strict)
    max_rank = max(1, int(node.get("rank") or 1))
    ranks = list(range(1, max_rank + 1)) if strict else [None]
    if strict:
        shutil.rmtree(node_dir / "test_output", ignore_errors=True)
        (node_dir / "test_output").mkdir()
        # Sync the uv env (dependency install) outside the per-rank time limit.
        try:
            sync = subprocess.run(cmd + ["-c", "pass"], cwd=node_dir, capture_output=True, text=True,
                                  timeout=ENV_SYNC_TIMEOUT, env=_env(None, None, upstream))
        except subprocess.TimeoutExpired:
            return finish(f"Installing dependencies from {deps_file.name} timed out "
                          f"after {ENV_SYNC_TIMEOUT}s")
        if sync.returncode != 0:
            return finish(f"Could not install dependencies from {deps_file.name}:\n"
                          + (sync.stderr or sync.stdout))

    outputs = []
    all_ok = True
    for r in ranks:
        label = f"rank {r}/{max_rank}" if r is not None else "rank 1 (parity)"
        try:
            proc = subprocess.run(cmd + ["-c", script], cwd=node_dir, capture_output=True, text=True,
                                  timeout=timeout, env=_env(r, max_rank if r else None, upstream))
            text = "\n".join(p for p in (proc.stdout, proc.stderr) if p)
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired as exc:
            text, ok = f"Stub test timed out after {timeout}s\n{exc}", False
        if ok and strict:
            missing = [f for f in _expected_outputs(node, r)
                       if not (node_dir / "test_output" / f).exists()]
            if missing:
                ok = False
                result["missing_outputs"] += missing
                text += f"\nDeclared output(s) not uploaded with faasr_put_file: {missing}"
        result["ranks"].append({"rank": r or 1, "ok": ok})
        outputs.append(f"=== {label} ===\n{text}")
        if not ok:
            all_ok = False
            break

    tail = "\n".join(outputs)
    if not all_ok and not strict and _declared_deps(deps_file) and "ModuleNotFoundError" in tail:
        # Original CURATE: a missing optional generated dep is an install-time issue.
        result["module_missing_pass"] = True
        all_ok = True
    if not all_ok:
        result["network_error_suspected"] = any(h in tail for h in NETWORK_HINTS)
    result["ok"] = all_ok
    return finish(tail)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="stubtest.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("run")
    sp.add_argument("--run", required=True)
    sp.add_argument("--node", required=True)
    sp.add_argument("--mode", choices=["strict", "parity"])
    sp.add_argument("--timeout", type=int, default=60)
    args = p.parse_args(argv)
    try:
        result = run_stubtest(Path(args.run).resolve(), args.node, args.mode, args.timeout)
    except (InputError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
