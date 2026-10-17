"""Run-state helpers shared by spec.py, scratch.py and emit.py (curate-skills v2).

- run_lock: an exclusive fcntl lock on <run>/state.lock, held around every
  read-modify-write of state.json, so two commands can't overwrite each other.
- write_json_atomic: write via a temp file and rename.
- spec_hash / node_basis / obj_hash: the input hashes stages record, so a later
  stage can tell whether what it builds on has changed since.

New code; no counterpart in the CURATE prototype.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
from pathlib import Path

from curate_models import WorkflowSpec

# Fields harvest or the MGA change on a node; excluded from a node's basis so the
# basis only changes when the node's specification changes.
NODE_BASIS_EXCLUDED = {"code", "tests", "dependencies", "secrets"}


@contextlib.contextmanager
def run_lock(run: str | Path):
    run = Path(run)
    if not run.is_dir():
        yield
        return
    with open(run / "state.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def write_json_atomic(path: str | Path, data) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def obj_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def spec_hash(spec_dict) -> str | None:
    """Hash of a WorkflowSpec, normalised through the model so defaults don't matter."""
    if not spec_dict:
        return None
    return obj_hash(WorkflowSpec.model_validate(spec_dict).model_dump(mode="json"))


def node_basis(node_dict) -> str:
    return obj_hash({k: v for k, v in node_dict.items() if k not in NODE_BASIS_EXCLUDED})
