# /// script
# requires-python = "==3.13.*"
# dependencies = ["pydantic==2.11.7", "rank-bm25==0.2.2"]
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Derived from CURATE-prototype src/faasr_agents/catalog/store.py,
# src/faasr_agents/catalog/workflow_store.py and src/faasr_agents/orchestrator.py
# (_do_save) (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). See scripts/VENDORED.md.
"""CURATE module catalog: BM25 search, save, import/export.

Catalog dir, first match wins:
  --catalog-dir  >  run's pinned state.catalog_dir (--run)  >  $CURATE_CATALOG_DIR
  >  --config-catalog-dir (plugin setting)  >  ~/.curate/catalog
Layout: <dir>/functions/<uuid>.json and <dir>/workflows/<uuid>.json.

Usage (prints JSON {"ok": ...}; exit 0 ok, 1 check failed, 2 bad input):
  catalog.py where | init | list [--workflows]
  catalog.py search --query Q [--k 5]
  catalog.py search-nodes --run DIR [--k 5]
  catalog.py get --id ID [--workflow]
  catalog.py increment-usage --id ID [--workflow]
  catalog.py save-functions (--run DIR [--only-verified] | --spec FILE) [--dry-run]
  catalog.py save-workflow  (--run DIR | --spec FILE) [--dry-run]
  catalog.py import PATH... [--dedupe-code] [--overwrite]
  catalog.py export --out DIR [--ids ID...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

from pydantic import ValidationError
from rank_bm25 import BM25Okapi

from curate_models import (
    CATALOG_EXCLUDED_SPEC_FIELDS,
    CatalogEntry,
    WorkflowEntry,
    WorkflowSpec,
)

DEFAULT_CATALOG_DIR = Path.home() / ".curate" / "catalog"
VERIFIED_STATUSES = {"passed", "reused"}


class InputError(Exception):
    pass


# ── catalog dir resolution ───────────────────────────────────────────────────

def _usable(value: Optional[str]) -> bool:
    # An unset plugin setting may arrive empty or as the unexpanded "${...}" literal.
    return bool(value and value.strip() and "${" not in value)


def resolve_catalog_dir(catalog_dir: Optional[str] = None, run: Optional[str] = None,
                        config_dir: Optional[str] = None) -> tuple[Path, str]:
    if _usable(catalog_dir):
        return Path(catalog_dir).expanduser().resolve(), "--catalog-dir"
    if run:
        state_path = Path(run) / "state.json"
        if state_path.exists():
            pinned = json.loads(state_path.read_text()).get("catalog_dir")
            if _usable(pinned):
                return Path(pinned).expanduser().resolve(), "run state"
    env = os.environ.get("CURATE_CATALOG_DIR")
    if _usable(env):
        return Path(env).expanduser().resolve(), "CURATE_CATALOG_DIR"
    if _usable(config_dir):
        return Path(config_dir).expanduser().resolve(), "plugin setting"
    return DEFAULT_CATALOG_DIR, "default"


# ── loading (from catalog/store.py, workflow_store.py) ───────────────────────

def _load_entry_file(p: Path):
    """Parse one catalog or workflow entry file; None if it isn't one."""
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if "workflow_spec" in data:
        try:
            return WorkflowEntry.model_validate(data)
        except ValidationError:
            return None
    if "function_spec" in data:
        # Recover code from a companion .py if the JSON has none (original's migration path).
        if not data["function_spec"].get("code"):
            py_file = p.with_suffix(".py")
            if py_file.exists():
                lines = py_file.read_text().splitlines()
                start = next((i for i, l in enumerate(lines) if not l.startswith("#")), 0)
                data["function_spec"]["code"] = "\n".join(lines[start:]).strip()
        try:
            return CatalogEntry.model_validate(data)
        except ValidationError:
            return None
    return None


class Catalog:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.fn_dir = self.root / "functions"
        self.wf_dir = self.root / "workflows"
        self.functions: dict[str, CatalogEntry] = {}
        self.workflows: dict[str, WorkflowEntry] = {}
        for d, store, kind in ((self.fn_dir, self.functions, CatalogEntry),
                               (self.wf_dir, self.workflows, WorkflowEntry)):
            if d.is_dir():
                for p in sorted(d.glob("*.json")):
                    e = _load_entry_file(p)
                    if isinstance(e, kind):
                        store[e.id] = e

    def init(self) -> None:
        self.fn_dir.mkdir(parents=True, exist_ok=True)
        self.wf_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(text)
        tmp.replace(path)

    def save_function(self, entry: CatalogEntry) -> None:
        self.functions[entry.id] = entry
        self._write(self.fn_dir / f"{entry.id}.json", entry.model_dump_json(
            indent=2, exclude={"function_spec": CATALOG_EXCLUDED_SPEC_FIELDS}))

    def save_workflow(self, entry: WorkflowEntry) -> None:
        self.workflows[entry.id] = entry
        self._write(self.wf_dir / f"{entry.id}.json", entry.model_dump_json(
            indent=2, exclude={"workflow_spec": {"nodes": {"__all__": CATALOG_EXCLUDED_SPEC_FIELDS}}}))

    def workflow_by_name(self, name: str) -> Optional[WorkflowEntry]:
        return next((e for e in self.workflows.values() if e.workflow_spec.name == name), None)

    # ── search (from catalog/store.py) ──
    def search(self, query: str, k: int = 5) -> list[tuple[float, CatalogEntry]]:
        if not self.functions:
            return []
        entries = list(self.functions.values())
        corpus = []
        for e in entries:
            spec = e.function_spec
            corpus.append(
                spec.name.split("_")
                + spec.description.lower().split()
                + e.keywords
                + [inp.name for inp in spec.inputs]
                + [out.name for out in spec.outputs]
            )
        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(query.lower().split())
        ranked = sorted(zip(scores, entries), key=lambda x: x[0], reverse=True)
        return [(float(score), entry) for score, entry in ranked[:k] if score > 0]


def node_query(node) -> str:
    """Query for one workflow node, as built in agents/fca.py."""
    name_tokens = node.name.replace("_", " ")
    return (f"{name_tokens} {node.description} {' '.join(i.name for i in node.inputs)} "
            f"{' '.join(o.name for o in node.outputs)}")


def hit_json(score: float, e: CatalogEntry) -> dict:
    s = e.function_spec
    return {
        "id": e.id, "name": s.name, "score": round(score, 4), "description": s.description,
        "inputs": [i.name for i in s.inputs], "outputs": [o.name for o in s.outputs],
        "usage_count": e.usage_count, "provenance": e.provenance,
        # The line format the MCA prompt expects (agents/fca.py).
        "line": f"[{e.id}] {s.name}: {s.description} | inputs={[i.name for i in s.inputs]} "
                f"| outputs={[o.name for o in s.outputs]}",
    }


def entry_summary(e) -> dict:
    if isinstance(e, WorkflowEntry):
        w = e.workflow_spec
        return {"id": e.id, "kind": "workflow", "name": w.name, "description": w.description,
                "nodes": [n.name for n in w.nodes], "usage_count": e.usage_count}
    s = e.function_spec
    return {"id": e.id, "kind": "function", "name": s.name, "description": s.description,
            "rank": s.rank, "dependencies": s.dependencies, "secrets": s.secrets,
            "usage_count": e.usage_count, "provenance": e.provenance}


# ── save (from orchestrator.py _do_save) ─────────────────────────────────────

def load_run(run: Optional[str], spec_file: Optional[str]) -> tuple[WorkflowSpec, dict]:
    if spec_file:
        data, state = json.loads(Path(spec_file).read_text()), {}
    elif run:
        state = json.loads((Path(run) / "state.json").read_text())
        data = state.get("spec")
        if not data:
            raise InputError(f"{run}/state.json has no 'spec' yet")
    else:
        raise InputError("pass --run DIR or --spec FILE")
    try:
        return WorkflowSpec.model_validate(data), state
    except ValidationError as e:
        raise InputError(f"invalid WorkflowSpec:\n{e}") from e


def save_functions(cat: Catalog, spec: WorkflowSpec, state: dict, only_verified: bool,
                   dry_run: bool = False) -> dict:
    stored, skipped = [], []
    mga = state.get("mga") or {}
    existing_codes = {e.function_spec.code.strip() for e in cat.functions.values()
                      if e.function_spec.code}
    for node in spec.nodes:
        if not node.code:
            skipped.append({"name": node.name, "reason": "no code"})
            continue
        if only_verified and (mga.get(node.name) or {}).get("status") not in VERIFIED_STATUSES:
            skipped.append({"name": node.name, "reason": "not verified"})
            continue
        # Only new/adapted implementations enter the catalog. Verbatim catalog reuse
        # is already there; byte-identical code (cached copies, re-saves) is skipped.
        if node.source == "catalog" or node.code.strip() in existing_codes:
            skipped.append({"name": node.name, "reason": "already in catalog"})
            continue
        entry = CatalogEntry(
            id=str(uuid.uuid4()),
            function_spec=node,
            keywords=node.name.split("_") + node.description.lower().split()[:5],
            provenance=f"workflow:{spec.name}",
        )
        if not dry_run:
            cat.save_function(entry)
        stored.append({"name": node.name, "id": None if dry_run else entry.id,
                       "keywords": entry.keywords})
        existing_codes.add(node.code.strip())
    return {"ok": True, "dry_run": dry_run, "stored": stored, "skipped": skipped}


def save_workflow(cat: Catalog, spec: WorkflowSpec, dry_run: bool = False) -> dict:
    existing = cat.workflow_by_name(spec.name)
    if dry_run:
        return {"ok": True, "dry_run": True, "id": existing.id if existing else None,
                "updated": existing is not None}
    entry = WorkflowEntry(
        id=existing.id if existing else str(uuid.uuid4()),
        workflow_spec=spec,
        keywords=spec.name.replace("-", " ").split() + spec.description.lower().split()[:5],
        provenance="user request",
        usage_count=existing.usage_count if existing else 0,
    )
    cat.save_workflow(entry)
    return {"ok": True, "id": entry.id, "updated": existing is not None}


# ── import / export ──────────────────────────────────────────────────────────

def _candidate_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise InputError(f"{path} does not exist")
    files = []
    for d in (path, path / "functions", path / "workflows", path / "data"):
        if d.is_dir():
            files.extend(sorted(d.glob("*.json")))
    return files


def import_entries(cat: Catalog, paths: list[str], dedupe_code: bool, overwrite: bool) -> dict:
    imported, skipped = [], []
    codes = {e.function_spec.code.strip() for e in cat.functions.values() if e.function_spec.code}
    for raw in paths:
        for f in _candidate_files(Path(raw).expanduser()):
            e = _load_entry_file(f)
            if e is None:
                skipped.append({"file": str(f), "reason": "not a catalog entry"})
                continue
            store = cat.workflows if isinstance(e, WorkflowEntry) else cat.functions
            if e.id in store and not overwrite:
                skipped.append({"file": str(f), "id": e.id, "reason": "id exists"})
                continue
            if isinstance(e, CatalogEntry):
                code = (e.function_spec.code or "").strip()
                if dedupe_code and code and code in codes:
                    skipped.append({"file": str(f), "id": e.id, "reason": "identical code"})
                    continue
                codes.add(code)
                cat.save_function(e)
            else:
                cat.save_workflow(e)
            imported.append(entry_summary(e))
    return {"ok": True, "imported": imported, "skipped": skipped}


def export_entries(cat: Catalog, out: Path, ids: Optional[list[str]]) -> dict:
    target = Catalog(out)
    exported = []
    for e in list(cat.functions.values()) + list(cat.workflows.values()):
        if ids and e.id not in ids:
            continue
        if isinstance(e, CatalogEntry):
            target.save_function(e)
        else:
            target.save_workflow(e)
        exported.append(e.id)
    missing = sorted(set(ids or []) - set(exported))
    return {"ok": not missing, "out": str(out.resolve()), "exported": exported, "missing_ids": missing}


def deployed_run_problem(state: dict) -> Optional[str]:
    """A deployed run is saved only after its monitor result is success, or success with an
    unconfirmed match that the user accepted at Gate 5."""
    if state.get("deploy_choice") != "yes" or not state.get("deploy"):
        return None
    mon = state.get("monitor") or {}
    if mon.get("result") == "success":
        return None
    if mon.get("result") == "success_unconfirmed":
        return None if mon.get("accepted") else (
            "the deployed run's Actions runs were matched by time only; the user has to accept that at "
            "Gate 5 (monitor.py accept-unconfirmed) before anything is saved")
    return (f"the deployed run has not been confirmed to work (monitor result {mon.get('result') or 'none'}); "
            "nothing is saved")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--catalog-dir")
    common.add_argument("--config-catalog-dir", help="plugin setting value; empty = unset")
    common.add_argument("--run", help="run dir containing state.json")

    p = argparse.ArgumentParser(prog="catalog.py", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("where", parents=[common])
    sub.add_parser("init", parents=[common])
    sp = sub.add_parser("list", parents=[common])
    sp.add_argument("--workflows", action="store_true")
    sp = sub.add_parser("search", parents=[common])
    sp.add_argument("--query", required=True)
    sp.add_argument("--k", type=int, default=5)
    sp = sub.add_parser("search-nodes", parents=[common])
    sp.add_argument("--k", type=int, default=5)
    for name in ("get", "increment-usage"):
        sp = sub.add_parser(name, parents=[common])
        sp.add_argument("--id", required=True)
        sp.add_argument("--workflow", action="store_true")
    sp = sub.add_parser("save-functions", parents=[common])
    sp.add_argument("--spec")
    sp.add_argument("--only-verified", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("save-workflow", parents=[common])
    sp.add_argument("--spec")
    sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("import", parents=[common])
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--dedupe-code", action="store_true")
    sp.add_argument("--overwrite", action="store_true")
    sp = sub.add_parser("export", parents=[common])
    sp.add_argument("--out", required=True)
    sp.add_argument("--ids", nargs="*")
    args = p.parse_args(argv)

    try:
        root, source = resolve_catalog_dir(args.catalog_dir, args.run, args.config_catalog_dir)
        result = run_command(args, root)
        result.setdefault("catalog_dir", str(root))
        result.setdefault("catalog_dir_source", source)
    except (InputError, OSError, json.JSONDecodeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


def run_command(args, root: Path) -> dict:
    cat = Catalog(root)
    cmd = args.cmd
    if cmd == "where":
        return {"ok": True, "exists": root.is_dir(),
                "functions": len(cat.functions), "workflows": len(cat.workflows)}
    if cmd == "init":
        cat.init()
        return {"ok": True}
    if cmd == "list":
        entries = cat.workflows.values() if args.workflows else cat.functions.values()
        return {"ok": True, "entries": [entry_summary(e) for e in entries]}
    if cmd == "search":
        return {"ok": True, "hits": [hit_json(s, e) for s, e in cat.search(args.query, args.k)]}
    if cmd == "search-nodes":
        if not args.run:
            raise InputError("search-nodes needs --run DIR")
        state = json.loads((Path(args.run) / "state.json").read_text())
        data = state.get("spec") or (state.get("skeleton_draft") or {}).get("spec")
        if not data:
            raise InputError("run has no spec or skeleton yet")
        spec = WorkflowSpec.model_validate(data)
        return {"ok": True, "nodes": {
            n.name: {"query": node_query(n),
                     "hits": [hit_json(s, e) for s, e in cat.search(node_query(n), args.k)]}
            for n in spec.nodes}}
    if cmd in ("get", "increment-usage"):
        store = cat.workflows if args.workflow else cat.functions
        e = store.get(args.id)
        if e is None:
            return {"ok": False, "error": f"no {'workflow' if args.workflow else 'function'} entry {args.id}"}
        if cmd == "get":
            return {"ok": True, "entry": json.loads(e.model_dump_json())}
        e.usage_count += 1
        (cat.save_workflow if args.workflow else cat.save_function)(e)
        return {"ok": True, "id": e.id, "usage_count": e.usage_count}
    if cmd in ("save-functions", "save-workflow") and args.run and not args.dry_run:
        problem = deployed_run_problem(json.loads((Path(args.run) / "state.json").read_text()))
        if problem:
            raise InputError(problem)
    if cmd == "save-functions":
        if args.only_verified and not args.run:
            raise InputError("--only-verified needs --run (stub-test status lives in state.json)")
        spec, state = load_run(args.run, args.spec)
        return save_functions(cat, spec, state, args.only_verified, args.dry_run)
    if cmd == "save-workflow":
        spec, _ = load_run(args.run, args.spec)
        return save_workflow(cat, spec, args.dry_run)
    if cmd == "import":
        return import_entries(cat, args.paths, args.dedupe_code, args.overwrite)
    if cmd == "export":
        return export_entries(cat, Path(args.out).expanduser(), args.ids)
    raise InputError(f"unknown command {cmd}")


if __name__ == "__main__":
    sys.exit(main())
