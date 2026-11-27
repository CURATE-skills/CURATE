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


_HELD: dict[str, int] = {}  # state.lock files this process holds, with how many times it entered


@contextlib.contextmanager
def run_lock(run: str | Path):
    """An exclusive lock on <run>/state.lock. Re-entrant within a process (flock would block on a
    second open of the same file), so a command that holds a run's lock can call code that takes it."""
    run = Path(run)
    if not run.is_dir():
        yield
        return
    key = str((run / "state.lock").resolve())
    if _HELD.get(key):
        _HELD[key] += 1
        try:
            yield
        finally:
            _HELD[key] -= 1
        return
    with open(run / "state.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        _HELD[key] = 1
        try:
            yield
        finally:
            _HELD.pop(key, None)
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


# ── sessions and runs ───────────────────────────────────────
#
# A session (<project>/.curate/runs/<run-id>/) is one /curate session. A run
# (<project>/.curate/workflows/<workflow-id>/runs/<run-id>/) is one deploy of a published version,
# on FaaSr or Pegasus; it has its own state.json ("kind": "execution"). It is opened either by a
# session's Gate 4 (or "Start this run?"), and then belongs to it ("session"), or by
# `--start-at deploy` (--workflow / --from-run), and then stands alone with its own stages.
#
# The records of an execution (EXECUTION_RECORDS) live only in the run's state.json. A
# session links to its current execution with its own "execution" record (whose "dir" is
# that folder); load() shows the session's state with the execution's records in it, and
# save() writes them back there. A session made before runs had their own state
# (no state.json in that folder) keeps everything in its own state.json, as before.
#
# Locks: every read-modify-write of a session holds its state.lock and then, if it has a
# current run, that run's state.lock, for the whole command (locked()); a write of an
# run alone holds only that run's lock. The order is always generation → execution, and
# nothing takes a session's lock while holding a run's, so two commands cannot
# deadlock, and a command never writes back execution records it read before another command
# changed them.

EXECUTION_RECORDS = ("deploy", "pegasus", "monitor")


def read_state(run: str | Path) -> dict:
    return json.loads((Path(run) / "state.json").read_text())


def is_execution(s: dict) -> bool:
    return s.get("kind") == "execution"


def handle(s: dict) -> str:
    """How commands name a run: a session's id, a run's <workflow-id>/<run-id>."""
    return s.get("reference") or s["run_id"] if is_execution(s) else s["run_id"]


def linked_execution(s: dict) -> Path | None:
    """A session's current run folder, when that run keeps its own state."""
    if is_execution(s):
        return None
    d = (s.get("execution") or {}).get("dir")
    return Path(d) if d and (Path(d) / "state.json").is_file() else None


def load(run: str | Path) -> dict:
    """The run's state; for a session, with its current execution's records in it."""
    s = read_state(run)
    ex_dir = linked_execution(s)
    if ex_dir is not None:
        ex = read_state(ex_dir)
        for k in EXECUTION_RECORDS:
            if k in ex:  # a FaaSr run has no "pegasus" field, as before
                s[k] = ex[k]
        s["execution"] = ex.get("execution") or s.get("execution")
    return s


def save(run: str | Path, s: dict, check=None) -> None:
    """Write a state load() gave (changed): a session's execution records go to its
    run's state.json (under that run's lock), everything else to the run's own. `check`
    (text -> None, raising) vets each file's text first (e.g. the leak check)."""
    run = Path(run)
    ex_dir = linked_execution(s)
    own = dict(s)
    if ex_dir is not None:
        with run_lock(ex_dir):
            ex = read_state(ex_dir)
            for k in EXECUTION_RECORDS:
                if k in own:
                    ex[k] = own.pop(k)
                else:
                    ex.pop(k, None)
            ex["execution"] = s.get("execution")
            _write_checked(ex_dir / "state.json", ex, check)
    _write_checked(run / "state.json", own, check)


def _write_checked(path: Path, data: dict, check) -> None:
    if check is not None:
        check(json.dumps(data, indent=2) + "\n")
    write_json_atomic(path, data)


@contextlib.contextmanager
def locked(run: str | Path):
    """Hold the run's lock and, for a session, its current run's lock (in that
    order) until the block ends: the locks a load/change/save of the run needs."""
    with run_lock(run):
        try:
            ex_dir = linked_execution(read_state(run)) if (Path(run) / "state.json").is_file() else None
        except (OSError, ValueError):
            ex_dir = None
        with run_lock(ex_dir) if ex_dir is not None else contextlib.nullcontext():
            yield


def update(run: str | Path, change, check=None) -> dict:
    """Load, change (a function of the state), save, under the run's locks (locked()). Returns the
    new state."""
    with locked(run):
        s = load(run)
        change(s)
        save(run, s, check)
        return s


def _owner(ex_dir: Path) -> Path:
    """A run that belongs to a session whose current execution it still is is used
    through that session (its stages and gates are that run's); any other stands alone."""
    try:
        ex = read_state(ex_dir)
        gen = Path((ex.get("session") or {}).get("dir") or "")
        if gen.name and (gen / "state.json").is_file():
            if Path((read_state(gen).get("execution") or {}).get("dir") or "").resolve() == ex_dir.resolve():
                return gen
    except (OSError, ValueError):
        pass
    return ex_dir


def resolve_run(ref: str | Path, project: str | Path | None = None) -> Path:
    """The folder `--run` names: a folder with a state.json, a session id (.curate/sessions/, or
    .curate/runs/ from before), <workflow-id>/<run-id> or a run id (workflows/*/runs/, or
    .curate/workflows/*/runs/ from before). Anything else is returned unchanged (the command then
    says it is not a run)."""
    import ids
    p = Path(str(ref)).expanduser()
    if (p / "state.json").is_file():
        p = p.resolve()
        return _owner(p) if is_execution(read_state(p)) else p
    project = Path(project or ".").resolve()
    ref = str(ref).strip().strip("/")
    if "/" not in ref and ref and (d := ids.session_dir(project, ref)) is not None:
        return d.resolve()
    if ref.count("/") == 1:
        cand = ids.execution_dir(project, *ref.split("/"))
        if (cand / "state.json").is_file():
            return _owner(cand.resolve())
    elif "/" not in ref and ref:
        found = ids.execution_dirs(project, ref)
        if len(found) == 1:
            return _owner(found[0].resolve())
    return p


def files_dir(run: str | Path, s: dict | None = None) -> Path:
    """Where a run's execution files go (deploy log, FaaSr deploy copy, monitor report, logs): its
    run's folder; for a session made before runs had their own state,
    the run's own folder (<run>/deploy/, <run>/pegasus/, as then)."""
    s = load(run) if s is None else s
    if is_execution(s):
        return Path(run)
    return linked_execution(s) or Path(run)


def is_legacy(run: str | Path, s: dict | None = None) -> bool:
    """A session that keeps its execution's files in its own folder (made before execution
    runs had their own state)."""
    s = load(run) if s is None else s
    return not is_execution(s) and linked_execution(s) is None
