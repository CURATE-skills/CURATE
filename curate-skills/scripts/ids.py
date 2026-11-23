"""Workflow IDs, run IDs, the .curate/workflows/ layout, versions and bundle manifests (CWL IR
brief, "Workflow and run IDs"; docs/CWL_IR.md, "Identity and versions" and "Layout").

  workflow ID   <name>-v<N>                e.g. corvallis-winter-v3   (parse: ^(.+)-v(\\d+)$)
  run ID        YYYYMMDD-HHMMSS-<4 hex>    e.g. 20261118-012903-a3f9
  reference     <workflow-id>/<run-id>

  <project>/.curate/runs/<run-id>/draft/                   emit's draft (a generation session)
  <project>/.curate/workflows/<workflow-id>/               a published version (never edited)
  <project>/.curate/workflows/<workflow-id>/runs/<run-id>/ one execution: bundle/, manifest.json

A draft becomes a version only when Gate 4 approves it: plan_publish() works out the next
version number and any existing version with the same content hash (nothing is written, so
the caller can ask), then publish() copies the draft atomically, adds s:identifier, s:version
and curate:content_hash to its workflow.yml and makes the folder read-only.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
from datetime import datetime
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
WORKFLOW_ID = re.compile(r"^(.+)-v(\d+)$")
RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")


class IdError(Exception):
    pass


def slug(title: str) -> str:
    """A workflow name from a title: lower-case letters and digits joined by single dashes."""
    s = re.sub(r"[^a-z0-9]+", "-", str(title).lower()).strip("-")
    return s or "workflow"


def check_name(name: str) -> str:
    if not NAME.match(name or ""):
        raise IdError(f"workflow name {name!r} must be lower-case letters and digits joined by single dashes")
    if WORKFLOW_ID.match(name):
        raise IdError(f"workflow name {name!r} ends like a version (-v<N>); choose another")
    return name


def workflow_id(name: str, version: int) -> str:
    return f"{check_name(name)}-v{int(version)}"


def parse_workflow_id(wid: str) -> tuple[str, int]:
    m = WORKFLOW_ID.match(wid or "")
    if not m:
        raise IdError(f"{wid!r} is not a workflow ID (<name>-v<N>)")
    return m.group(1), int(m.group(2))


def new_run_id(now: datetime | None = None, token: str | None = None) -> str:
    return f"{(now or datetime.now()):%Y%m%d-%H%M%S}-{token or secrets.token_hex(2)}"


def reference(wid: str, rid: str) -> str:
    return f"{wid}/{rid}"


# ── layout ───────────────────────────────────────────────────────────────────

def workflows_root(project) -> Path:
    return Path(project) / ".curate" / "workflows"


def version_dir(project, wid: str) -> Path:
    return workflows_root(project) / wid


def execution_dir(project, wid: str, rid: str) -> Path:
    return version_dir(project, wid) / "runs" / rid


def draft_dir(run_dir) -> Path:
    return Path(run_dir) / "draft"


# ── hashes ───────────────────────────────────────────────────────────────────

OS_METADATA = re.compile(r"(^|/)(\.DS_Store|Thumbs\.db|desktop\.ini|__pycache__(/.*)?|\._[^/]*)$")


def tree_hash(folder, skip=()) -> str:
    """sha256 over a folder's files (relative path + bytes), ignoring OS metadata and `skip` (top-level
    names). The same rule as deploy.tree_hash, which hashes bundles."""
    import hashlib
    folder = Path(folder)
    h = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        rel = p.relative_to(folder).as_posix()
        if p.is_file() and not OS_METADATA.search(rel) and rel.split("/", 1)[0] not in skip:
            h.update(rel.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def draft_content_hash(draft) -> str:
    """The brief's content hash of a draft folder (workflow.yml + its function files)."""
    import yaml
    from spec_cwl import content_hash
    draft = Path(draft)
    doc = yaml.safe_load((draft / "workflow.yml").read_text())
    functions = {}
    for step in (doc.get("steps") or {}).values():
        code = (((step.get("hints") or {}).get("curate:function")) or {}).get("code")
        if code:
            functions[code] = (draft / code).read_text()
    return content_hash(doc, functions)


# ── versions ─────────────────────────────────────────────────────────────────

def legacy_entries(name: str, catalog) -> list:
    """Catalog workflow entries saved before workflow IDs (uuid only) whose spec name slugs to
    `name`, oldest first (by their file's modification time, then id)."""
    if catalog is None:
        return []
    hits = [e for e in catalog.workflows.values()
            if not getattr(e, "workflow_id", None) and slug(e.workflow_spec.name) == name]

    def age(e):
        f = Path(catalog.wf_dir) / f"{e.id}.json"
        return (f.stat().st_mtime if f.is_file() else 0.0, e.id)
    return sorted(hits, key=age)


def reserved_numbers(name: str, used, catalog) -> dict[int, str]:
    """Version numbers held for the catalog's old uuid entries of this name, so that numbers are
    never renumbered: each old entry holds one of the lowest free numbers now (oldest entry first),
    new versions are numbered after them, and the phase 8 migration gives each old entry exactly
    the number it holds here. {number: entry id}."""
    out, n = {}, 0
    for e in legacy_entries(name, catalog):
        n += 1
        while n in used:
            n += 1
        out[n] = e.id
    return out


def versions(name: str, project=None, catalog=None) -> dict[int, str | None]:
    """{version number: content hash or None} of a workflow name, from the project's published
    folders and the catalog's entries (Catalog from catalog.py), plus the numbers the catalog's old
    uuid entries hold (reserved_numbers; no content hash: they are not offered as duplicates)."""
    out: dict[int, str | None] = {}
    if project is not None and workflows_root(project).is_dir():
        import yaml
        for d in workflows_root(project).iterdir():
            m = WORKFLOW_ID.match(d.name)
            if m and m.group(1) == name and (d / "workflow.yml").is_file():
                doc = yaml.safe_load((d / "workflow.yml").read_text()) or {}
                out[int(m.group(2))] = doc.get("curate:content_hash")
    if catalog is not None:
        for e in catalog.workflows.values():
            if getattr(e, "workflow_id", None):
                n, v = parse_workflow_id(e.workflow_id)
                if n == name:
                    out[v] = out.get(v) or getattr(e, "content_hash", None)
        for v in reserved_numbers(name, set(out), catalog):
            out.setdefault(v, None)
    return out


def name_status(name: str, catalog) -> dict:
    """Asked at the sketch: is this name taken in the global catalog (by an ID'd entry, or by an
    older entry whose spec has a name with the same slug)?"""
    check_name(name)
    vs = versions(name, catalog=catalog)
    legacy = [e.id for e in legacy_entries(name, catalog)]
    return {"name": name, "taken": bool(vs or legacy), "versions": sorted(vs), "legacy_entries": legacy}


def plan_publish(draft, name: str, project, catalog=None) -> dict:
    """What publish() would do, without writing: {"workflow_id", "version", "content_hash",
    "draft_hash", "duplicates": [workflow IDs with the same content hash]}."""
    check_name(name)
    vs = versions(name, project, catalog)
    version = max(vs, default=0) + 1
    ch = draft_content_hash(draft)
    dups = sorted((workflow_id(name, v) for v, h in vs.items() if h == ch), key=lambda w: parse_workflow_id(w)[1])
    return {"workflow_id": workflow_id(name, version), "version": version, "content_hash": ch,
            "draft_hash": tree_hash(draft), "duplicates": dups}


def publish(draft, name: str, project, version: int, approved_hash: str) -> dict:
    """Copy an approved draft to .curate/workflows/<name>-v<version>/ atomically and add its identity.
    Refuses a draft whose tree hash is not the approved one, and an existing version folder."""
    import yaml
    draft = Path(draft)
    got = tree_hash(draft)
    if got != approved_hash:
        raise IdError(f"the draft changed since Gate 4 approved it ({got[:12]} != {approved_hash[:12]}); "
                      "nothing was published")
    wid = workflow_id(name, version)
    dest = version_dir(project, wid)
    if dest.exists():
        raise IdError(f"{wid} already exists; a version is never overwritten")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{wid}.tmp-{secrets.token_hex(3)}"
    shutil.copytree(draft, tmp)
    doc = yaml.safe_load((tmp / "workflow.yml").read_text())
    ch = draft_content_hash(draft)
    ns = dict(doc.get("$namespaces") or {})
    ns.setdefault("s", "https://schema.org/")
    ordered = {"cwlVersion": doc.get("cwlVersion"), "class": doc.get("class"), "$namespaces": ns}
    for k in ("label", "doc"):
        if k in doc:
            ordered[k] = doc[k]
    ordered.update({"s:identifier": wid, "s:version": str(version), "curate:content_hash": ch})
    ordered.update({k: v for k, v in doc.items() if k not in ordered})
    (tmp / "workflow.yml").write_text(yaml.safe_dump(ordered, sort_keys=False, default_flow_style=False,
                                                     allow_unicode=True))
    try:
        os.rename(tmp, dest)  # atomic on one file system; fails if dest appeared meanwhile
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise IdError(f"{wid} appeared while publishing; nothing was overwritten")
    _read_only(dest)
    return {"workflow_id": wid, "version": version, "dir": str(dest), "content_hash": ch,
            "draft_hash": approved_hash, "version_hash": tree_hash(dest, skip=("runs",))}


def copy_version(src, project, wid: str, expected_content_hash: str) -> dict:
    """Bring an existing version into this project (Gate 4's "drop the new one" when the identical
    version is only in the catalog): workflow.yml, inputs.yml and functions/ (never runs/), same
    workflow ID, read-only, refused unless its content hash is the catalog's."""
    src = Path(src)
    dest = version_dir(project, wid)
    if dest.exists():
        raise IdError(f"{wid} already exists in this project; nothing was copied")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{wid}.tmp-{secrets.token_hex(3)}"
    shutil.copytree(src, tmp, ignore=lambda d, names: ["runs"] if Path(d) == src else [])
    got = draft_content_hash(tmp)
    if got != expected_content_hash:
        shutil.rmtree(tmp, ignore_errors=True)
        raise IdError(f"the catalog's copy of {wid} does not match its content hash; nothing was copied")
    try:
        os.rename(tmp, dest)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise IdError(f"{wid} appeared while copying; nothing was overwritten")
    _read_only(dest)
    return {"dir": str(dest), "version_hash": tree_hash(dest, skip=("runs",)), "content_hash": got}


def _read_only(folder: Path) -> None:
    for p in folder.rglob("*"):
        if p.is_file():
            p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def check_version(project, wid: str, approved_version_hash: str) -> None:
    got = tree_hash(version_dir(project, wid), skip=("runs",))
    if got != approved_version_hash:
        raise IdError(f"{wid} changed since it was published ({got[:12]} != {approved_version_hash[:12]})")


# ── bundles ──────────────────────────────────────────────────────────────────

def curate_version() -> str:
    """The CURATE code a bundle was built with: the plugin's git commit (+ "-dirty" with local
    changes), else the plugin's version."""
    try:
        commit = subprocess.run(["git", "-C", str(PLUGIN_DIR), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=10)
        if commit.returncode == 0 and commit.stdout.strip():
            dirty = subprocess.run(["git", "-C", str(PLUGIN_DIR), "status", "--porcelain", "--", "."],
                                   capture_output=True, text=True, timeout=10).stdout.strip()
            return commit.stdout.strip() + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return "plugin " + json.loads((PLUGIN_DIR / ".claude-plugin" / "plugin.json").read_text())["version"]
    except (OSError, ValueError, KeyError):
        return "unknown"


def write_manifest(execution, version_hash: str, target: str, bundle_dir="bundle") -> dict:
    """execution/manifest.json next to execution/<bundle_dir>: the version folder's hash, the CURATE
    version, the target and the bundle's own hash."""
    execution = Path(execution)
    m = {"version_hash": version_hash, "curate_version": curate_version(), "target": target,
         "bundle": bundle_dir, "bundle_hash": tree_hash(execution / bundle_dir)}
    (execution / "manifest.json").write_text(json.dumps(m, indent=2) + "\n")
    return m


def check_manifest(execution, approved_version_hash: str) -> dict:
    """Deploy's check: the bundle was built from the approved version and has not changed since."""
    execution = Path(execution)
    m = json.loads((execution / "manifest.json").read_text())
    if m.get("version_hash") != approved_version_hash:
        raise IdError("the bundle was built from another version than the approved one")
    got = tree_hash(execution / m.get("bundle", "bundle"))
    if got != m.get("bundle_hash"):
        raise IdError(f"the bundle changed after it was built ({got[:12]} != {str(m.get('bundle_hash'))[:12]})")
    return m
