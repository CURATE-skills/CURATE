# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "rank-bm25==0.2.2"]
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Partly derived from CURATE-prototype src/faasr_agents/agents/wca.py,
# agents/fca.py and faasr/context_dir.py (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140):
# the functions marked in their section headers are adapted copies. The rest is
# new code for curate-skills. See scripts/VENDORED.md ("Ported and new code").
"""Run state and workflow-spec operations for the /curate gate loop.

State lives in <project>/.curate/runs/<run_id>/state.json. Claude writes
work/skeleton.json, work/candidates.json and work/resolved.json; this script
validates and merges them. Every command prints JSON {"ok": ...};
exit 0 ok, 1 check failed / needs attention, 2 bad input.

  spec.py init --request-file F [--user-model P]... [--context-file P]...
               [--catalog-dir D] [--config-catalog-dir D]
               [--mga-retries N] [--config-mga-retries N]
               [--stubtest-mode strict|parity] [--config-stubtest-mode M]
               [--project-dir D]
  spec.py runs [--project-dir D]
  spec.py preflight
  spec.py status          --run R
  spec.py prompt-context  --run R --stage skeleton|mca|resolve [--k 5]
  spec.py skeleton        --run R --json work/skeleton.json
  spec.py apply-candidates --run R --json work/candidates.json
  spec.py merge-resolved  --run R --json work/resolved.json
  spec.py check           --run R
  spec.py toposort        --run R
  spec.py approve         --run R --gate 1|2|3|4
  spec.py directive       --run R --gate 1|2|3|4|emit --text T [--structural true|false]
  spec.py set-cap         --run R --max N
  spec.py set-stage       --run R --stage S        (low level: stop, tests)
  spec.py import          --stage S --from FILE [--candidates F] [--functions DIR]
                          [--existing-code keep|rewrite|ignore] [--review] [init flags]
  spec.py start           --at S (--from-run ID | --from-bundle DIR | --from-file F | --sketch-file F)
                          [--existing-code …] [--dry-run] [init flags]   (new run; source untouched)
  spec.py next            --run R
  spec.py require         --run R --stage S
  spec.py begin           --run R --stage S [--rewind --confirmed]
  spec.py end             --run R --stage generate
  spec.py show            --run R --gate 1|2|3|4
  spec.py summary         --run R
"""
from __future__ import annotations

import argparse
import ast
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import sys
from collections import deque
from pathlib import Path
from typing import Optional

from pydantic import ValidationError

import catalog as catalog_mod
from curate_models import (
    ContextFile,
    DataFlowEdge,
    FunctionSpec,
    IOSpec,
    UserModel,
    WorkflowEdge,
    WorkflowSpec,
    clean_dependencies,
)
from emit import external_inputs
from runstate import node_basis, obj_hash, run_lock, spec_hash, write_json_atomic
from deploy import tree_hash

SCHEMA_VERSION = 2
DEFAULT_MGA_RETRIES = 3
DEFAULT_STUBTEST_MODE = "strict"
STUBTEST_MODES = ("strict", "parity")
DEFAULT_MAX_ITERATIONS = 5
STAGES = ("skeleton", "gate1", "reuse", "resolve", "gate2", "generate", "gate3",
          "emit", "gate4", "deploy", "monitor", "finalize", "done", "stopped")
# Stage skills, in pipeline order, with the run `stage` values each one owns.
PIPELINE = (
    ("skeleton", ("skeleton", "gate1"), "curate-skeleton"),
    ("reuse", ("reuse",), "curate-reuse"),
    ("resolve", ("resolve", "gate2"), "curate-resolve"),
    ("generate", ("generate", "gate3"), "curate-generate"),
    ("emit", ("emit", "gate4"), "curate-emit"),
    ("deploy", ("deploy",), "curate-deploy"),
    ("monitor", ("monitor",), "curate-monitor"),
    ("finalize", ("finalize",), "wda-finalize"),
)
STAGE_NAMES = tuple(p[0] for p in PIPELINE)
PLUGIN = "curate-skills"
FINAL_MGA = {"passed", "failed", "external_only", "reused", "reused_failed"}
SOURCE_TAGS = {"catalog": "[catalog]", "adapt": "[adapt]", "cached": "[cached]",
               "user_provided": "[User Provided Model]", "new": "[new]"}


class InputError(Exception):
    pass


class NeedsChoice(InputError):
    """A decision only the user can make; the caller should ask, then rerun with it."""

    def __init__(self, message: str, choice: str, options: dict, **extra):
        super().__init__(message)
        self.details = {"needs_choice": choice, "options": options, **extra}


EXISTING_CODE_OPTIONS = {
    "keep": "Keep your code as it is: each function is only stub-tested; no subagents run.",
    "rewrite": "Rewrite: a subagent regenerates each function, starting from your code.",
    "ignore": "Ignore: drop your code and generate every function from scratch.",
}


# ── state I/O ────────────────────────────────────────────────────────────────

def load_state(run: str | Path) -> dict:
    path = Path(run) / "state.json"
    if not path.exists():
        raise InputError(f"{path} not found — is --run a run directory?")
    state = json.loads(path.read_text())
    if state.get("schema_version", 1) < SCHEMA_VERSION:
        raise InputError(f"{path} was made by curate-skills v1 (schema_version "
                         f"{state.get('schema_version', 1)}); continue it with the v1 plugin "
                         "(the curate-v1 worktree), or start a new run")
    return state


def save_state(run: str | Path, state: dict) -> None:
    write_json_atomic(Path(run) / "state.json", state)


def spec_of(data: Optional[dict]) -> Optional[WorkflowSpec]:
    return WorkflowSpec.model_validate(data) if data else None


def dump(spec: Optional[WorkflowSpec]) -> Optional[dict]:
    return json.loads(spec.model_dump_json()) if spec is not None else None


def read_json_file(path: str) -> object:
    p = Path(path)
    if not p.exists():
        raise InputError(f"{p} not found")
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise InputError(f"{p} is not valid JSON: {e}") from e


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def snapshot_history(run: Path, state: dict, label: str) -> None:
    hist = run / "history"
    hist.mkdir(exist_ok=True)
    n = len(list(hist.glob("*.json"))) + 1
    (hist / f"{n:03d}-{label}.json").write_text(json.dumps(state, indent=2) + "\n")


# ── settings resolution (flag > env > plugin setting > default) ─────────────

def _usable(value) -> bool:
    return value is not None and str(value).strip() != "" and "${" not in str(value)


def resolve_setting(flag, env_var: str, config, default, cast):
    for value, source in ((flag, "flag"), (os.environ.get(env_var), env_var),
                          (config, "plugin setting")):
        if _usable(value):
            try:
                return cast(str(value).strip()), source
            except ValueError as e:
                raise InputError(f"bad value {value!r} from {source}: {e}") from e
    return default, "default"


def _retries(v: str) -> int:
    n = int(v)
    if n < 0:
        raise ValueError("must be >= 0")
    return n


def _mode(v: str) -> str:
    if v not in STUBTEST_MODES:
        raise ValueError(f"must be one of {STUBTEST_MODES}")
    return v


# ── init / runs ──────────────────────────────────────────────────────────────

def _snake(name: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_").lower()
    if not s or s[0].isdigit():
        s = "fn_" + s
    return s


def _module_doc(path: Path) -> str:
    try:
        return (ast.get_docstring(ast.parse(path.read_text())) or "").strip()
    except (SyntaxError, OSError, UnicodeDecodeError):
        return ""


def cmd_init(args) -> dict:
    request = Path(args.request_file).read_text().strip() if args.request_file else ""
    if not request:
        raise InputError("the request file is empty")
    _, _, out = _new_run(args, request)
    return out


def _new_run(args, request: str, run_id_hint: str = "") -> tuple[Path, dict, dict]:
    """Create a run directory and its initial state (shared by init and import)."""
    project = Path(args.project_dir or ".").resolve()

    user_models = []
    for raw in args.user_model or []:
        p = Path(raw).expanduser().resolve()
        if not p.is_file() or p.suffix != ".py":
            raise InputError(f"user model {raw} must be an existing .py file")
        user_models.append(UserModel(name=_snake(p.stem), path=str(p), description=_module_doc(p)))
    names = [m.name for m in user_models]
    if len(set(names)) != len(names):
        raise InputError(f"user models need distinct file names, got {names}")
    context_files = []
    for raw in args.context_file or []:
        p = Path(raw).expanduser().resolve()
        if not p.is_file():
            raise InputError(f"context file {raw} not found")
        context_files.append(ContextFile(name=p.name, path=str(p)))

    catalog_dir, catalog_source = catalog_mod.resolve_catalog_dir(
        args.catalog_dir, None, args.config_catalog_dir)
    retries, retries_source = resolve_setting(
        args.mga_retries, "CURATE_MGA_RETRIES", args.config_mga_retries, DEFAULT_MGA_RETRIES, _retries)
    mode, mode_source = resolve_setting(
        args.stubtest_mode, "CURATE_STUBTEST_MODE", args.config_stubtest_mode,
        DEFAULT_STUBTEST_MODE, _mode)

    words = re.findall(r"[a-zA-Z0-9]+", (run_id_hint or request).lower())[:4]
    run_id = _dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + ("-".join(words) or "run")
    base_id, n = run_id, 2
    while (project / ".curate" / "runs" / run_id).exists():
        run_id, n = f"{base_id}-{n}", n + 1
    run = project / ".curate" / "runs" / run_id
    (run / "work").mkdir(parents=True)

    state = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "created": _now(),
        "project_dir": str(project),
        "run_dir": str(run),
        "catalog_dir": str(catalog_dir),
        "mga_max_retries": retries,
        "stubtest_mode": mode,
        "user_request": request,
        "user_models": [m.model_dump() for m in user_models],
        "context_files": [c.model_dump() for c in context_files],
        "stage": "skeleton",
        "iteration": 0,
        "max_iterations": DEFAULT_MAX_ITERATIONS,
        "revision_directives": [],
        "feedback": None,
        "code_feedback": None,
        "structural_change": None,
        "skeleton_draft": None,
        "spec": None,
        "prior_spec": None,
        "spec_is_seed": None,
        "enriched_spec": None,
        "enriched_basis": None,
        "spec_basis": None,
        "candidates": [],
        "repairs": [],
        "mga": {},
        "validation": None,
        "bundle_dir": None,
        "history": [],
        "stopped_from": None,
        "begun": None,
    }
    save_state(run, state)
    return run, state, {"ok": True, "run": str(run), "run_id": run_id,
            "settings": {
                "catalog_dir": {"value": str(catalog_dir), "source": catalog_source},
                "mga_max_retries": {"value": retries, "source": retries_source},
                "stubtest_mode": {"value": mode, "source": mode_source}},
            "user_models": [m.name for m in user_models],
            "context_files": [c.name for c in context_files]}


FOREGROUND_VAR = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"


def cmd_preflight(args) -> dict:
    """Generation needs curate-mga subagents to run in the foreground.

    In interactive sessions Claude Code runs subagents in the background unless
    CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1, and an idle session does not resume
    on the completion notice. Scripts inherit Claude Code's environment, so the
    variable is visible here exactly when the session has it.
    """
    value = os.environ.get(FOREGROUND_VAR)
    ok = value == "1"
    out = {"ok": ok, FOREGROUND_VAR: value}
    if not ok:
        out["error"] = preflight_error()
    return out


def preflight_error() -> str:
    return (f"{FOREGROUND_VAR} is not set to 1 in this Claude Code session, so curate-mga "
            "subagents would run in the background and the session would stall after each "
            "one. Quit and relaunch with the `curate` shell alias, which sets it: "
            f"alias curate='{FOREGROUND_VAR}=1 claude --plugin-dir <path to curate-skills>' "
            "(see the plugin README). Then resume with /curate --resume <run_id>.")


def cmd_runs(args) -> dict:
    root = Path(args.project_dir or ".").resolve() / ".curate" / "runs"
    runs = []
    for sp in sorted(root.glob("*/state.json")) if root.is_dir() else []:
        try:
            s = json.loads(sp.read_text())
        except json.JSONDecodeError:
            continue
        runs.append({"run": str(sp.parent), "run_id": s.get("run_id"), "stage": s.get("stage"),
                     "iteration": s.get("iteration"),
                     "workflow": (s.get("spec") or (s.get("skeleton_draft") or {}).get("spec") or {}).get("name"),
                     "request": (s.get("user_request") or "")[:80]})
    return {"ok": True, "runs": runs}


def cmd_status(args) -> dict:
    s = load_state(args.run)
    spec = spec_of(s.get("spec"))
    return {"ok": True, "run_id": s["run_id"], "stage": s["stage"], "iteration": s["iteration"],
            "max_iterations": s["max_iterations"], "catalog_dir": s["catalog_dir"],
            "mga_max_retries": s["mga_max_retries"], "stubtest_mode": s["stubtest_mode"],
            "workflow": spec.name if spec else None,
            "nodes": [n.name for n in spec.nodes] if spec else [],
            "mga": {k: v.get("status") for k, v in (s.get("mga") or {}).items()},
            "bundle_dir": s.get("bundle_dir"), "validation": s.get("validation"),
            "auto_emit_retries": auto_emit_retries(s), "next_skill": next_skill(s),
            "stopped_from": s.get("stopped_from"),
            **(resume_commands(s) if s["stage"] == "stopped" else {})}


def auto_emit_retries(s: dict) -> int:
    """Automatic emit→resolve retries since the latest Gate 3 approval."""
    count = 0
    for h in s.get("history") or []:
        if h.get("gate") == 3 and h.get("decision") == "approve":
            count = 0
        elif h.get("gate") == "emit":
            count += 1
    return count


# ── shared prompt sections (agents/wca.py 28-38, 118-163; agents/fca.py) ──────

def accumulated_feedback(state: dict, include_code_feedback: bool = False) -> str:
    """Standing change requests as a bullet list (wca.py _accumulated_feedback)."""
    directives = list(state.get("revision_directives") or [])
    fb = (state.get("feedback") or "").strip()
    if fb and (not directives or directives[-1] != fb):
        directives.append(fb)
    text = "\n".join(f"- {d}" for d in directives)
    cf = (state.get("code_feedback") or "").strip()
    if include_code_feedback and cf and cf not in text:
        text = (text + "\n- " + cf).strip()
    return text


def skeleton_outline(spec: WorkflowSpec) -> str:
    lines = []
    for n in spec.nodes:
        rank = f" rank={n.rank}" if (n.rank or 1) > 1 else ""
        lines.append(f"  - {n.name}{rank}: {(n.description or '').strip()}")
    edges = ", ".join(f"{e.from_node}->{e.to_node}" for e in spec.edges)
    body = "\n".join(lines) + (f"\n  edges: {edges}" if edges else "")
    return ("Current workflow being revised (apply the requested change; keep "
            "everything else identical):\n" + body + "\n")


def user_models_section(models: list[dict]) -> str:
    if not models:
        return ""
    lines = [f"  - {m['name']}: {(m.get('description') or '(user-provided script/function)').strip()[:120]}"
             for m in models]
    return ("User-provided functions (guaranteed steps — each MUST be a node with this EXACT "
            "snake_case name; the MGA will turn the attached code into a FaaSr function):\n"
            + "\n".join(lines) + "\n")


def context_files_section(files: list[dict]) -> str:
    if not files:
        return ""
    lines = [f"  - {c['name']}" + (f": {c['description'].strip()[:120]}" if c.get("description") else "")
             for c in files]
    return ("User-provided reference files (background material — papers, I/O examples, "
            "datasets; NOT nodes, do not add a node for them):\n" + "\n".join(lines) + "\n")


def _strip_code(spec_dict: dict) -> dict:
    out = dict(spec_dict)
    out["nodes"] = [{k: v for k, v in n.items() if k not in ("code", "tests")} for n in spec_dict["nodes"]]
    return out


def candidates_text(summary: list[dict]) -> str:
    """wca.py _fca_candidates_text."""
    def _io(c):
        return f"inputs=[{', '.join(c['inputs']) or '—'}] outputs=[{', '.join(c['outputs']) or '—'}]"
    return "\n".join(f"  {c['name']}: [{c['source'].upper()}] — {c['description'][:80]}\n      {_io(c)}"
                     for c in summary)


def cmd_prompt_context(args) -> dict:
    s = load_state(args.run)
    if args.stage == "skeleton":
        draft = s.get("skeleton_draft") or {}
        existing = spec_of(s.get("spec"))
        acc = accumulated_feedback(s)
        out = {
            "user_request": s["user_request"],
            "feedback_section": ("Standing change requests to incorporate (apply ALL — later items "
                                 "override earlier ones on conflict):\n" + acc + "\n") if acc else "",
            "current_workflow_section": skeleton_outline(existing) if existing and existing.nodes else "",
            "user_models_section": user_models_section(s["user_models"]),
            "context_files_section": context_files_section(s["context_files"]),
        }
        if draft.get("refinement"):
            out["refinement"] = draft["refinement"]
            out["previous_draft"] = draft.get("raw")
        return {"ok": True, "stage": "skeleton", **out}

    spec = spec_of(s.get("spec"))
    if spec is None:
        raise InputError("no approved skeleton yet (run approve --gate 1)")
    if args.stage in ("reuse", "mca"):
        ctx = f"{spec.name}: {(spec.description or '').strip()}".strip().rstrip(":").strip()
        if s["user_request"].strip():
            ctx += f"\nOriginal user request (verbatim): {s['user_request'].strip()}"
        if s["context_files"]:
            ctx += ("\nUser-provided reference files available to the implementer: "
                    + ", ".join(c["name"] for c in s["context_files"]))
        prior = {n.name: n for n in (spec_of(s.get("prior_spec")).nodes if s.get("prior_spec") else [])
                 if n.code}
        user_models = {m["name"]: m for m in s["user_models"]}
        cat = catalog_mod.Catalog(Path(s["catalog_dir"]))
        nodes = []
        for n in spec.nodes:
            p = prior.get(n.name)
            nodes.append({
                "name": n.name, "description": n.description, "rank": n.rank,
                "inputs": [{"name": i.name, "type": i.type} for i in n.inputs],
                "outputs": [{"name": o.name, "type": o.type} for o in n.outputs],
                "catalog_query": catalog_mod.node_query(n),
                "catalog_matches": [catalog_mod.hit_json(sc, e)["line"]
                                    for sc, e in cat.search(catalog_mod.node_query(n), args.k)],
                "prior_implementation": None if p is None else {
                    "description": p.description, "inputs": [i.name for i in p.inputs],
                    "outputs": [o.name for o in p.outputs]},
                "user_model": user_models.get(n.name),
            })
        return {"ok": True, "stage": "mca", "workflow_context": ctx or "(not specified)",
                "feedback": accumulated_feedback(s, include_code_feedback=True),
                "catalog_dir": s["catalog_dir"], "nodes": nodes}

    # resolve
    enriched = s.get("enriched_spec")
    if not enriched:
        raise InputError("no candidates applied yet (run apply-candidates)")
    acc = accumulated_feedback(s, include_code_feedback=True)
    return {"ok": True, "stage": "resolve",
            "workflow_json": _strip_code(enriched),
            "fca_candidates": candidates_text(s["candidates"]),
            "feedback_section": (f"Standing change requests (honor ALL — later items override earlier "
                                 f"ones on conflict):\n{acc}\n\nApply every one of these to the workflow.\n")
                                if acc else "",
            "user_models_section": user_models_section(s["user_models"]),
            "context_files_section": context_files_section(s["context_files"])}


# ── parsing (wca.py _parse_workflow_spec 72-115) ─────────────────────────────

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_workflow_spec(data: dict, skeleton: bool = False) -> WorkflowSpec:
    if not isinstance(data, dict):
        raise InputError("expected a JSON object")
    nodes = []
    for n in data.get("nodes", []):
        if not isinstance(n, dict) or not n.get("name"):
            raise InputError(f"every node needs a name, got {n!r}")
        if skeleton:
            inputs, outputs, deps, secrets = [], [], [], []
        else:
            inputs = [IOSpec(**i) if isinstance(i, dict) else IOSpec(name=str(i)) for i in n.get("inputs", [])]
            outputs = [IOSpec(**o) if isinstance(o, dict) else IOSpec(name=str(o)) for o in n.get("outputs", [])]
            deps = n.get("dependencies", [])
            secrets = n.get("secrets", [])
        nodes.append(FunctionSpec(
            name=n["name"], description=n.get("description", ""),
            inputs=inputs, outputs=outputs, dependencies=deps, secrets=secrets,
            rank=max(1, int(n.get("rank", 1) or 1)),
        ))
    if not nodes:
        raise InputError("the workflow has no nodes")
    names = [n.name for n in nodes]
    bad = [n for n in names if not _IDENT.match(n)]
    if bad:
        raise InputError(f"node names must be Python identifiers (snake_case): {bad}")
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise InputError(f"duplicate node names: {dupes}")
    edges = [WorkflowEdge(**e) for e in data.get("edges", [])]
    unknown = [f"{e.from_node}->{e.to_node}" for e in edges
               if e.from_node not in names or e.to_node not in names]
    if unknown and skeleton:
        raise InputError(f"edges reference unknown nodes: {unknown}")
    data_flow = [DataFlowEdge(**df) for df in data.get("data_flow", [])]
    return WorkflowSpec(
        name=data.get("name", "workflow"), description=data.get("description", ""),
        nodes=nodes, edges=edges, data_flow=data_flow,
        entry=data.get("entry") or nodes[0].name,
        folder=data.get("folder", "workflow_data"),
        compute_target=data.get("compute_target", "GH"),
        # Infrastructure binding, not a model choice: S3 keeps the payload's credential
        # references, GitHub secret names and local env vars aligned.
        data_store="S3",
    )


# ── DAG repair (wca.py _ensure_reachable 448-546, _prune_transitive_edges 549-610) ──

def ensure_reachable(spec: WorkflowSpec, log: list[str]) -> WorkflowSpec:
    nodes = spec.nodes
    if not nodes:
        return spec
    names = [n.name for n in nodes]
    name_set = set(names)
    edges, dropped = [], []
    for e in spec.edges:
        (edges if e.from_node in name_set and e.to_node in name_set else dropped).append(e)
    for e in dropped:
        log.append(f"dropped edge {e.from_node} → {e.to_node}: references a node that doesn't exist")

    incoming = {e.to_node for e in edges}
    entry = spec.entry if spec.entry in name_set else None
    if entry is None:
        roots = [n for n in names if n not in incoming]
        entry = roots[0] if roots else names[0]

    producers: dict[str, list[str]] = {}
    for df in spec.data_flow:
        if df.from_node in name_set and df.to_node in name_set and df.from_node != df.to_node:
            producers.setdefault(df.to_node, []).append(df.from_node)

    def _adjacency(edge_list):
        adj: dict[str, list[str]] = {n: [] for n in names}
        for e in edge_list:
            if e.from_node in adj and e.to_node in name_set:
                adj[e.from_node].append(e.to_node)
        return adj

    def _reach(start, adj):
        seen, stack = set(), [start]
        while stack:
            cur = stack.pop()
            if cur not in seen:
                seen.add(cur)
                stack.extend(adj.get(cur, []))
        return seen

    added: list[tuple[str, str]] = []
    for _ in range(len(names) + 1):
        adj = _adjacency(edges)
        reachable = _reach(entry, adj)
        orphans = [n for n in names if n not in reachable]
        if not orphans:
            break
        progressed = False
        for orphan in orphans:
            prod = [p for p in producers.get(orphan, []) if p in reachable]
            if prod:
                chosen = prod[0]
            elif not producers.get(orphan):
                chosen = entry
            else:
                continue
            if chosen == orphan or orphan in _reach(chosen, adj):
                continue
            edges.append(WorkflowEdge(from_node=chosen, to_node=orphan))
            added.append((chosen, orphan))
            progressed = True
        if not progressed:
            break
    for frm, to in added:
        log.append(f"added trigger edge {frm} → {to} to keep it reachable")
    if not added and not dropped and entry == spec.entry:
        return spec
    return spec.model_copy(update={"edges": edges, "entry": entry})


def prune_transitive_edges(spec: WorkflowSpec, log: list[str]) -> WorkflowSpec:
    remaining = list(spec.edges)
    if len(remaining) < 2:
        return spec

    def _reaches(start: str, target: str, edge_list) -> bool:
        adj: dict[str, list[str]] = {}
        for e in edge_list:
            if e.condition is None:
                adj.setdefault(e.from_node, []).append(e.to_node)
        seen: set[str] = set()
        stack = list(adj.get(start, []))
        while stack:
            cur = stack.pop()
            if cur == target:
                return True
            if cur not in seen:
                seen.add(cur)
                stack.extend(adj.get(cur, []))
        return False

    removed = []
    for edge in list(remaining):
        if edge.condition is not None:
            continue
        without = [e for e in remaining if e is not edge]
        if _reaches(edge.from_node, edge.to_node, without):
            remaining = without
            removed.append(edge)
    for e in removed:
        log.append(f"dropped redundant edge {e.from_node} → {e.to_node}: already ordered by another path")
    return spec.model_copy(update={"edges": remaining}) if removed else spec


def topo_order(spec: WorkflowSpec) -> list[str]:
    """Kahn's algorithm (faasr/context_dir.py bfs_topological_sort 76-110)."""
    graph = {n.name: [] for n in spec.nodes}
    indeg = {n.name: 0 for n in spec.nodes}
    for e in spec.edges:
        if e.from_node in graph and e.to_node in graph:
            graph[e.from_node].append(e.to_node)
            indeg[e.to_node] += 1
    queue = deque(n for n, d in indeg.items() if d == 0)
    order: list[str] = []
    while queue:
        name = queue.popleft()
        order.append(name)
        for s in graph[name]:
            indeg[s] -= 1
            if indeg[s] == 0:
                queue.append(s)
    order.extend(n.name for n in spec.nodes if n.name not in order)
    return order


def ancestors(spec: WorkflowSpec, node: str) -> list[str]:
    """All upstream nodes of `node`, in topological order."""
    parents: dict[str, list[str]] = {}
    for e in spec.edges:
        parents.setdefault(e.to_node, []).append(e.from_node)
    seen, stack = set(), list(parents.get(node, []))
    while stack:
        cur = stack.pop()
        if cur not in seen:
            seen.add(cur)
            stack.extend(parents.get(cur, []))
    return [n for n in topo_order(spec) if n in seen]


# ── skeleton / gate 1 ────────────────────────────────────────────────────────

def cmd_skeleton(args) -> dict:
    run = Path(args.run)
    s = load_state(run)
    draft = s.get("skeleton_draft") or {}
    last_good = draft.get("last_good")
    raw_path = Path(args.json)
    raw = raw_path.read_text() if raw_path.exists() else ""
    log: list[str] = []
    try:
        spec = prune_transitive_edges(parse_workflow_spec(read_json_file(args.json), skeleton=True), log)
    except (InputError, ValidationError, TypeError, ValueError) as e:
        msg = (f"Could not parse the revision: {e}. Showing the previous skeleton." if last_good
               else f"Error parsing workflow: {e}")
        s["skeleton_draft"] = {"spec": last_good, "last_good": last_good, "raw": raw,
                               "parse_error": msg, "refinement": None}
        s["stage"] = "gate1" if last_good else "skeleton"
        save_state(run, s)
        return {"ok": False, "error": msg, "kept_previous": bool(last_good)}
    d = dump(spec)
    s["skeleton_draft"] = {"spec": d, "last_good": d, "raw": raw, "parse_error": None, "refinement": None}
    s["stage"] = "gate1"
    save_state(run, s)
    return {"ok": True, "nodes": [n.name for n in spec.nodes], "repairs": log}


# ── candidates (agents/fca.py propose_candidate 199-276; wca.py renames 378-445) ──

def _coerce_iospecs(raw) -> Optional[list[IOSpec]]:
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw:
        if isinstance(item, dict) and item.get("name"):
            out.append(IOSpec(name=item["name"], type=item.get("type", "any"),
                              description=item.get("description", "")))
        elif isinstance(item, str) and item.strip():
            out.append(IOSpec(name=item.strip()))
    return out or None


def _uniq(*sources) -> list[str]:
    seen: list[str] = []
    for src in sources:
        for raw in src or []:
            v = str(raw).strip()
            if v and v not in seen:
                seen.append(v)
    return seen


def decide_node(node: FunctionSpec, result: dict, prior: Optional[FunctionSpec],
                user_model: Optional[dict], cat: catalog_mod.Catalog, notes: list[str]) -> FunctionSpec:
    """Map one MCA decision onto a node."""
    if user_model is not None:
        mode = result.get("user_model_mode") or "verbatim"
        if mode not in ("verbatim", "adapt"):
            raise InputError(f"{node.name}: user_model_mode must be verbatim or adapt")
        return node.model_copy(update={
            "source": "user_provided", "user_model_mode": mode, "catalog_id": None,
            "origin_kind": None, "origin_name": None,
            "code": Path(user_model["path"]).read_text(),
            "description": (user_model.get("description") or node.description or "").strip(),
        })

    decision = result.get("decision", "new")
    adapt_from = result.get("adapt_from")
    has_prior = prior is not None and bool(prior.code)

    if decision == "cached" and has_prior:
        return prior.model_copy(update={"source": "cached", "rank": node.rank,
                                        "origin_kind": "cache", "origin_name": prior.name})
    if decision == "cached":
        notes.append(f"{node.name}: 'cached' chosen but no prior implementation exists → new")

    if decision == "adapt" and has_prior and (adapt_from == "cached" or not result.get("catalog_id")):
        updates = {"source": "adapt", "catalog_id": None, "origin_kind": "cache",
                   "origin_name": prior.name, "rank": node.rank,
                   "description": result.get("adaptation_notes") or result.get("refined_description")
                   or prior.description}
        if (ins := _coerce_iospecs(result.get("updated_inputs"))) is not None:
            updates["inputs"] = ins
        if (outs := _coerce_iospecs(result.get("updated_outputs"))) is not None:
            updates["outputs"] = outs
        return prior.model_copy(update=updates)

    if decision in ("reuse", "adapt"):
        catalog_id = result.get("catalog_id")
        entry = cat.functions.get(catalog_id) if catalog_id else None
        if entry is None:
            notes.append(f"{node.name}: catalog_id {catalog_id!r} not in catalog → new")
        else:
            source = "adapt" if decision == "adapt" else "catalog"
            updates = {
                "source": source, "catalog_id": catalog_id, "origin_kind": "catalog",
                "origin_name": entry.function_spec.name,
                "description": result.get("adaptation_notes") or entry.function_spec.description,
                "secrets": _uniq(entry.function_spec.secrets, result.get("required_secrets")),
                "dependencies": clean_dependencies(entry.function_spec.dependencies),
                "rank": node.rank,  # rank is topology (from the skeleton), not the catalog item
            }
            if source == "adapt":
                updates["name"] = node.name
                if (ins := _coerce_iospecs(result.get("updated_inputs"))) is not None:
                    updates["inputs"] = ins
                if (outs := _coerce_iospecs(result.get("updated_outputs"))) is not None:
                    updates["outputs"] = outs
            return entry.function_spec.model_copy(update=updates)
    elif decision not in ("new", "cached"):
        notes.append(f"{node.name}: unknown decision {decision!r} → new")

    return node.model_copy(update={
        "source": "new", "catalog_id": None, "origin_kind": None, "origin_name": None,
        "description": result.get("refined_description") or node.description,
        "dependencies": clean_dependencies(result.get("suggested_dependencies") or node.dependencies),
        "secrets": _uniq(node.secrets, result.get("required_secrets")),
    })


def apply_renames(spec: WorkflowSpec, enriched: list[FunctionSpec], log: list[str]) -> WorkflowSpec:
    orig = [n.name for n in spec.nodes]
    desired = [(c.name or o) for c, o in zip(enriched, orig)]
    renames: dict[str, str] = {}
    final: list[FunctionSpec] = []
    for i, (o, cand) in enumerate(zip(orig, enriched)):
        new = desired[i]
        if new == o:
            final.append(cand)
            continue
        if new in orig[:i] + orig[i + 1:] or new in desired[:i] + desired[i + 1:]:
            final.append(cand.model_copy(update={"name": o, "source": "adapt"}))
            log.append(f"catalog name '{new}' collides with another node — keeping '{o}' as [adapt]")
            continue
        renames[o] = new
        final.append(cand)
    if not renames:
        return spec.model_copy(update={"nodes": final})

    def _r(name):
        return renames.get(name, name)

    for o, n in renames.items():
        log.append(f"node '{o}' reuses catalog '{n}' verbatim — renamed node and its edge references")
    return spec.model_copy(update={
        "nodes": final,
        "edges": [e.model_copy(update={"from_node": _r(e.from_node), "to_node": _r(e.to_node)})
                  for e in spec.edges],
        "data_flow": [d.model_copy(update={"from_node": _r(d.from_node), "to_node": _r(d.to_node)})
                      for d in spec.data_flow],
        "entry": _r(spec.entry),
    })


def candidate_summary(n: FunctionSpec) -> dict:
    return {"name": n.name, "source": n.source, "user_model_mode": n.user_model_mode,
            "catalog_id": n.catalog_id, "origin_kind": n.origin_kind, "origin_name": n.origin_name,
            "description": n.description, "inputs": [i.name for i in n.inputs],
            "outputs": [o.name for o in n.outputs], "dependencies": n.dependencies or [],
            "secrets": n.secrets or []}


def cmd_apply_candidates(args) -> dict:
    run = Path(args.run)
    s = load_state(run)
    spec = spec_of(s.get("spec"))
    if spec is None:
        raise InputError("no approved skeleton yet")
    data = read_json_file(args.json)
    if not isinstance(data, list):
        raise InputError("candidates.json must be a list of per-node decisions")
    by_node = {}
    for item in data:
        if not isinstance(item, dict) or not item.get("node"):
            raise InputError(f"each candidate needs a 'node' field, got {item!r}")
        by_node[item["node"]] = item
    missing = [n.name for n in spec.nodes if n.name not in by_node]
    extra = sorted(set(by_node) - {n.name for n in spec.nodes})
    if missing or extra:
        raise InputError(f"candidates must cover each node exactly once; missing={missing} unknown={extra}")

    prior = {n.name: n for n in (spec_of(s.get("prior_spec")).nodes if s.get("prior_spec") else [])
             if n.code}
    user_models = {m["name"]: m for m in s["user_models"]}
    cat = catalog_mod.Catalog(Path(s["catalog_dir"]))
    notes: list[str] = []
    enriched = [decide_node(n, by_node[n.name], prior.get(n.name), user_models.get(n.name), cat, notes)
                for n in spec.nodes]
    log: list[str] = []
    unknown = [n for n in notes if "not in catalog" in n]
    if unknown and getattr(args, "strict", False):
        raise InputError("candidates name catalog entries that are not in this run's catalog "
                         f"({s['catalog_dir']}): {unknown}")
    with_candidates = apply_renames(spec, enriched, log)
    summary = [candidate_summary(n) for n in with_candidates.nodes]
    s["enriched_spec"] = dump(with_candidates)
    s["enriched_basis"] = spec_hash(s["spec"])
    s["candidates"] = summary
    s["repairs"] = log
    s["stage"] = "resolve"
    s["begun"] = None
    save_state(run, s)
    return {"ok": True, "candidates": [{k: c[k] for k in ("name", "source", "user_model_mode",
                                                          "origin_kind", "origin_name")} for c in summary],
            "notes": notes, "repairs": log}


# ── resolve (wca.py _merge_resolved 323-375) ─────────────────────────────────

def merge_resolved(resolved: WorkflowSpec, enriched_nodes: list[FunctionSpec]) -> WorkflowSpec:
    by_name = {n.name: n for n in enriched_nodes}
    final = []
    for node in resolved.nodes:
        c = by_name.get(node.name, node)
        if c.source in ("catalog", "cached"):
            update = {"source": c.source, "catalog_id": c.catalog_id, "origin_kind": c.origin_kind,
                      "origin_name": c.origin_name, "code": c.code,
                      "dependencies": clean_dependencies(c.dependencies or node.dependencies),
                      "secrets": c.secrets, "inputs": c.inputs or node.inputs,
                      "outputs": c.outputs or node.outputs}
        elif c.source in ("adapt", "user_provided"):
            update = {"source": c.source, "user_model_mode": c.user_model_mode,
                      "catalog_id": c.catalog_id, "origin_kind": c.origin_kind,
                      "origin_name": c.origin_name, "code": c.code,
                      "dependencies": clean_dependencies(c.dependencies or node.dependencies),
                      "secrets": c.secrets}
        else:
            update = {"source": c.source, "catalog_id": c.catalog_id, "code": c.code,
                      "dependencies": clean_dependencies(c.dependencies or node.dependencies),
                      "secrets": c.secrets}
        final.append(node.model_copy(update=update))
    return resolved.model_copy(update={"nodes": final})


def cmd_merge_resolved(args) -> dict:
    run = Path(args.run)
    s = load_state(run)
    enriched = spec_of(s.get("enriched_spec"))
    if enriched is None:
        raise InputError("no candidates applied yet (run apply-candidates)")
    try:
        resolved = parse_workflow_spec(read_json_file(args.json))
    except ValidationError as e:
        raise InputError(f"resolved.json is not a valid WorkflowSpec:\n{e}") from e
    names = {n.name for n in resolved.nodes}
    expected = {n.name for n in enriched.nodes}
    if names != expected:
        raise InputError(f"resolved nodes must match the candidates exactly; "
                         f"missing={sorted(expected - names)} unknown={sorted(names - expected)}")
    log: list[str] = []
    spec = prune_transitive_edges(ensure_reachable(merge_resolved(resolved, enriched.nodes), log), log)
    s["spec"] = dump(spec)
    s["spec_basis"] = spec_hash(s["enriched_spec"])
    s["repairs"] = (s.get("repairs") or []) + log
    s["stage"] = "gate2"
    save_state(run, s)
    problems = check_spec(spec)
    return {"ok": True, "repairs": log, "problems": problems,
            "errors": sum(p["severity"] == "error" for p in problems)}


# ── consistency check ────────────────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"\{([^}]*)\}")


def _file_pattern(name: str) -> re.Pattern:
    return re.compile("^" + re.escape(name).replace(r"\{rank\}", r"\d+") + "$")


def _names_match(a: str, b: str) -> bool:
    return a == b or bool(_file_pattern(a).match(b)) or bool(_file_pattern(b).match(a)) \
        or a.replace("{rank}", "#") == b.replace("{rank}", "#")


def check_spec(spec: WorkflowSpec) -> list[dict]:
    problems: list[dict] = []

    def add(sev, msg):
        problems.append({"severity": sev, "message": msg})

    nodes = {n.name: n for n in spec.nodes}
    for n in spec.nodes:
        if not _IDENT.match(n.name):
            add("error", f"node name {n.name!r} is not a Python identifier")
        for io in n.inputs + n.outputs:
            for ph in _PLACEHOLDER.findall(io.name):
                if ph != "rank":
                    add("error", f"{n.name}: '{io.name}' uses {{{ph}}}; only {{rank}} is allowed")
        if n.rank > 1:
            for o in n.outputs:
                if "{rank}" not in o.name:
                    add("warning", f"{n.name} runs ×{n.rank} but output '{o.name}' has no {{rank}}: "
                                   f"instances would overwrite each other")
        if not n.outputs:
            add("warning", f"{n.name} declares no outputs")
    for e in spec.edges:
        for end in (e.from_node, e.to_node):
            if end not in nodes:
                add("error", f"edge {e.from_node} → {e.to_node} references unknown node {end!r}")
    if spec.entry not in nodes:
        add("error", f"entry {spec.entry!r} is not a node")

    targets = {e.to_node for e in spec.edges}
    roots = [n for n in nodes if n not in targets]
    if roots != [spec.entry] and len(nodes) > 1:
        add("error", f"workflow must have exactly one entry ({spec.entry}); nodes with no trigger: {roots}")
    order = topo_order(spec)
    graph = {n: [e.to_node for e in spec.edges if e.from_node == n] for n in nodes}
    pos = {n: i for i, n in enumerate(order)}
    if any(pos.get(t, 0) <= pos[f] for f, ts in graph.items() for t in ts if t in pos):
        add("error", "the workflow contains a cycle")

    def reaches(a, b):
        seen, stack = set(), list(graph.get(a, []))
        while stack:
            cur = stack.pop()
            if cur == b:
                return True
            if cur not in seen:
                seen.add(cur)
                stack.extend(graph.get(cur, []))
        return False

    for df in spec.data_flow:
        if df.from_node not in nodes or df.to_node not in nodes:
            add("error", f"data_flow '{df.file}' references unknown node(s) {df.from_node} → {df.to_node}")
            continue
        if not any(_names_match(o.name, df.file) for o in nodes[df.from_node].outputs):
            add("error", f"data_flow '{df.file}': not among {df.from_node}'s outputs "
                         f"{[o.name for o in nodes[df.from_node].outputs]}")
        if not any(_names_match(i.name, df.file) for i in nodes[df.to_node].inputs):
            add("error", f"data_flow '{df.file}': not among {df.to_node}'s inputs "
                         f"{[i.name for i in nodes[df.to_node].inputs]}")
        if not reaches(df.from_node, df.to_node):
            add("error", f"data_flow '{df.file}': {df.to_node} can run before {df.from_node} "
                         f"(no invocation path)")
    return problems


def cmd_check(args) -> dict:
    s = load_state(args.run)
    spec = spec_of(s.get("spec"))
    if spec is None:
        raise InputError("no spec yet")
    problems = check_spec(spec)
    errors = sum(p["severity"] == "error" for p in problems)
    return {"ok": errors == 0, "problems": problems, "errors": errors}


def cmd_toposort(args) -> dict:
    spec = spec_of(load_state(args.run).get("spec"))
    if spec is None:
        raise InputError("no spec yet")
    return {"ok": True, "order": topo_order(spec),
            "ancestors": {n.name: ancestors(spec, n.name) for n in spec.nodes}}


# ── gate decisions ───────────────────────────────────────────────────────────

def _record(s: dict, gate, decision: str, text: str = "", structural=None) -> None:
    s.setdefault("history", []).append({"gate": gate, "decision": decision, "text": text,
                                        "structural": structural, "ts": _now()})


def cmd_approve(args) -> dict:
    run = Path(args.run)
    s = load_state(run)
    g = args.gate
    if getattr(args, "deploy", None) and g != 4:
        raise InputError("--deploy only applies to Gate 4")
    if g == 1:
        draft = s.get("skeleton_draft") or {}
        spec = draft.get("spec")
        if not spec:
            raise InputError("nothing to approve: no valid skeleton draft")
        existing = s.get("spec")
        # Stash genuinely-implemented code before the stripped skeleton replaces it
        # (wca.py 288-296); Gate-2 seeds (spec_is_seed) are never stashed.
        if existing and spec_of(existing).nodes and not s.get("spec_is_seed") \
                and any(n.code for n in spec_of(existing).nodes):
            s["prior_spec"] = existing
        s["spec"] = spec
        s["skeleton_draft"] = None
        s["spec_is_seed"] = None
        s.update(enriched_spec=None, enriched_basis=None, spec_basis=None, stage="reuse")
    elif g == 2:
        if not s.get("spec"):
            raise InputError("no resolved spec to approve")
        # enriched_spec is kept: generate checks spec_basis against it.
        s.update(feedback=None, structural_change=None, spec_is_seed=None, stage="generate")
    elif g == 3:
        s.update(code_feedback=None, structural_change=None, stage="emit")
    elif g == 4:
        v = s.get("validation") or {}
        if not v.get("ok"):
            raise InputError("validation has not passed; cannot approve Gate 4")
        if v.get("spec_hash") != spec_hash(s.get("spec")):
            raise InputError("the bundle was built from a different version of the spec; run "
                             f"/{PLUGIN}:curate-emit --run {s['run_id']} again before approving Gate 4")
        choice = getattr(args, "deploy", None) or "no"
        bundle = Path(s["bundle_dir"]) if s.get("bundle_dir") else None
        has_bundle = bool(bundle and (bundle / "workflow.json").is_file())
        if choice == "yes" and not has_bundle:
            raise InputError(f"the bundle {bundle} is missing, so it cannot be deployed; run "
                             f"/{PLUGIN}:curate-emit --run {s['run_id']} again")
        # The approved bundle's hash: curate-deploy refuses to deploy a bundle that changed since.
        s.update(feedback=None, bundle_hash=tree_hash(bundle) if has_bundle else None,
                 deploy_choice=choice, deploy=None,
                 stage="deploy" if choice == "yes" else "finalize")
    s["begun"] = None
    _record(s, g, "approve")
    if g == 4:
        s["history"][-1]["deploy"] = s["deploy_choice"]
    snapshot_history(run, s, f"gate{g}-approve")
    save_state(run, s)
    out = {"ok": True, "stage": s["stage"]}
    if g == 4:
        spec = spec_of(s["spec"])
        out["deploy"] = s["deploy_choice"]
        out["next_skill"] = next_skill(s)
        out["reused_catalog_ids"] = sorted({n.catalog_id for n in spec.nodes
                                            if n.catalog_id and n.source in ("catalog", "adapt")})
    return out


def _bool(v: Optional[str]) -> Optional[bool]:
    if v is None:
        return None
    if v.lower() in ("true", "yes", "structure", "1"):
        return True
    if v.lower() in ("false", "no", "function", "0"):
        return False
    raise InputError(f"--structural must be true or false, got {v!r}")


def cmd_directive(args) -> dict:
    run = Path(args.run)
    s = load_state(run)
    text = (args.text or "").strip()
    if not text:
        raise InputError("--text is empty")
    g = args.gate
    structural = _bool(args.structural)
    if g in ("2", "3", "4") and structural is None:
        raise InputError(f"gate {g} revisions need --structural true|false")

    if g == "1":
        draft = s.get("skeleton_draft") or {}
        s["skeleton_draft"] = {**draft, "refinement": text}
        s["stage"] = "skeleton"
        _record(s, 1, "refine", text)
        save_state(run, s)
        return {"ok": True, "next_stage": "skeleton", "iteration": s["iteration"],
                "cap_reached": False}

    if s["iteration"] >= s["max_iterations"]:
        # Checked before anything changes: "approve as-is" stays a plain approve, and
        # "raise the cap" is set-cap followed by this same directive.
        return {"ok": False, "cap_reached": True, "iteration": s["iteration"],
                "max_iterations": s["max_iterations"], "stage": s["stage"],
                "error": f"revision cap reached ({s['iteration']}/{s['max_iterations']}); "
                         "nothing was changed. Ask: approve as-is, stop, or raise the cap "
                         "(set-cap) and repeat this directive."}

    spec = s.get("spec")
    if g == "2":
        s.update(feedback=text, structural_change=structural, spec_is_seed=True)
        s["revision_directives"].append(text)
    elif g == "3":
        s["revision_directives"].append(text)
        if structural:
            s.update(feedback=text, code_feedback=None, structural_change=True)
        else:
            s.update(code_feedback=text, structural_change=False, prior_spec=spec)
    elif g == "4":
        s["revision_directives"].append(text)
        s.update(feedback=text, structural_change=structural, prior_spec=spec)
    elif g == "emit":
        # Automatic retry after a validation failure: transient feedback, functional.
        structural = False
        s.update(feedback=text, structural_change=False, prior_spec=spec)
    s["iteration"] += 1
    # Functional changes go back through reuse (v1's resolve stage included the MCA).
    s["stage"] = "skeleton" if structural else "reuse"
    s["begun"] = None
    if structural:
        s["skeleton_draft"] = None
    _record(s, g if g == "emit" else int(g), "revise", text, structural)
    snapshot_history(run, s, f"gate{g}-revise")
    save_state(run, s)
    return {"ok": True, "next_stage": s["stage"], "structural": structural,
            "iteration": s["iteration"], "max_iterations": s["max_iterations"],
            "cap_reached": False, "next_skill": next_skill(s)}


def cmd_confirm_start(args) -> dict:
    """The user answered Yes to "Start this run?". For a run started at deploy, that answer is the
    deploy choice (the source's Gate 4 may predate the deploy option); it is recorded as such."""
    run = Path(args.run)
    s = load_state(run)
    hist = s.get("history") or []
    first = hist[0] if hist else {}
    if not (first.get("decision") == "copied" and first.get("gate") == 4 and first.get("deploy") == "pending"):
        return {"ok": True, "recorded": None, "stage": s["stage"]}
    if s.get("deploy_choice") == "yes":
        return {"ok": True, "recorded": None, "stage": s["stage"], "deploy_choice": "yes"}
    s["deploy_choice"] = "yes"
    _record(s, 4, "deploy-confirmed",
            'the user answered Yes to "Start this run?" for --start-at deploy; this is the deploy choice '
            f"(the source's Gate 4 recorded: {first.get('source_deploy_choice') or 'no deploy choice, approved before deploying existed'})")
    s["history"][-1]["deploy"] = "yes"
    save_state(run, s)
    return {"ok": True, "recorded": "deploy_choice", "deploy_choice": "yes", "stage": s["stage"],
            "next_skill": next_skill(s)}


def cmd_set_cap(args) -> dict:
    run = Path(args.run)
    s = load_state(run)
    if args.max < 1:
        raise InputError("--max must be >= 1")
    s["max_iterations"] = args.max
    save_state(run, s)
    return {"ok": True, "max_iterations": args.max, "iteration": s["iteration"]}


def cmd_set_stage(args) -> dict:
    """Low-level stage change (stop, and tests). Stages are normally moved by begin/approve."""
    run = Path(args.run)
    s = load_state(run)
    if args.stage == "stopped" and s["stage"] != "stopped":
        s["stopped_from"] = s["stage"]
    s["stage"] = args.stage
    s["begun"] = None
    save_state(run, s)
    out = {"ok": True, "stage": args.stage, "next_skill": next_skill(s)}
    if args.stage == "stopped":
        out.update(resume_commands(s))
    return out


# ── import: start a stage from your own files ────────────────────────────────

IMPORT_PREV_GATE = {"reuse": 1, "resolve": 1, "generate": 2, "emit": 3}
BUNDLE_HEADER = "# Generated by curate-skills"


def strip_bundle_header(code: str) -> str:
    """Drop the 3-line header emit.py adds to bundle functions (and the blank after it)."""
    lines = code.splitlines(keepends=True)
    if lines and lines[0].startswith(BUNDLE_HEADER):
        lines = lines[3:]
        if lines and not lines[0].strip():
            lines = lines[1:]
    return "".join(lines)


def _file_sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_full_spec(path: str) -> WorkflowSpec:
    data = read_json_file(path)
    if not isinstance(data, dict):
        raise InputError(f"{path}: expected a WorkflowSpec JSON object")
    data = {**data, "data_store": "S3"}
    try:
        spec = WorkflowSpec.model_validate(data)
    except ValidationError as e:
        raise InputError(f"{path} is not a valid WorkflowSpec:\n{e}") from e
    bad = [n.name for n in spec.nodes if not _IDENT.match(n.name)]
    if bad or not spec.nodes:
        raise InputError(f"{path}: needs nodes with Python-identifier names (bad: {bad})")
    if not spec.entry:
        spec = spec.model_copy(update={"entry": spec.nodes[0].name})
    return spec


def _is_full_spec(data) -> bool:
    return isinstance(data, dict) and any(
        n.get("inputs") or n.get("outputs") or n.get("code") for n in data.get("nodes") or []
        if isinstance(n, dict))


EXISTING_CODE_ALIASES = {"reuse": "keep", "seed": "rewrite"}  # old --functions-as values


def _existing_code_mode(args) -> Optional[str]:
    """--existing-code, or the old --functions-as (hidden alias). Both given must agree."""
    new = getattr(args, "existing_code", None)
    old = EXISTING_CODE_ALIASES.get(getattr(args, "functions_as", None) or "")
    if new and old and new != old:
        raise InputError(f"--existing-code {new} contradicts --functions-as {args.functions_as}; use one")
    return new or old


def _apply_functions(spec: WorkflowSpec, directory: Optional[str], mode: Optional[str],
                     stage: str) -> tuple[WorkflowSpec, list[str], list[str]]:
    """Attach code from --functions files (which override code inside the spec).

    At generate, every function that ends up with code (from a file or inline in the spec,
    e.g. a bundle's spec.json) needs an explicit --existing-code:
      keep    — keep it as is (stub-tested only, no subagents)
      rewrite — regenerate each function starting from it
      ignore  — drop it and generate from scratch
    Nothing is used silently as a seed.
    """
    d = Path(directory) if directory else None
    if d is not None and not d.is_dir():
        raise InputError(f"--functions {directory} is not a directory")
    if mode == "ignore" and d is not None:
        raise InputError("--existing-code ignore drops all code, so --functions has no effect; "
                         "leave out --functions, or choose keep or rewrite")
    from_files, inline, undecided, nodes = [], [], [], []
    for n in spec.nodes:
        f = d / f"{n.name}.py" if d is not None else None
        update: dict = {}
        if f is not None and f.exists():
            update["code"] = strip_bundle_header(f.read_text())
            from_files.append(n.name)
        elif n.code:
            inline.append(n.name)
        has_code = bool(update.get("code") or n.code)
        if stage == "generate" and has_code:
            if mode == "keep":
                update.update(source="cached", origin_kind="import", origin_name=n.name)
            elif mode == "rewrite":
                update.update(source="adapt", origin_kind="import", origin_name=n.name)
            elif mode == "ignore":
                update.update(code=None, source="new", origin_kind=None, origin_name=None,
                              catalog_id=None)
            else:
                undecided.append(n.name)
        nodes.append(n.model_copy(update=update) if update else n)
    if undecided:
        where = " and ".join(x for x, ok in (("--functions", from_files), ("the spec itself", inline)) if ok)
        raise NeedsChoice(
            f"function(s) {', '.join(undecided)} already have code (from {where}). Say what to do "
            "with it: --existing-code keep (keep it as is; stub-tested only, no subagents), "
            "--existing-code rewrite (regenerate each function starting from it) or "
            "--existing-code ignore (drop it and generate from scratch).",
            "existing_code", EXISTING_CODE_OPTIONS, functions=undecided, flag="--existing-code")
    return spec.model_copy(update={"nodes": nodes}), from_files, inline


def cmd_import(args) -> dict:
    stage = args.stage
    if stage == "deploy":
        raise InputError("deploying needs a bundle approved at Gate 4 in a run: start from that run "
                         "with --start-at deploy --from-run <run_id>, or import at emit and approve Gate 4")
    if stage == "monitor":
        raise InputError("monitoring watches a deployed run: /curate-skills:curate-monitor --run <run_id>")
    if stage == "finalize":
        raise InputError("starting at finalize from files is not supported: a bundle has no stub-test "
                         "results, so nothing could be saved as verified. Use --run on the run that "
                         "built it, or import at generate with --existing-code keep")
    if args.review and stage not in ("reuse", "generate", "emit"):
        raise InputError(f"--review stops at the gate before {stage}; {stage} has none "
                         "(use it when importing at reuse, generate or emit)")
    if _existing_code_mode(args) and stage != "generate":
        raise InputError("--existing-code only applies when starting at generate")
    src = args.from_file
    if not Path(src).is_file():
        raise InputError(f"{src} not found")

    warnings: list[str] = []
    data = None if stage == "skeleton" else read_json_file(src)
    if stage == "skeleton":
        request = Path(src).read_text().strip()
        if not request:
            raise InputError(f"{src} is empty")
    elif args.request_file:
        request = Path(args.request_file).read_text().strip()
    else:
        name = (data or {}).get("name", "workflow") if isinstance(data, dict) else "workflow"
        desc = (data or {}).get("description", "") if isinstance(data, dict) else ""
        request = f"{name}: {desc}".strip().rstrip(":")
        warnings.append("no sketch given: the request text was built from the spec's name and "
                        "description (pass --request-file to use your own)")

    run, s, out = _new_run(args, request, run_id_hint=Path(src).stem)
    try:
        detail = _import_into(run, s, stage, src, data, args, warnings)
    except Exception:
        shutil.rmtree(run, ignore_errors=True)
        raise
    s = load_state(run)
    out.update(stage=s["stage"], next_skill=next_skill(s), warnings=warnings, **detail)
    return out


def _import_into(run: Path, s: dict, stage: str, src: str, data, args, warnings: list[str]) -> dict:
    ns = argparse.Namespace
    gate = IMPORT_PREV_GATE.get(stage)
    if stage == "skeleton":
        _record(s, 0, "imported", src)
        s["history"][-1]["sha256"] = _file_sha(src)
        save_state(run, s)
        return {}

    if stage in ("reuse", "resolve"):
        if _is_full_spec(data):
            if args.review:
                raise InputError("--review needs a skeleton.json (no inputs/outputs); this file is a "
                                 "full WorkflowSpec, which is imported as a seed")
            spec = _load_full_spec(src)
            s.update(spec=dump(spec), spec_is_seed=True, stage="reuse")
            warnings.append("full WorkflowSpec imported as a seed for reuse (inputs/outputs kept)")
        else:
            work = run / "work" / "skeleton.json"
            work.write_text(json.dumps(data, indent=2))
            res = cmd_skeleton(ns(run=str(run), json=str(work)))
            if not res["ok"]:
                raise InputError(f"{src}: {res['error']}")
            if args.review:
                return {"review": "gate1"}
            cmd_approve(ns(run=str(run), gate=1))
            s = load_state(run)
        _record(s, 1, "imported", src)
        s["history"][-1]["sha256"] = _file_sha(src)
        s["begun"] = None
        save_state(run, s)
        if stage == "reuse":
            return {}
        if not args.candidates:
            raise InputError("starting at resolve needs --candidates candidates.json "
                             "(the reuse decisions, as written by curate-reuse)")
        res = cmd_apply_candidates(ns(run=str(run), json=args.candidates, strict=True))
        s = load_state(run)
        _record(s, "reuse", "imported", args.candidates)
        s["history"][-1]["sha256"] = _file_sha(args.candidates)
        save_state(run, s)
        return {"candidates": res["candidates"]}

    # generate / emit: a full spec, optionally with function files.
    spec = _load_full_spec(src)
    mode = _existing_code_mode(args) if stage == "generate" else None
    spec, found, inline = _apply_functions(spec, args.functions, mode, stage)
    s["spec"] = dump(spec)
    s["enriched_spec"] = None
    problems = [p for p in require_problems(s, stage, env={"CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1"})]
    if problems:
        raise InputError(f"{src} is not enough to start {stage}: " + "; ".join(problems))
    if stage == "emit":
        warnings.append("imported code has not been stub-tested, so wda-finalize will not save it; "
                        "import at generate with --existing-code keep to verify it")
    s["stage"] = f"gate{gate}"
    _record(s, gate, "imported", src)
    s["history"][-1]["sha256"] = _file_sha(src)
    save_state(run, s)
    if not args.review:
        cmd_approve(ns(run=str(run), gate=gate))
    return {"functions_imported": found, "inline_code": inline,
            "existing_code": mode if found or inline else None}


# ── start: /curate --start-at with typed sources ─────────────────────────────

# Which gate's approval a start point builds on, and the stage that owns that gate.
START_GATE = {"reuse": 1, "resolve": 2, "generate": 2, "emit": 3, "deploy": 4, "finalize": 4}
GATE_OWNER = {1: "skeleton", 2: "resolve", 3: "generate", 4: "emit"}
# Fields copied from the approval snapshot (besides spec), per start point.
START_COPY = {
    "reuse": (),
    "resolve": ("enriched_spec", "enriched_basis", "spec_basis", "candidates"),
    "generate": ("enriched_spec", "spec_basis"),
    "emit": ("enriched_spec", "spec_basis", "mga"),
    "deploy": ("enriched_spec", "spec_basis", "mga", "validation", "bundle_dir", "bundle_hash"),
    "finalize": ("enriched_spec", "spec_basis", "mga", "validation", "bundle_dir"),
}
INIT_FLAGS = ("user_model", "context_file", "catalog_dir", "config_catalog_dir", "mga_retries",
              "config_mga_retries", "stubtest_mode", "config_stubtest_mode", "project_dir")


def _find_run(ref: str, project: Path) -> Path:
    p = Path(ref).expanduser()
    if (p / "state.json").is_file():
        return p.resolve()
    runs = project / ".curate" / "runs"
    if (runs / ref / "state.json").is_file():
        return (runs / ref).resolve()
    close = sorted(d.name for d in runs.iterdir() if d.is_dir() and ref in d.name) if runs.is_dir() else []
    if close:
        raise InputError(f"no run is exactly {ref!r}; did you mean one of: {', '.join(close)}? "
                         "Give the full run id (or the run's folder)")
    raise InputError(f"no run {ref!r} in {runs} (give a run id from this project, or a run folder)")


def _superseded_by(history: list, approval_index: int, gate: int) -> Optional[dict]:
    """A later change request or rewind that sent the run back before `gate`."""
    owner = STAGE_NAMES.index(GATE_OWNER[gate])
    for h in history[approval_index + 1:]:
        target = None
        if h.get("decision") == "revise":
            target = "skeleton" if h.get("structural") else "reuse"
        elif h.get("decision") == "rewind":
            m = re.match(r"to (\w+)", h.get("text") or "")
            target = m.group(1) if m else None
        if target and STAGE_NAMES.index(target) <= owner:
            return h
    return None


def _redoes(at: str) -> list[str]:
    """The stages a run started at `at` goes through; deploy only if chosen at Gate 4."""
    cond = {"deploy": "deploy (if chosen at Gate 4)", "monitor": "monitor (if deployed)"}
    return [cond[n] if n in cond and at not in ("deploy", "monitor") else n
            for n in STAGE_NAMES[STAGE_NAMES.index(at):]]


def _start_from_run(args, at: str) -> dict:
    project = Path(args.project_dir or ".").resolve()
    src = _find_run(args.from_run, project)
    st = json.loads((src / "state.json").read_text())  # read only: no lock, nothing written
    if st.get("schema_version", 1) < SCHEMA_VERSION:
        raise InputError(f"run {st.get('run_id')} was made by curate-skills v1; start from it with the "
                         "v1 plugin (the curate-v1 worktree)")
    gate = START_GATE[at]
    history = st.get("history") or []
    approvals = [i for i, h in enumerate(history) if h.get("gate") == gate and h.get("decision") == "approve"]
    if not approvals:
        raise InputError(f"run {st['run_id']} never approved Gate {gate}, which starting at {at} builds on "
                         f"(the run is at stage {st['stage']}); approve Gate {gate} in that run first, "
                         "or start at an earlier stage")
    sup = _superseded_by(history, approvals[-1], gate)
    if sup:
        raise InputError(f"run {st['run_id']}'s last Gate {gate} approval was superseded by a later "
                         f"{sup['decision']} at gate {sup['gate']} ({(sup.get('text') or '')[:80]!r}) and "
                         f"Gate {gate} was not approved again; finish that in run {st['run_id']}, or start "
                         "at an earlier stage")
    snaps = sorted((src / "history").glob(f"*-gate{gate}-approve.json"))
    if not snaps:
        raise InputError(f"run {st['run_id']} has no Gate {gate} approval snapshot in history/")
    snap_path = snaps[-1]
    snap = json.loads(snap_path.read_text())
    if not snap.get("spec"):
        raise InputError(f"{snap_path.name} has no approved spec")
    spec = spec_of(snap["spec"])
    copied = {f: snap.get(f) for f in START_COPY[at] if snap.get(f) not in (None, {}, [])}
    moved_from = None
    if copied.get("bundle_dir"):
        copied["bundle_dir"], moved_from = _approved_bundle_dir(st, snap)
    hash_from = None
    if at == "deploy":
        copied, hash_from = _deploy_bundle_hash(st, snap, copied)
    code_from = None
    if at == "generate":
        spec, code_from = _code_from_gate3(args, st, src, history, approvals[-1], spec)
    preview = {
        "source": {"kind": "run", "run_id": st["run_id"], "run_dir": str(src),
                   "source_stage_now": st["stage"]},
        "uses": f"history/{snap_path.name} (the latest Gate {gate} approval)"
                + (f"; code from history/{code_from.name} (the latest Gate 3 approval)" if code_from else ""),
        "workflow": spec.name,
        "functions": [{"name": n.name, "source": n.source, "has_code": bool(n.code),
                       "stub_test": ((snap.get("mga") or {}).get(n.name) or {}).get("status")
                       if at in ("emit", "finalize") else None} for n in spec.nodes],
        "copies": ["approved spec"] + sorted(copied) + ["sketch, attachments and standing change requests"],
        **({"bundle_hash_from": hash_from} if hash_from else {}),
        **({"bundle_dir_moved_from": moved_from} if moved_from else {}),
        "redoes": _redoes(at),
        "settings": _preview_settings(args),
    }
    if args.dry_run:
        return preview
    run, s, out = _new_run(args, snap.get("user_request") or st.get("user_request") or spec.name,
                           run_id_hint=spec.name)
    s["user_models"] = snap.get("user_models") or []
    s["context_files"] = snap.get("context_files") or []
    s["revision_directives"] = list(snap.get("revision_directives") or [])
    s["spec"] = dump(spec)
    s.update(copied)
    if at == "deploy":
        # Pending until the user answers Yes to "Start this run?" (spec.py confirm-start).
        s["deploy_choice"] = None
    s["stage"] = PIPELINE[STAGE_NAMES.index(at)][1][0]
    s["begun"] = None
    _record(s, gate, "copied", f"from run {st['run_id']}")
    s["history"][-1].update(from_run=st["run_id"], snapshot=snap_path.name,
                            sha256=hashlib.sha256(snap_path.read_bytes()).hexdigest())
    if hash_from:
        s["history"][-1].update(bundle_hash_from=hash_from)
    if copied.get("bundle_hash"):
        s["history"][-1].update(bundle_hash=copied["bundle_hash"])
    if at == "deploy":
        s["history"][-1].update(source_deploy_choice=snap.get("deploy_choice"), deploy="pending")
    if moved_from:
        s["history"][-1].update(bundle_dir_moved_from=moved_from)
    if code_from:
        s["history"][-1].update(code_snapshot=code_from.name, existing_code=args.existing_code,
                                code_sha256=hashlib.sha256(code_from.read_bytes()).hexdigest())
    save_state(run, s)
    return {**out, **preview, "stage": s["stage"], "next_skill": next_skill(s)}


def _approved_bundle_dir(st: dict, snap: dict) -> tuple[str, Optional[str]]:
    """The approved bundle's folder. A run that was copied or moved keeps the old path in its
    approval snapshots; when the source's state.json points at an existing bundle folder of the
    same name, that one is used — only if it matches a recorded bundle hash (snapshot or state)."""
    snap_dir, state_dir = snap.get("bundle_dir"), st.get("bundle_dir")
    if not state_dir or state_dir == snap_dir:
        return snap_dir, None
    if not (Path(state_dir).name == Path(snap_dir).name and (Path(state_dir) / "workflow.json").is_file()):
        return snap_dir, None
    # Never by folder name alone: the moved folder must match a recorded hash of the approved bundle.
    expected = snap.get("bundle_hash") or st.get("bundle_hash")
    if not expected:
        raise InputError(
            f"run {st['run_id']}'s Gate 4 approval points at {snap_dir}, but its state.json points at "
            f"{state_dir} (was the run copied or moved?). Neither records a bundle hash, so that folder cannot "
            "be confirmed to be the approved bundle by its name alone. To go on: approve Gate 4 again in that "
            f"run (it records the hash), or put the approved bundle back at {snap_dir}, or check the bundle "
            "by hand and record its hash in that run's state.json as bundle_hash (with a bundle_hash_recorded "
            "note saying how)")
    if tree_hash(Path(state_dir)) != expected:
        raise InputError(f"the bundle at {state_dir} (the run's copied or moved bundle) does not match the hash "
                         "recorded for the approved bundle; it was changed after Gate 4 was approved")
    return state_dir, snap_dir


def _deploy_bundle_hash(st: dict, snap: dict, copied: dict) -> tuple[dict, str]:
    """Starting at deploy: the approved bundle and its hash. The hash comes from the Gate 4
    snapshot; runs approved before Gate 4 recorded one may carry it in state.json instead
    (recorded later, with `bundle_hash_recorded` saying how), for the same bundle folder only."""
    bundle = Path(copied.get("bundle_dir") or "")
    if not (bundle / "workflow.json").is_file():
        raise InputError(f"the approved bundle {bundle} is missing (no workflow.json)")
    if copied.get("bundle_hash"):
        how = "the Gate 4 approval snapshot"
    elif st.get("bundle_hash") and st.get("bundle_dir") == copied.get("bundle_dir"):
        copied["bundle_hash"] = st["bundle_hash"]
        how = ("the source run's state.json (recorded after approval: "
               + ((st.get("bundle_hash_recorded") or {}).get("how") or "no note") + ")")
    else:
        raise InputError(f"run {st['run_id']}'s Gate 4 approval has no recorded bundle hash, so a changed "
                         "bundle could not be detected; approve Gate 4 again in that run (or record the "
                         "hash of the approved bundle in its state.json as bundle_hash)")
    if tree_hash(bundle) != copied["bundle_hash"]:
        raise InputError(f"the bundle {bundle} changed after Gate 4 was approved (its hash differs from "
                         f"the one recorded in {how.split(' (')[0]}); it cannot be deployed")
    return copied, how


def _code_from_gate3(args, st: dict, src: Path, history: list, gate2_index: int,
                     spec: WorkflowSpec) -> tuple[WorkflowSpec, Optional[Path]]:
    """Starting generate from a run whose Gate 3 was approved: its code needs a choice.

    keep / rewrite put the code of the latest Gate 3 approval onto the Gate 2 spec (by function
    name); ignore (or no approved code) generates every function from scratch. Never silent.
    """
    mode = args.existing_code
    g3 = [i for i, h in enumerate(history) if h.get("gate") == 3 and h.get("decision") == "approve"
          and i > gate2_index]
    snaps = sorted((src / "history").glob("*-gate3-approve.json"))
    usable = bool(g3) and not _superseded_by(history, g3[-1], 3) and bool(snaps)
    if not usable:
        if mode in ("keep", "rewrite"):
            raise InputError(f"run {st['run_id']} has no approved code to {mode} (its Gate 3 was never "
                             "approved after its last Gate 2 approval, or a later change superseded it); "
                             "leave out --existing-code to generate every function, or start from a "
                             "bundle with --from-bundle <folder> --existing-code keep")
        return spec, None
    code = {n.name: n.code for n in spec_of(json.loads(snaps[-1].read_text())["spec"]).nodes if n.code}
    if mode is None:
        names = [n.name for n in spec.nodes if code.get(n.name)]
        raise NeedsChoice(
            f"run {st['run_id']}'s approved Gate 3 has code for {', '.join(names)}. Say what to do with "
            "it: --existing-code keep (keep it as is; stub-tested only, no subagents — then ask for a "
            "change to one function at Gate 3), --existing-code rewrite (regenerate each function "
            "starting from it) or --existing-code ignore (generate every function from scratch).",
            "existing_code", EXISTING_CODE_OPTIONS, functions=names, flag="--existing-code")
    if mode == "ignore":
        return spec, None
    nodes = [n.model_copy(update={"code": code[n.name]}) if code.get(n.name) else n for n in spec.nodes]
    spec, _, _ = _apply_functions(spec.model_copy(update={"nodes": nodes}), None, mode, "generate")
    return spec, snaps[-1]


def _preview_settings(args) -> dict:
    cat, cat_src = catalog_mod.resolve_catalog_dir(args.catalog_dir, None, args.config_catalog_dir)
    retries, r_src = resolve_setting(args.mga_retries, "CURATE_MGA_RETRIES", args.config_mga_retries,
                                     DEFAULT_MGA_RETRIES, _retries)
    mode, m_src = resolve_setting(args.stubtest_mode, "CURATE_STUBTEST_MODE", args.config_stubtest_mode,
                                  DEFAULT_STUBTEST_MODE, _mode)
    return {"catalog_dir": {"value": str(cat), "source": cat_src},
            "mga_max_retries": {"value": retries, "source": r_src},
            "stubtest_mode": {"value": mode, "source": m_src}}


def _import_args(args, at: str, from_file: str):
    ns = argparse.Namespace(stage=at, from_file=from_file, candidates=args.candidates,
                            functions=args.functions, functions_as=None,
                            existing_code=args.existing_code, review=False,
                            request_file=args.request_file)
    for f in INIT_FLAGS:
        setattr(ns, f, getattr(args, f, None))
    return ns


def _start_via_import(args, at: str, from_file: str, kind: str, label: str) -> dict:
    if not args.dry_run:
        out = cmd_import(_import_args(args, at, from_file))
        run = Path(out["run"])
        s = load_state(run)
        s["history"][-1].update(source_kind=kind, source=label)
        save_state(run, s)
        spec = spec_of(s.get("spec"))
        return {**out, "source": {"kind": kind, "path": label}, "uses": from_file,
                "workflow": spec.name if spec else None,
                "functions": [{"name": n.name, "source": n.source, "has_code": bool(n.code)}
                              for n in (spec.nodes if spec else [])],
                "redoes": _redoes(at)}
    # Dry run: do the same import in a throwaway project, report it, and remove it.
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        ns = _import_args(args, at, from_file)
        ns.project_dir = tmp
        out = cmd_import(ns)
        s = load_state(Path(out["run"]))
        spec = spec_of(s["spec"])
        return {"source": {"kind": kind, "path": label}, "uses": from_file,
                "workflow": spec.name if spec else None,
                "functions": [{"name": n.name, "source": n.source, "has_code": bool(n.code)}
                              for n in (spec.nodes if spec else [])],
                "settings": out["settings"], "warnings": out.get("warnings", []),
                "redoes": _redoes(at)}


def cmd_start(args) -> dict:
    """/curate --start-at: a NEW run from one typed source; the source is never changed."""
    at = args.at
    sources = [n for n, v in (("--from-run", args.from_run), ("--from-bundle", args.from_bundle),
                              ("--from-file", args.from_file), ("--sketch-file", args.sketch_file)) if v]
    if len(sources) != 1:
        raise InputError("give exactly one source: --from-run <run_id>, --from-bundle <folder>, "
                         f"--from-file <file> or --sketch-file <file> (got {sources or 'none'})")
    if args.existing_code and at != "generate":
        raise InputError("--existing-code only applies when starting at generate")
    if args.sketch_file:
        if at != "skeleton":
            raise InputError(f"a sketch starts at skeleton, not {at}")
        out = _start_via_import(args, "skeleton", args.sketch_file, "sketch", args.sketch_file)
    elif args.from_file:
        out = _start_via_import(args, at, args.from_file, "file", args.from_file)
    elif at == "monitor":
        raise InputError("monitoring is not a start point: it watches a run that was deployed — "
                         "/curate-skills:curate-monitor --run <run_id> (or deploy again with --start-at deploy)")
    elif at == "deploy" and not args.from_run:
        raise InputError("deploying starts from a run whose Gate 4 was approved: --start-at deploy "
                         "--from-run <run_id> (a bundle or file has no Gate 4 approval)")
    elif args.from_bundle:
        if at not in ("generate", "emit"):
            raise InputError(f"a bundle can start generate or emit, not {at}: it has no gate history "
                             "(start from the run that built it with --from-run instead)")
        spec_file = Path(args.from_bundle) / "spec.json"
        if not spec_file.is_file():
            raise InputError(f"{args.from_bundle} is not a curate bundle (no spec.json)")
        data = read_json_file(str(spec_file))
        if not any(n.get("code") for n in data.get("nodes") or []):
            raise InputError(f"the bundle's spec.json has no code; start from a spec with --from-file instead")
        out = _start_via_import(args, at, str(spec_file), "bundle", str(Path(args.from_bundle).resolve()))
    else:
        if at == "skeleton":
            raise InputError("starting at skeleton needs a sketch (--sketch or --sketch-file)")
        out = _start_from_run(args, at)
    if args.dry_run:
        return {"ok": True, "dry_run": True, "start_at": at, **out, "summary": start_summary(at, out)}
    return {"ok": True, "start_at": at, **out, "summary": start_summary(at, out)}


def start_summary(at: str, p: dict) -> str:
    """What `start` will use, as markdown for /curate to print before starting."""
    src = p.get("source") or {}
    if src.get("kind") == "run":
        source = f"run `{src['run_id']}` (now at stage `{src['source_stage_now']}`; it will not be changed)"
    else:
        source = f"{src.get('kind')} `{src.get('path')}` (will not be changed)"
    title = f"**Start at {at} — new run `{p['run_id']}`**" if p.get("run_id") else f"**Start at {at} — a new run**"
    lines = [title, "", f"- Source: {source}", f"- Uses: {p.get('uses') or src.get('path')}"]
    fns = p.get("functions") or []
    with_code = [f["name"] for f in fns if f.get("has_code")]
    tested = [f"{f['name']} ({f['stub_test']})" for f in fns if f.get("stub_test")]
    lines.append(f"- Workflow: `{p.get('workflow')}`, {len(fns)} functions — "
                 + (f"with code: {', '.join(with_code)}" if with_code else "no code (it will be generated)"))
    if tested:
        lines.append(f"- Stub-test results copied: {', '.join(tested)}")
    if at == "generate" and fns:
        kept = [f["name"] for f in fns if f.get("has_code") and f.get("source") in ("catalog", "cached")]
        made = [f["name"] for f in fns if f["name"] not in kept]
        lines.append("- Generate: " + "; ".join(x for x in (
            f"keeps {', '.join(kept)} (stub-tested only, no subagent)" if kept else "",
            (f"writes {', '.join(made)} with a subagent each" if made else "")) if x)
            + (" — to change one kept function, ask for it at Gate 3" if kept else ""))
    if p.get("copies"):
        lines.append(f"- Copies: {', '.join(p['copies'])}")
    if p.get("bundle_dir_moved_from"):
        lines.append(f"- Bundle: the source run's state.json points at a copied or moved bundle folder of the "
                     f"same name; using it (the approval snapshot says {p['bundle_dir_moved_from']})")
    if p.get("bundle_hash_from"):
        lines.append(f"- Bundle hash (checked against the bundle now): from {p['bundle_hash_from']}")
    lines.append(f"- Redoes: {' → '.join(p.get('redoes') or [])}")
    st = p.get("settings") or {}
    if st:
        lines.append("- Settings: " + ", ".join(f"{k} = {v['value']} ({v['source']})" for k, v in st.items()))
    lines += [f"- Note: {w}" for w in p.get("warnings") or []]
    return "\n".join(lines)


# ── stages: next / require / begin / end ─────────────────────────────────────

# Fields a stage (and everything after it) produces; cleared on a fresh entry.
DEPLOY_OUTPUTS = ("bundle_hash", "deploy_choice", "deploy", "monitor")
STAGE_OUTPUTS = {
    "skeleton": ("enriched_spec", "enriched_basis", "spec_basis", "candidates", "mga",
                 "validation", "bundle_dir") + DEPLOY_OUTPUTS,
    "reuse": ("enriched_spec", "enriched_basis", "spec_basis", "candidates", "mga",
              "validation", "bundle_dir") + DEPLOY_OUTPUTS,
    "resolve": ("spec_basis", "mga", "validation", "bundle_dir") + DEPLOY_OUTPUTS,
    "generate": ("mga", "validation", "bundle_dir") + DEPLOY_OUTPUTS,
    "emit": ("validation", "bundle_dir") + DEPLOY_OUTPUTS,
    "deploy": ("monitor",),  # what was deployed stays recorded (the deploy log is in <run>/deploy/)
    "monitor": (),
    "finalize": (),
}
_EMPTY = {"mga": {}, "candidates": []}


def stage_index(stage_value: str) -> Optional[int]:
    if stage_value == "done":
        return len(PIPELINE)
    for i, (_, values, _) in enumerate(PIPELINE):
        if stage_value in values:
            return i
    return None


def _current_index(s: dict) -> Optional[int]:
    if s["stage"] == "stopped":
        return stage_index(s.get("stopped_from") or "")
    return stage_index(s["stage"])


def skill_for(name: str) -> str:
    return f"{PLUGIN}:" + next(p[2] for p in PIPELINE if p[0] == name)


def next_skill(s: dict) -> Optional[str]:
    if s["stage"] in ("stopped", "done"):
        return None
    i = stage_index(s["stage"])
    return f"{PLUGIN}:{PIPELINE[i][2]}" if i is not None and i < len(PIPELINE) else None


def _run_cmd(s: dict, name: str) -> str:
    return f"/{skill_for(name)} --run {s['run_id']}"


def _needs_generation(n: FunctionSpec) -> bool:
    return not (n.source in ("catalog", "cached") and n.code)


def require_problems(s: dict, name: str, env=None) -> list[str]:
    """What stage `name` is missing, each with the stage skill that produces it."""
    env = os.environ if env is None else env
    spec = spec_of(s.get("spec"))
    out: list[str] = []
    if name == "skeleton":
        if not (s.get("user_request") or "").strip() and spec is None:
            out.append(f"no sketch — start a run with /{skill_for('skeleton')} --from <sketch file>")
        return out
    if spec is None or not spec.nodes:
        out.append(f"no approved skeleton — run {_run_cmd(s, 'skeleton')}")
        return out
    if name == "reuse":
        return out
    if name == "resolve":
        enriched = s.get("enriched_spec")
        if not enriched:
            out.append(f"no reuse decisions for this skeleton — run {_run_cmd(s, 'reuse')}")
        elif s.get("enriched_basis") != spec_hash(s["spec"]) and s.get("spec_basis") != spec_hash(enriched):
            out.append("the reuse decisions were made for an older version of the spec — run "
                       f"{_run_cmd(s, 'reuse')} again")
        return out
    if name == "generate":
        empty = [n.name for n in spec.nodes if not n.inputs and not n.outputs]
        if empty:
            out.append(f"function(s) {', '.join(empty)} have no inputs or outputs yet — run "
                       f"{_run_cmd(s, 'resolve')}")
        errors = [p["message"] for p in check_spec(spec) if p["severity"] == "error"]
        if errors:
            out.append(f"the workflow spec has errors (fix them at Gate 2 with {_run_cmd(s, 'resolve')}): "
                       + "; ".join(errors))
        undescribed = [n.name for n in spec.nodes if _needs_generation(n) and not n.description.strip()]
        if undescribed:
            out.append(f"function(s) {', '.join(undescribed)} need a description to be generated — "
                       f"add one at Gate 2 ({_run_cmd(s, 'resolve')})")
        enriched = s.get("enriched_spec")
        if enriched and s.get("spec_basis") and s["spec_basis"] != spec_hash(enriched):
            out.append("the spec was not resolved from the current reuse decisions — run "
                       f"{_run_cmd(s, 'resolve')}")
        if any(_needs_generation(n) for n in spec.nodes) and env.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS") != "1":
            out.append(preflight_error())
        return out
    if name == "emit":
        no_code = [n.name for n in spec.nodes if not n.code]
        if no_code:
            out.append(f"function(s) {', '.join(no_code)} have no code — run {_run_cmd(s, 'generate')}")
        nodes = {n["name"]: n for n in s["spec"]["nodes"]}
        stale = [k for k, m in (s.get("mga") or {}).items()
                 if m.get("basis") and k in nodes and m["basis"] != node_basis(nodes[k])]
        if stale:
            out.append(f"function(s) {', '.join(stale)} were generated for an older version of the "
                       f"spec — run {_run_cmd(s, 'generate')} again")
        return out
    if name == "deploy":
        hist = s.get("history") or []
        approvals = [i for i, h in enumerate(hist) if h.get("gate") == 4 and h.get("decision") in ("approve", "copied")]
        if not approvals or _superseded_by(hist, approvals[-1], 4):
            out.append(f"Gate 4 has not been approved for the current bundle — run {_run_cmd(s, 'emit')}")
        elif s.get("deploy_choice") != "yes" and hist[approvals[-1]].get("deploy") == "pending":
            out.append('the user has not confirmed starting this run yet ("Start this run?" → Yes, then '
                       f"spec.py confirm-start --run {s['run_id']})")
        elif s.get("deploy_choice") != "yes":
            out.append("Gate 4 was approved without deploying; to deploy this bundle, start a new run "
                       f"from it: /{PLUGIN}:curate --start-at deploy --from-run {s['run_id']}")
        v = s.get("validation") or {}
        if not v.get("ok") or v.get("spec_hash") != spec_hash(s["spec"]):
            out.append(f"no validated bundle for the current spec — run {_run_cmd(s, 'emit')}")
        if not s.get("bundle_hash"):
            out.append(f"no bundle hash was recorded at Gate 4 — approve Gate 4 again ({_run_cmd(s, 'emit')})")
        return out
    if name == "monitor":
        dep = s.get("deploy") or {}
        if not (dep.get("steps") or {}).get("invoke", {}).get("ok") or not dep.get("invocation_id"):
            out.append(f"nothing to monitor: the workflow was not started — run {_run_cmd(s, 'deploy')}")
        return out
    if name == "finalize":
        if s.get("deploy_choice") == "yes" and s.get("deploy"):
            mon = s.get("monitor") or {}
            if mon.get("result") not in ("success", "success_unconfirmed"):
                out.append("the deployed run has not been confirmed to work (monitor result "
                           f"{mon.get('result') or 'none'}) — run {_run_cmd(s, 'monitor')}")
        v = s.get("validation") or {}
        if not v.get("ok"):
            out.append(f"no validated bundle — run {_run_cmd(s, 'emit')}")
        elif v.get("spec_hash") != spec_hash(s["spec"]):
            out.append(f"the bundle is older than the spec — run {_run_cmd(s, 'emit')} again")
        return out
    raise InputError(f"unknown stage {name!r}")


def cmd_require(args) -> dict:
    s = load_state(args.run)
    problems = require_problems(s, args.stage)
    return {"ok": not problems, "stage": args.stage, "run_stage": s["stage"], "problems": problems,
            "next_skill": next_skill(s)}


def cmd_next(args) -> dict:
    s = load_state(args.run)
    skill = next_skill(s)
    out = {"ok": True, "stage": s["stage"], "next_skill": skill,
           "command": f"/{skill} --run {s['run_id']}" if skill else None}
    if s["stage"] == "stopped":
        out.update(resume_commands(s))
    return out


def _clear_from(s: dict, name: str) -> list[str]:
    cleared = [f for f in STAGE_OUTPUTS[name] if s.get(f) not in (None, {}, [])]
    for f in STAGE_OUTPUTS[name]:
        s[f] = _EMPTY.get(f, None) if f in _EMPTY else None
    return cleared


def cmd_begin(args) -> dict:
    """Start (or resume, or rewind to) a stage. Every stage skill calls this first."""
    run = Path(args.run)
    s = load_state(run)
    name = args.stage
    target = STAGE_NAMES.index(name)
    cur = _current_index(s)
    if cur is None:
        raise InputError(f"run is at stage {s['stage']!r}, which no stage skill owns")
    first_value = PIPELINE[target][1][0]

    if cur < target:
        here = f"/{next_skill(s) or skill_for(STAGE_NAMES[cur])} --run {s['run_id']}"
        if s["stage"].startswith("gate"):
            n = s["stage"][4:]
            how = f"it is waiting at Gate {n}. Approve Gate {n} first: {here}"
        else:
            how = f"it is at stage {s['stage']}. Run {here} first"
        return {"ok": False, "mode": "refused", "stage": s["stage"],
                "error": f"the run has not reached {name} yet: {how}"}

    if cur > target:
        cost = (f"going back to {name} uses one revision round ({s['iteration']} of "
                f"{s['max_iterations']} used) and redoes {name} and every stage after it")
        if not args.rewind:
            return {"ok": False, "mode": "refused", "stage": s["stage"], "needs_confirmation": True,
                    "error": f"the run is already past {name} (stage {s['stage']}). {cost[0].upper()}"
                             f"{cost[1:]}. Ask the user first; only if they agree, run begin again "
                             "with --rewind --confirmed"}
        if s["iteration"] >= s["max_iterations"]:
            return {"ok": False, "mode": "refused", "cap_reached": True, "stage": s["stage"],
                    "error": f"revision cap reached ({s['iteration']}/{s['max_iterations']}); "
                             "raise it with set-cap to rewind"}
        if not args.confirmed:
            # A rewind is the user's decision: the gate routine asks, then passes --confirmed.
            return {"ok": False, "mode": "refused", "stage": s["stage"], "needs_confirmation": True,
                    "error": f"rewinding needs the user's explicit OK: {cost}. Ask them; only if they "
                             "say yes, run begin again with --rewind --confirmed"}
        spec = spec_of(s.get("spec"))
        if spec is not None and any(n.code for n in spec.nodes):
            s["prior_spec"] = json.loads(json.dumps(s["spec"]))  # a copy: working code for [cached]
            if target <= STAGE_NAMES.index("generate"):
                # Functions marked new are regenerated from scratch: their old code stays only in
                # prior_spec (for later [cached] decisions), never in the spec or the folders.
                s["spec"]["nodes"] = [
                    {**n, "code": None} if n.get("source") == "new" else n for n in s["spec"]["nodes"]]
        s["iteration"] += 1
        if name == "skeleton":
            s["skeleton_draft"] = None
        _record(s, "rewind", "rewind", f"to {name} from {s['stage']}")
        mode = "rewind"
        cleared = _clear_from(s, name)
        s["stage"] = first_value
    elif s["stage"] == "stopped":
        s["stage"] = s.get("stopped_from") or first_value
        mode, cleared = "resume", []
    elif s.get("begun") == name or s["stage"].startswith("gate"):
        # Already started, or waiting at this stage's gate (its work is done).
        mode, cleared = "resume", []
    else:
        mode = "fresh"
        cleared = _clear_from(s, name)

    # Resuming at a gate only re-shows it: the stage's work is done, so its inputs are
    # not re-checked (an imported --review run may never have had them).
    at_gate = mode == "resume" and s["stage"].startswith("gate")
    problems = [] if at_gate else require_problems(s, name)
    if problems:
        return {"ok": False, "mode": mode, "stage": s["stage"], "problems": problems,
                "error": "; ".join(problems)}
    s["begun"] = name
    if mode == "rewind":
        snapshot_history(run, s, f"rewind-{name}")
    save_state(run, s)
    return {"ok": True, "mode": mode, "stage": s["stage"], "cleared": cleared,
            "iteration": s["iteration"], "next_skill": next_skill(s)}


def cmd_end(args) -> dict:
    run = Path(args.run)
    s = load_state(run)
    if args.stage != "generate":
        raise InputError("only the generate stage has an explicit end")
    spec = spec_of(s.get("spec"))
    mga = s.get("mga") or {}
    pending = [n.name for n in (spec.nodes if spec else []) if (mga.get(n.name) or {}).get("status") not in FINAL_MGA]
    if pending:
        return {"ok": False, "pending": pending,
                "error": f"function(s) {', '.join(pending)} have no final status yet; finish them first"}
    s["stage"] = "gate3"
    save_state(run, s)
    return {"ok": True, "stage": "gate3",
            "summary": {k: v.get("status") for k, v in mga.items()}}


# ── gate displays ────────────────────────────────────────────────────────────

def dag_lines(spec: WorkflowSpec) -> list[str]:
    rank = {n.name: n.rank for n in spec.nodes}

    def label(n):
        return f"{n} ×{rank[n]}" if rank.get(n, 1) > 1 else n

    lines = []
    for n in topo_order(spec):
        outs = [e for e in spec.edges if e.from_node == n]
        for e in outs:
            cond = f" [if {e.condition}]" if e.condition else ""
            lines.append(f"  {label(n)} → {label(e.to_node)}{cond}")
        if not outs and not any(e.to_node == n for e in spec.edges):
            lines.append(f"  {label(n)}")
    return lines


def _io_names(items) -> str:
    return ", ".join(f"`{i.name}`" for i in items) or "—"


def _origin(n: FunctionSpec) -> str:
    if n.origin_kind == "import":  # code the user brought with --from / --functions
        return {"cached": "kept: your code", "adapt": "rewritten from your code"}.get(
            n.source, f"[{n.source}] ← your code")
    tag = SOURCE_TAGS.get(n.source, f"[{n.source}]")
    if n.source == "user_provided" and n.user_model_mode:
        tag = f"[User Provided Model — {n.user_model_mode}]"
    if n.origin_kind:
        tag += f" ← [{n.origin_kind}] {n.origin_name}"
    return tag


def _last_line(text: Optional[str], limit: int = 240) -> str:
    """Last meaningful line of a stub-test tail, for a one-line gate note."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip() and not l.startswith("===")]
    if not lines:
        return "(no output)"
    line = lines[-1]
    return line if len(line) <= limit else "…" + line[-limit:]


# Plainer wording for the Gate 3 "Stub test" column (state keeps the short status).
STATUS_LABELS = {"reused": "passed (not regenerated)", "reused_failed": "FAILED (not regenerated)",
                 "external_only": "passed except an external fetch"}


def resume_commands(s: dict) -> dict:
    """How to pick a stopped run up again."""
    stopped_from = s.get("stopped_from") or ""
    i = stage_index(stopped_from)
    out = {"resume_command": f"/curate-skills:curate --resume {s['run_id']}",
           "stopped_from": stopped_from}
    if i is not None and i < len(PIPELINE):
        out["resume_stage_command"] = f"/{PLUGIN}:{PIPELINE[i][2]} --run {s['run_id']}"
    return out


def show_gate(s: dict, gate: int) -> str:
    out: list[str] = []
    if gate == 1:
        draft = s.get("skeleton_draft") or {}
        spec = spec_of(draft.get("spec"))
        out.append("## Gate 1 — Skeleton")
        if draft.get("parse_error"):
            out += ["", f"> {draft['parse_error']}"]
        if spec is None:
            return "\n".join(out + ["", "No valid skeleton yet."])
        out += ["", f"**{spec.name}** — {spec.description}", "",
                "| # | Function | Parallel copies | What it does |", "|---|---|---|---|"]
        for i, name in enumerate(topo_order(spec), 1):
            n = next(x for x in spec.nodes if x.name == name)
            out.append(f"| {i} | `{n.name}` | {n.rank} | {n.description} |")
        out += ["", "```", *dag_lines(spec), "```"]
        return "\n".join(out)

    spec = spec_of(s.get("spec"))
    if spec is None:
        return f"## Gate {gate}\n\nNo spec yet."
    if gate == 2:
        out += [f"## Gate 2 — Candidates and data flow  (iteration {s['iteration']}/{s['max_iterations']})",
                "", f"**{spec.name}** — {spec.description}", "",
                "| Function | Reuse decision | Parallel copies | Inputs | Outputs | Packages | Secrets |",
                "|---|---|---|---|---|---|---|"]
        for name in topo_order(spec):
            n = next(x for x in spec.nodes if x.name == name)
            out.append(f"| `{n.name}` | {_origin(n)} | {n.rank} | {_io_names(n.inputs)} | "
                       f"{_io_names(n.outputs)} | {', '.join(n.dependencies) or '—'} | "
                       f"{', '.join(n.secrets) or '—'} |")
        out += ["", "```", *dag_lines(spec), "```"]
        if spec.data_flow:
            out += ["", "Data flow:"] + [f"- `{d.file}`: {d.from_node} → {d.to_node}" for d in spec.data_flow]
        ext = external_inputs(spec)
        out += ["", "Files you must provide (S3):"] + (
            [f"- `{e['s3_path']}` — {e['description']}" for e in ext] or ["- none"])
        if s.get("repairs"):
            out += ["", "Automatic repairs:"] + [f"- {r}" for r in s["repairs"]]
        problems = check_spec(spec)
        if problems:
            out += ["", "Check:"] + [f"- **{p['severity']}**: {p['message']}" for p in problems]
        return "\n".join(out)
    if gate == 3:
        out += [f"## Gate 3 — Code  (iteration {s['iteration']}/{s['max_iterations']}, "
                f"stub tests: {s['stubtest_mode']})", "",
                "| Function | Source | Stub test | Attempts | Lines | File |", "|---|---|---|---|---|---|"]
        mga = s.get("mga") or {}
        for name in topo_order(spec):
            n = next(x for x in spec.nodes if x.name == name)
            m = mga.get(name, {})
            lines = len((n.code or "").splitlines())
            path = f"functions/{name}/{name}.py"
            out.append(f"| `{name}` | {_origin(n)} | {STATUS_LABELS.get(m.get('status'), m.get('status') or 'pending')} | "
                       f"{m.get('attempts', 0)} | {lines} | `{path}` |")
        warn = [k for k, v in mga.items() if v.get("status") in ("failed", "external_only")]
        if warn:
            out += ["", "Needs attention:"] + [f"- `{k}`: {mga[k]['status']} — {_last_line(mga[k].get('last_output_tail'))}"
                                               for k in warn]
        for name in topo_order(spec):
            n = next(x for x in spec.nodes if x.name == name)
            m = mga.get(name, {})
            kept = m.get("status") in ("reused", "reused_failed")
            out += ["", f"### `{name}` — " + ("kept (not regenerated); " if kept else "")
                    + f"stub test: {STATUS_LABELS.get(m.get('status'), m.get('status') or 'pending')}"]
            if m.get("status") in ("failed", "reused_failed", "external_only") and m.get("last_output_tail"):
                out.append(f"Last line of its stub test: {_last_line(m.get('last_output_tail'))}")
            out += ["", "```python", (n.code or "# (no code)").rstrip("\n"), "```"]
        return "\n".join(out)
    if gate == 4:
        v = s.get("validation") or {}
        out += ["## Gate 4 — Bundle", "", f"**{spec.name}** — {spec.description}", ""]
        for i, name in enumerate(topo_order(spec), 1):
            n = next(x for x in spec.nodes if x.name == name)
            out.append(f"{i}. `{n.name}`" + (f" ×{n.rank} (parallel copies)" if n.rank > 1 else "")
                       + f" — {_cut(n.description, 120)}")
        out += ["", "```", *dag_lines(spec), "```", "", f"- bundle: `{s.get('bundle_dir')}`",
                f"- validation: {'passed' if v.get('ok') else 'FAILED'} (level `{v.get('level')}`)"]
        if v.get("error"):
            out.append(f"- error: {v['error']}")
        missing = [n.name for n in spec.nodes if not n.code]
        if missing:
            out.append(f"- **functions without code (not in the bundle): {', '.join(missing)}**")
        failed = [k for k, m in (s.get("mga") or {}).items() if m.get("status") in ("failed", "reused_failed")]
        if failed:
            out.append(f"- functions whose stub test failed: {', '.join(failed)}")
        bundle_readme = Path(s["bundle_dir"]) / "README.md" if s.get("bundle_dir") else None
        if bundle_readme and bundle_readme.exists():
            holders = sorted(set(re.findall(r"<FAASR_[A-Z0-9_]+>", bundle_readme.read_text())))
            if holders:
                out.append(f"- placeholders to fill before deploying: {', '.join(holders)}")
        names = sorted({x for n in spec.nodes for x in n.secrets} | {"GH_PAT", "S3_AccessKey", "S3_SecretKey"})
        out += ["", "Secrets to set before deploying (names only):"] + [f"- `{x}`" for x in names]
        ext = external_inputs(spec)
        out += ["", "Files to upload before running:"] + (
            [f"- `{e['s3_path']}` — {e['description']}" for e in ext] or ["- none"])
        out += ["", "Files in the bundle:"] + (bundle_files(s.get("bundle_dir")) or ["- none yet"])
        return "\n".join(out)
    if gate == 5:
        return "\n".join([summary_md(s), "", *save_preview(s)])
    raise InputError("--gate must be 1-5")


BUNDLE_SKIP = re.compile(r"(^|/)(\.DS_Store|Thumbs\.db|desktop\.ini|\._[^/]*|__pycache__(/.*)?)$")


def bundle_files(bundle_dir) -> list[str]:
    root = Path(bundle_dir) if bundle_dir else None
    if not root or not root.is_dir():
        return []
    files = sorted(p for p in root.rglob("*") if p.is_file() and not BUNDLE_SKIP.search(p.relative_to(root).as_posix()))
    return [f"- `{p.relative_to(root).as_posix()}` ({p.stat().st_size:,} bytes)" for p in files]


def save_preview(s: dict) -> list[str]:
    """Gate 5: what saving would store (the catalog's own dry runs; nothing is written)."""
    out = ["### What would be saved", "", f"Catalog: `{s.get('catalog_dir')}`"]
    try:
        C = catalog_mod
        spec = spec_of(s.get("spec"))
        cat = C.Catalog(Path(s["catalog_dir"]).expanduser())
        fns = C.save_functions(cat, spec, s, only_verified=True, dry_run=True)
        wf = C.save_workflow(cat, spec, dry_run=True)
        blocked = C.deployed_run_problem(s)
    except Exception as e:  # noqa: BLE001 — the preview is informational; finalize runs the real dry runs
        return out + [f"- (could not preview: {type(e).__name__}: {e})"]
    out += ["", "Functions (only verified ones are saved):"]
    out += [f"- `{x['name']}` — would be stored (keywords: {', '.join(x['keywords'])})" for x in fns["stored"]]
    out += [f"- `{x['name']}` — skipped: {x['reason']}" for x in fns["skipped"]]
    out += ["", f"Workflow `{spec.name}`: " + ("would update the entry with the same name" if wf.get("updated")
                                             else "would be a new entry")]
    if blocked:
        out += ["", f"**Saving is refused for now:** {blocked}"]
    return out


def summary_md(s: dict) -> str:
    """The finalize report (local stand-in for the original's post-run WDA summary)."""
    spec = spec_of(s.get("spec"))
    if spec is None:
        raise InputError("no spec yet")
    mga = s.get("mga") or {}
    v = s.get("validation") or {}
    out = [f"## Run summary — {spec.name}", "", spec.description or "", "",
           f"- run: `{s['run_id']}` (stage `{s['stage']}`, {s['iteration']} revision round(s))",
           f"- settings: catalog `{s['catalog_dir']}`, MGA retries {s['mga_max_retries']}, "
           f"stub tests `{s['stubtest_mode']}`",
           f"- bundle: `{s.get('bundle_dir')}` — validation "
           f"{'passed' if v.get('ok') else 'not passed'} (level `{v.get('level')}`)", "",
           "| Function | Source | Rank | Stub test | Attempts | Lines | Deps | Secrets |",
           "|---|---|---|---|---|---|---|---|"]
    for name in topo_order(spec):
        n = next(x for x in spec.nodes if x.name == name)
        m = mga.get(name, {})
        out.append(f"| `{name}` | {_origin(n)} | {n.rank} | {m.get('status', '—')} | "
                   f"{m.get('attempts', 0)} | {len((n.code or '').splitlines())} | "
                   f"{', '.join(n.dependencies) or '—'} | {', '.join(n.secrets) or '—'} |")
    out += ["", "```", *dag_lines(spec), "```"]
    ext = external_inputs(spec)
    out += ["", "Files to upload before running:"] + (
        [f"- `{e['s3_path']}` — {e['description']}" for e in ext] or ["- none"])
    mon = s.get("monitor") or {}
    if mon.get("summary"):
        out += ["", mon["summary"]]
        if mon.get("result") == "success_unconfirmed":
            out.append("- Unconfirmed match " + ("**accepted** by the user" if mon.get("accepted")
                                                 else "**not accepted yet** (Gate 5 asks first)"))
    hist = s.get("history") or []
    out += ["", "Decisions:"] + ([
        f"- Gate {h['gate']}: {h['decision']}" + (f" — {h['text']}" if h.get("text") else "")
        + (" (structural)" if h.get("structural") else "")
        for h in hist] or ["- none"])
    return "\n".join(out)


def cmd_summary(args) -> dict:
    return {"ok": True, "markdown": summary_md(load_state(args.run))}


def _cut(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def gate_compact(s: dict, gate: int) -> str:
    """A short plain-text version of the gate display, for the question's preview."""
    if gate == 1:
        spec = spec_of((s.get("skeleton_draft") or {}).get("spec"))
        if spec is None:
            return "No valid skeleton yet."
        lines = [f"{spec.name}"]
        for i, name in enumerate(topo_order(spec), 1):
            n = next(x for x in spec.nodes if x.name == name)
            lines.append(f"{i}. {n.name}" + (f" ×{n.rank}" if n.rank > 1 else "") + f" — {_cut(n.description, 70)}")
        return "\n".join(lines + ["", *[l.strip() for l in dag_lines(spec)]])
    spec = spec_of(s.get("spec"))
    if spec is None:
        return "No spec yet."
    order = [next(x for x in spec.nodes if x.name == nm) for nm in topo_order(spec)]
    if gate == 2:
        lines = [f"{spec.name} (revision {s['iteration']}/{s['max_iterations']})"]
        for n in order:
            io = f"{', '.join(i.name for i in n.inputs) or '—'} → {', '.join(o.name for o in n.outputs) or '—'}"
            lines.append(f"{n.name}" + (f" ×{n.rank}" if n.rank > 1 else "") + f" | {_origin(n)} | {io}")
        errors = sum(p["severity"] == "error" for p in check_spec(spec))
        return "\n".join(lines + ([f"! {errors} check error(s)"] if errors else []))
    if gate == 3:
        mga = s.get("mga") or {}
        lines = [f"{spec.name} (revision {s['iteration']}/{s['max_iterations']})"]
        for n in order:
            m = mga.get(n.name, {})
            status = STATUS_LABELS.get(m.get("status"), m.get("status") or "pending")
            lines.append(f"{n.name} | {_origin(n)} | {status} | attempts {m.get('attempts', 0)}")
        return "\n".join(lines)
    if gate == 5:
        mga = s.get("mga") or {}
        return "\n".join([f"{spec.name}", f"run: {s['run_id']}"] + [
            f"{n.name} | {STATUS_LABELS.get((mga.get(n.name) or {}).get('status'), (mga.get(n.name) or {}).get('status') or '—')}"
            for n in order])
    if gate == 4:
        v = s.get("validation") or {}
        missing = [n.name for n in spec.nodes if not n.code]
        return "\n".join([f"{spec.name}", f"bundle: {s.get('bundle_dir')}",
                          f"validation: {'passed' if v.get('ok') else 'FAILED'} ({v.get('level')})"]
                         + ([f"no code: {', '.join(missing)}"] if missing else []))
    raise InputError("--gate must be 1-4")


GATE_QUESTION = {1: "Approve this skeleton (the steps and their order)?",
                 2: "Approve the reuse decisions, files and data flow?",
                 3: "Approve the generated code?",
                 4: "Approve this FaaSr bundle, and deploy it?",
                 5: "Save to the catalog?"}


def cmd_show(args) -> dict:
    """The gate's full content (`markdown`: print it as a message BEFORE asking), also written to
    <run>/review/gate<N>.md (`review_line`: print it after the content)."""
    s = load_state(args.run)
    md = show_gate(s, args.gate)
    review = Path(args.run) / "review" / f"gate{args.gate}.md"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text(md + "\n")
    q = GATE_QUESTION[args.gate] + ("" if args.gate == 5 else ' (Type in "Other" to request changes.)')
    return {"ok": True, "markdown": md, "compact": gate_compact(s, args.gate), "question": q,
            "header": f"Gate {args.gate}", "review_file": str(review), "review_line": f"Full review: {review}"}


# ── CLI ──────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="spec.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def init_flags(q, request_required):
        q.add_argument("--request-file", required=request_required)
        q.add_argument("--user-model", action="append")
        q.add_argument("--context-file", action="append")
        q.add_argument("--catalog-dir")
        q.add_argument("--config-catalog-dir")
        q.add_argument("--mga-retries")
        q.add_argument("--config-mga-retries")
        q.add_argument("--stubtest-mode")
        q.add_argument("--config-stubtest-mode")
        q.add_argument("--project-dir")

    init_flags(sub.add_parser("init"), True)
    sp = sub.add_parser("import")
    sp.add_argument("--stage", required=True, choices=STAGE_NAMES)
    sp.add_argument("--from", dest="from_file", required=True)
    sp.add_argument("--candidates")
    sp.add_argument("--functions")
    sp.add_argument("--existing-code", choices=["keep", "rewrite", "ignore"])
    sp.add_argument("--functions-as", choices=["reuse", "seed"], help=argparse.SUPPRESS)  # old name
    sp.add_argument("--review", action="store_true")
    init_flags(sp, False)
    sp = sub.add_parser("runs")
    sp.add_argument("--project-dir")
    sub.add_parser("preflight")

    def with_run(name):
        q = sub.add_parser(name)
        q.add_argument("--run", required=True)
        return q

    with_run("status")
    q = with_run("prompt-context")
    q.add_argument("--stage", required=True, choices=["skeleton", "reuse", "mca", "resolve"])
    q.add_argument("--k", type=int, default=5)
    for name in ("skeleton", "apply-candidates", "merge-resolved"):
        with_run(name).add_argument("--json", required=True)
    with_run("check")
    with_run("toposort")
    q = with_run("approve")
    q.add_argument("--gate", type=int, required=True, choices=[1, 2, 3, 4])
    q.add_argument("--deploy", choices=["yes", "no"],
                   help="Gate 4 only: the user chose Approve & deploy (yes) or Approve, no deploy (no; default)")
    q = with_run("directive")
    q.add_argument("--gate", required=True, choices=["1", "2", "3", "4", "emit"])
    q.add_argument("--text", required=True)
    q.add_argument("--structural")
    with_run("set-cap").add_argument("--max", type=int, required=True)
    with_run("confirm-start")
    with_run("set-stage").add_argument("--stage", required=True, choices=STAGES)
    with_run("show").add_argument("--gate", type=int, required=True, choices=[1, 2, 3, 4, 5])
    with_run("summary")
    sp = sub.add_parser("start")
    sp.add_argument("--at", required=True, choices=STAGE_NAMES)
    sp.add_argument("--from-run")
    sp.add_argument("--from-bundle")
    sp.add_argument("--from-file")
    sp.add_argument("--sketch-file")
    sp.add_argument("--candidates")
    sp.add_argument("--functions")
    sp.add_argument("--existing-code", choices=["keep", "rewrite", "ignore"])
    sp.add_argument("--dry-run", action="store_true")
    init_flags(sp, False)
    with_run("next")
    with_run("require").add_argument("--stage", required=True, choices=STAGE_NAMES)
    q = with_run("begin")
    q.add_argument("--stage", required=True, choices=STAGE_NAMES)
    q.add_argument("--rewind", action="store_true")
    q.add_argument("--confirmed", action="store_true",
                   help="the user explicitly agreed to rewind (passed only after asking them)")
    with_run("end").add_argument("--stage", required=True, choices=["generate"])
    return p


COMMANDS = {
    "init": cmd_init, "runs": cmd_runs, "preflight": cmd_preflight, "status": cmd_status, "prompt-context": cmd_prompt_context,
    "skeleton": cmd_skeleton, "apply-candidates": cmd_apply_candidates,
    "merge-resolved": cmd_merge_resolved, "check": cmd_check, "toposort": cmd_toposort,
    "approve": cmd_approve, "directive": cmd_directive, "set-cap": cmd_set_cap,
    "set-stage": cmd_set_stage, "confirm-start": cmd_confirm_start, "show": cmd_show, "summary": cmd_summary,
    "import": cmd_import, "start": cmd_start, "next": cmd_next, "require": cmd_require, "begin": cmd_begin,
    "end": cmd_end,
}


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # One lock per run around the whole command: every read-modify-write of
        # state.json happens inside it.
        with run_lock(getattr(args, "run", None) or "/nonexistent"):
            result = COMMANDS[args.cmd](args)
    except (InputError, OSError, ValidationError) as e:
        print(json.dumps({"ok": False, "error": str(e), **getattr(e, "details", {})}, indent=2))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
