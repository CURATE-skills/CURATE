# /// script
# requires-python = "==3.13.*"
# dependencies = []
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Derived from CURATE-prototype src/faasr_agents/agents/fga.py (_run_stub_test,
# _module_missing_due_to_reported_dep) and src/faasr_agents/faasr/context_dir.py
# (signature_and_test_call) (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140).
# See scripts/VENDORED.md.
"""Stub-test one generated function inside its run directory.

  stubtest.py run --session S --node N [--mode strict|parity] [--timeout 60]

A function that follows the plain contract (plain_contract.py) is called as
main(inputs, outputs, params), per rank, with local paths, the same kinds of values the FaaSr
wrapper and the Pegasus runner give it:
  inputs   each file is looked up in the node's test_output/, then test_data/, then every
           upstream node's test_output/ (nearest first), and COPIED into stub_in/r<rank>/, so
           the function cannot change an upstream node's outputs; a list input gets every
           copy; a folder input gets stub_in/r<rank>/dir_<slot>/ holding every file whose
           name starts with its prefix (the first folder in that order wins)
  outputs  test_output/<file name>, or a list of them
  params   {"rank": r, "max_rank": N} for a ranked node, else {}
A missing input: in strict mode, a file some upstream step writes is an error (a wiring bug);
an external input (no step writes it) is created EMPTY with a warning and listed in
"empty_external_inputs" (Gate 3 shows it). Parity mode creates every missing input empty.

A function that is not plain fails, in both modes (older FaaSr-style catalog code is migrated
with migrate_catalog.py, or regenerated: reuse turns it into an adapt seed).

Modes (default: the run's pinned state.stubtest_mode):
  strict  every rank 1..N; the node's deps.txt installed into an isolated uv env;
          own test_output cleared first and every declared output must be written;
          ModuleNotFoundError fails.
  parity  original CURATE behaviour: rank 1 only, no dependency install, and a
          ModuleNotFoundError counts as a pass when deps.txt declares packages.

Prints JSON {"ok": ...}; exit 0 pass, 1 fail, 2 bad input. Always writes
functions/<node>/stubtest.log (the result JSON, a blank line, then the output).
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import plain_contract as PC  # noqa: E402  (standard library only)

PYTHON = "3.13"
TAIL = 6000
ENV_SYNC_TIMEOUT = 600
# Deployment / provider credentials never reach a stub run. Secrets a function
# reads (os.environ["NAME"]) are passed through so its local test can use them.
SCRUBBED_ENV = ("GH_PAT", "S3_AccessKey", "S3_SecretKey", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "BEDROCK_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                "AWS_BEARER_TOKEN_BEDROCK")
NETWORK_HINTS = ("URLError", "ConnectionError", "ConnectTimeout", "gaierror", "NameResolutionError",
                 "Max retries exceeded", "Temporary failure in name resolution", "HTTPError")


class InputError(Exception):
    pass


def upstream_dirs(spec: dict, node: str, session: Path) -> list[str]:
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
    return [str(session / "functions" / p / "test_output") for p in order]


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


def _env() -> dict:
    scrubbed = {n.upper() for n in SCRUBBED_ENV}  # any spelling: S3_ACCESSKEY too
    env = {k: v for k, v in os.environ.items() if k.upper() not in scrubbed}
    env.setdefault("MPLBACKEND", "Agg")
    return env


# ── the plain harness ────────────────────────────────────────────────────────

NOT_PLAIN = ("The function `{node}` does not follow the plain contract: its file must define "
             "`def main(inputs, outputs, params)` and work only on the local paths it is given "
             "(see the function-reference skill). Problems:\n{problems}")
PLAIN_SCRIPT = """\
import importlib.util, json, sys
call = json.load(open(sys.argv[1]))
spec = importlib.util.spec_from_file_location(call["module"], call["code"])
mod = importlib.util.module_from_spec(spec)
sys.modules[call["module"]] = mod
spec.loader.exec_module(mod)
result = mod.main(call["inputs"], call["outputs"], call["params"])
simple = result is None or isinstance(result, (bool, int, float, str))
print("CURATE_RETURN " + json.dumps(result if simple else repr(result)), flush=True)
"""
RETURN_RX = re.compile(r"^CURATE_RETURN (.*)$", re.MULTILINE)


def _find(name: str, dirs: list[Path]) -> Path | None:
    for d in dirs:
        p = d / name
        if p.is_file():
            return p
    return None


def plain_call(spec: dict, node_name: str, rank: int, node_dir: Path, dirs: list[Path], strict: bool):
    """Stage one rank's inputs and build its call. Returns (call, warnings, empty_external,
    missing): `missing` lists inputs an upstream step should have written (strict mode only)."""
    sp = PC.as_spec(spec)
    layout, _ = PC.io_layout(sp)
    lay = layout[node_name]
    writers = PC.producers(sp)
    ranked = lay["rank"] > 1
    r = rank if ranked else None
    stage = node_dir / "stub_in" / f"r{rank}"
    stage.mkdir(parents=True, exist_ok=True)
    rel = lambda p: os.path.relpath(p, node_dir)  # noqa: E731
    own = [re.compile("^" + re.escape(o["file"]).replace(re.escape(PC.RANK), r"\d+") + "$")
           for o in lay["outputs"]]
    warnings, empty_external, missing = [], [], []

    def one(spec_name: str, fname: str) -> str:
        src = _find(fname, dirs)
        dst = stage / fname
        if src is not None:
            shutil.copyfile(src, dst)
            return rel(dst)
        writer = writers.get(spec_name)
        if strict and writer and writer != node_name:
            missing.append(f"{fname} (written by {writer})")
        else:
            if not writer:
                empty_external.append(fname)
            warnings.append(f"[stub] WARNING: input {fname!r} was found in none of test_output/, test_data/ "
                            f"or the upstream outputs; using an EMPTY stand-in")
        dst.write_text("")
        return rel(dst)

    inputs = {}
    for s in lay["inputs"]:
        if s["kind"] == "one":
            inputs[s["slot"]] = one(s["file"], PC.resolve(s["file"], r))
        elif s["kind"] == "list":
            inputs[s["slot"]] = [one(s["file"], PC.resolve(s["file"], i)) for i in range(1, (s["copies"] or 0) + 1)]
        else:
            d = stage / f"dir_{s['slot']}"
            d.mkdir(exist_ok=True)
            got = set()
            for base in dirs:
                for f in sorted(base.iterdir()) if base.is_dir() else []:
                    if (f.is_file() and f.name.startswith(s["prefix"]) and f.name not in got
                            and not any(rx.match(f.name) for rx in own)):
                        shutil.copyfile(f, d / f.name)
                        got.add(f.name)
            if not got:
                produced = [m for m in s["matches"] if writers.get(m) not in (None, node_name)]
                if strict and produced:
                    missing.append(f"folder {s['slot']}: no file starting with {s['prefix']!r} "
                                   f"(written by {', '.join(sorted({writers[m] for m in produced}))})")
                else:
                    warnings.append(f"[stub] WARNING: folder input {s['slot']} is EMPTY: no file starting with "
                                    f"{s['prefix']!r} in test_output/, test_data/ or the upstream outputs")
                    if not produced:
                        empty_external.append(f"{s['prefix']}* (folder {s['slot']})")
            inputs[s["slot"]] = rel(d)
    outputs = {}
    for s in lay["outputs"]:
        if s["kind"] == "list":
            outputs[s["slot"]] = [f"test_output/{PC.resolve(s['file'], i)}" for i in range(1, (s["copies"] or 0) + 1)]
        else:
            outputs[s["slot"]] = f"test_output/{PC.resolve(s['file'], r)}"
    params = {"rank": rank, "max_rank": lay["rank"]} if ranked else {}
    call = {"module": f"{node_name}_main", "code": f"{node_name}.py",
            "inputs": inputs, "outputs": outputs, "params": params}
    return call, warnings, empty_external, missing


def _run_plain(session: Path, spec: dict, node: dict, node_dir: Path, deps_file: Path, strict: bool,
               timeout: int, result: dict, finish) -> dict:
    name = node["name"]
    upstream = upstream_dirs(spec, name, session)
    dirs = [node_dir / "test_output", node_dir / "test_data"] + [Path(d) for d in upstream]
    cmd = _uv_cmd(deps_file, with_deps=strict)
    max_rank = max(1, int(node.get("rank") or 1))
    ranks = list(range(1, max_rank + 1)) if strict else [1]
    shutil.rmtree(node_dir / "stub_in", ignore_errors=True)
    if strict:
        shutil.rmtree(node_dir / "test_output", ignore_errors=True)
        try:
            sync = subprocess.run(cmd + ["-c", "pass"], cwd=node_dir, capture_output=True, text=True,
                                  timeout=ENV_SYNC_TIMEOUT, env=_env())
        except subprocess.TimeoutExpired:
            return finish(f"Installing dependencies from {deps_file.name} timed out after {ENV_SYNC_TIMEOUT}s")
        if sync.returncode != 0:
            return finish(f"Could not install dependencies from {deps_file.name}:\n" + (sync.stderr or sync.stdout))
    (node_dir / "test_output").mkdir(exist_ok=True)

    outputs, all_ok = [], True
    for r in ranks:
        label = f"rank {r}/{max_rank}" if max_rank > 1 and strict else ("rank 1 (parity)" if not strict else "run")
        call, warns, empty_ext, missing = plain_call(spec, name, r, node_dir, dirs, strict)
        result["empty_external_inputs"] += [f for f in empty_ext if f not in result["empty_external_inputs"]]
        if missing:
            result["missing_inputs"] += missing
            outputs.append(f"=== {label} ===\nInput(s) an upstream step should have written are missing: "
                           + "; ".join(missing) + ". Its stub test did not write them, or the file names in "
                           "the spec don't match.")
            result["ranks"].append({"rank": r, "ok": False})
            all_ok = False
            break
        call_path = node_dir / "stub_in" / f"r{r}" / "call.json"
        call_path.write_text(json.dumps(call, indent=2))
        try:
            proc = subprocess.run(cmd + ["-c", PLAIN_SCRIPT, os.path.relpath(call_path, node_dir)], cwd=node_dir,
                                  capture_output=True, text=True, timeout=timeout, env=_env())
            text = "\n".join(p for p in ["\n".join(warns), proc.stdout, proc.stderr] if p)
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired as exc:
            text, ok = f"Stub test timed out after {timeout}s\n{exc}", False
        m = RETURN_RX.findall(text)
        result["returns"].append(json.loads(m[-1]) if m else None)
        if ok and strict:
            want = [p for v in call["outputs"].values() for p in (v if isinstance(v, list) else [v])]
            not_written = [p.split("/", 1)[1] for p in want if not (node_dir / p).exists()]
            if not_written:
                ok = False
                result["missing_outputs"] += not_written
                text += f"\nDeclared output(s) not written: {not_written}"
        result["ranks"].append({"rank": r, "ok": ok})
        outputs.append(f"=== {label} ===\n{text}")
        if not ok:
            all_ok = False
            break

    tail = "\n".join(outputs)
    if not all_ok and not strict and _declared_deps(deps_file) and "ModuleNotFoundError" in tail:
        result["module_missing_pass"] = True  # original CURATE: an install-time issue
        all_ok = True
    if not all_ok:
        result["network_error_suspected"] = any(h in tail for h in NETWORK_HINTS)
    result["ok"] = all_ok
    return finish(tail)


def run_stubtest(session: Path, node_name: str, mode: str | None = None, timeout: int = 60) -> dict:
    state_path = session / "state.json"
    if not state_path.exists():
        raise InputError(f"{state_path} not found")
    state = json.loads(state_path.read_text())
    mode = mode or state.get("stubtest_mode") or "strict"
    if mode not in ("strict", "parity"):
        raise InputError(f"unknown mode {mode!r}")
    spec = state.get("spec") or {}
    node = next((n for n in spec.get("nodes") or [] if n["name"] == node_name), None)
    if node is None:
        raise InputError(f"node {node_name!r} is not in the session's spec")
    strict = mode == "strict"
    node_dir = session / "functions" / node_name
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
        return finish(f"{impl.relative_to(session)} does not exist")
    code = impl.read_text()
    if not PC.is_plain(code):
        return finish(NOT_PLAIN.format(node=node_name, problems="\n".join(
            f"- {p}" for p in PC.plain_problems(code))))
    result.update(harness="plain", returns=[], empty_external_inputs=[], missing_inputs=[])
    return _run_plain(session, spec, node, node_dir, deps_file, strict, timeout, result, finish)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="stubtest.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("run")
    flag = sp.add_mutually_exclusive_group(required=True)  # as runstate.add_session_flag (no imports here)
    flag.add_argument("--session", dest="session", metavar="SESSION",
                      help="the /curate session: its id (.curate/sessions/<id>) or its folder")
    flag.add_argument("--run", dest="session", help=argparse.SUPPRESS)  # its old name
    sp.add_argument("--node", required=True)
    sp.add_argument("--mode", choices=["strict", "parity"])
    sp.add_argument("--timeout", type=int, default=60)
    args = p.parse_args(argv)
    try:
        result = run_stubtest(Path(args.session).resolve(), args.node, args.mode, args.timeout)
    except (InputError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
