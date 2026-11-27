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


def stop_message(s: dict, stage_command: str | None = None, stage_note: str = "") -> str:
    """What to tell the user when a session or run stops: /curate --resume first (it picks up
    where it stopped and carries on through the pipeline), then, if there is one, the command for
    just that stage."""
    lines = [f"To continue: `/curate-skills:curate --resume {handle(s)}`"]
    if stage_command:
        lines.append(f"Or, {stage_note or 'for just this stage'}: `{stage_command}`")
    return "\n".join(lines)


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


def legacy_owner(project: str | Path, ex_dir: str | Path) -> Path | None:
    """A run folder with no state.json of its own (made before runs kept their own state) is used
    through the session that links it: that session holds the run's records (deploy, pegasus,
    monitor). The session, or None (the folder has its own state, or no session links it)."""
    import ids
    ex_dir = Path(ex_dir).expanduser()
    if (ex_dir / "state.json").is_file() or not ex_dir.is_dir():
        return None
    ex_dir = ex_dir.resolve()
    for d in ids.session_dirs(Path(project)):
        try:
            st = read_state(d)
        except (OSError, ValueError):
            continue
        dd = (st.get("execution") or {}).get("dir")
        if not is_execution(st) and dd and Path(dd).expanduser().resolve() == ex_dir:
            return d
    return None


def current_run(s: dict) -> str | None:
    """The <workflow-id>/<run-id> a session's run commands use: its current run, when that run keeps
    its own state. None for a run, and for a session made before (its records are its own: commands
    name it by its session id)."""
    return (s.get("execution") or {}).get("reference") if linked_execution(s) is not None else None


class AmbiguousRef(ValueError):
    """A bare id names more than one thing (a session and a run, or runs of two versions)."""


def resolve_run(ref: str | Path, project: str | Path | None = None, prefer: str | None = None) -> Path:
    """The folder `--run` names: a folder with a state.json, a session id (.curate/sessions/, or
    .curate/runs/ from before), <workflow-id>/<run-id> or a run id (workflows/*/runs/, or
    .curate/workflows/*/runs/ from before). Anything else is returned unchanged (the command then
    says it is not a run).

    Sessions and runs share one id format, so a bare id can name both: that raises AmbiguousRef,
    unless `prefer="session"` (the user wrote `--session <id>`). A run id found under two
    versions raises it too."""
    import ids
    p = Path(str(ref)).expanduser()
    if (p / "state.json").is_file():
        p = p.resolve()
        return _owner(p) if is_execution(read_state(p)) else p
    if p.is_dir() and (proj := ids.project_of_path(p)) and (o := legacy_owner(proj, p)) is not None:
        return o.resolve()  # a run folder from before runs kept their own state: its session
    project = Path(project or ".").resolve()
    ref = str(ref).strip().strip("/")
    if ref.count("/") == 1:
        cand = ids.execution_dir(project, *ref.split("/"))
        if (cand / "state.json").is_file():
            return _owner(cand.resolve())
        o = legacy_owner(project, cand)
        return o.resolve() if o is not None else p
    if "/" in ref or not ref:
        return p
    session = ids.session_dir(project, ref)
    if session is not None and prefer == "session":
        return session.resolve()
    runs = [d for d in ids.execution_dirs(project, ref) if (d / "state.json").is_file()]
    runs += [o for root in ids.workflow_roots(project) for d in sorted(root.glob(f"*/runs/{ref}"))
             if (o := legacy_owner(project, d)) is not None]  # a run folder used through its session
    names = [f"{d.parent.parent.name}/{d.name}" if is_execution(read_state(d)) else
             (read_state(d).get("execution") or {}).get("reference") or d.name for d in runs]
    if session is not None and runs:
        raise AmbiguousRef(f"{ref} names both a session and a run ({', '.join(names)}); say which: "
                           f"--session {ref} for the session, or --run {names[0]} for the run")
    if len(runs) > 1:
        raise AmbiguousRef(f"{ref} names runs of more than one version ({', '.join(names)}); "
                           f"say which with --run <workflow-id>/<run-id>")
    if session is not None:
        return session.resolve()
    if not runs:
        return p
    return _owner(runs[0].resolve()) if is_execution(read_state(runs[0])) else runs[0].resolve()


def resolve_args(args, attr: str, project: str | Path | None = None, note: bool = False) -> str | None:
    """Resolve `args.<attr>` in place (see resolve_run; `--session` prefers the session). With
    `note`, print session_note to stderr (the run scripts). On an ambiguous id, print the JSON
    error and return it, for main to exit 2; None when all is well."""
    ref = getattr(args, attr, None)
    if not ref:
        return None
    try:
        resolved = resolve_run(ref, project, "session" if getattr(args, "session_flag", None) == "--session" else None)
    except AmbiguousRef as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return str(e)
    if note and (msg := session_note(ref, project, resolved)):
        import sys
        print(msg, file=sys.stderr)
    setattr(args, attr, str(resolved))
    return None


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


# ── the command-line flags ───────────────────────────────────────────────────

def add_session_flag(parser, required: bool = False, group=None, run_help: str | None = None) -> None:
    """`--session <id or folder>` (dest "session"). `--run` is its old name, kept for old command lines:
    hidden, unless `run_help` says what it means for this command (spec.py: a run's own stages)."""
    import argparse
    target = group if group is not None else parser.add_mutually_exclusive_group(required=required)
    class _Store(argparse.Action):  # remembers which flag named it (resolve_args: --session prefers a session)
        def __call__(self, parser, namespace, values, option_string=None):
            setattr(namespace, self.dest, values)
            namespace.session_flag = option_string

    target.add_argument("--session", dest="session", metavar="SESSION", action=_Store,
                        help="the /curate session: its id (.curate/sessions/<id>) or its folder")
    target.add_argument("--run", dest="session", metavar="RUN", action=_Store, help=run_help or argparse.SUPPRESS)


RUN_HELP = "the run: <workflow-id>/<run-id>, its run id or its folder (a session id: that session's current run)"


def session_note(ref: str | Path, project: str | Path | None = None, resolved: str | Path | None = None) -> str | None:
    """For deploy.py, pegasus_deploy.py and monitor.py, which take a run: if `ref` names a session
    rather than a run, one line saying which run it stands for; if it names a run folder from before
    runs kept their own state, one line naming the session used for it. None if `ref` names a run."""
    import ids
    p = Path(str(ref)).expanduser()
    if (p / "state.json").is_file():
        s = read_state(p)
    else:
        d = ids.session_dir(Path(project or ".").resolve(), str(ref)) if "/" not in str(ref) else None
        if d is None:
            r = Path(resolved) if resolved else None
            if (r is not None and (r / "state.json").is_file() and not is_execution(rs := read_state(r))
                    and linked_execution(rs) is None):  # not a session's current run: one used through it
                return (f"note: {ref} has no state of its own (made before runs kept theirs); using its "
                        f"session {rs['run_id']}, which holds its records")
            return None
        s = read_state(d)
    if is_execution(s):
        return None
    ex = (s.get("execution") or {}).get("reference")
    if not ex and any(s.get(k) for k in EXECUTION_RECORDS):
        return f"note: {ref} is a session from before runs; its deploy records are its own, so the session is used"
    if ex and linked_execution(s) is None:
        return (f"note: {ref} is a session from before runs kept their own state; its run {ex}'s records "
                f"are the session's, so the session is used")
    return (f"note: {ref} is a session; using its current run {ex}" if ex else
            f"note: {ref} is a session and has no run yet (Gate 4 or \"Start this run?\" opens one)")


def flag(s: dict, skill: str = "") -> str:
    """How a command names this session or run: `--session <id>` for a session, `--run
    <workflow-id>/<run-id>` for a run, and for the deploy and monitor skills, a session's run."""
    if is_execution(s):
        return f"--run {handle(s)}"
    if skill.rsplit(":", 1)[-1] in ("curate-deploy", "curate-monitor"):  # they take a run (a session: its run)
        return f"--run {current_run(s) or s['run_id']}"
    return f"--session {s['run_id']}"
