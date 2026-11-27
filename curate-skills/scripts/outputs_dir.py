"""A run's outputs/: its one results folder (FaaSr download-outputs, Pegasus monitor collect).

A later copy into it fills it in: a file already there with the same bytes is kept, a missing one
is added, and a file whose bytes differ (edited by hand) is never overwritten: it is reported as
skipped. manifest.json records each file and every copy made.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

MANIFEST = "manifest.json"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def place(dest: Path, rel: str, data: bytes) -> str:
    """Put `data` at dest/rel unless something is there: "added", "kept" (same bytes) or
    "differs" (left alone)."""
    target = dest / rel
    if target.is_file():
        return "kept" if sha256(target.read_bytes()) == sha256(data) else "differs"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return "added"


def read_manifest(dest: Path) -> dict:
    p = dest / MANIFEST
    return json.loads(p.read_text()) if p.is_file() else {}


def write_manifest(dest: Path, manifest: dict) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    p = dest / MANIFEST
    p.write_text(json.dumps(manifest, indent=2) + "\n")
    return p
