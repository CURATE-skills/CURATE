"""The command-line flags (step 5): a /curate session is `--session`, a run (an execution) is `--run`.

- spec.py, scratch.py, emit.py, stubtest.py and catalog.py take `--session <id or folder>`; `--run`, the
  old name, still works (hidden, except in spec.py, where it names a run's own stages).
- deploy.py, pegasus_deploy.py and monitor.py take `--run <workflow-id>/<run-id>` (a run id, or its
  folder). Given a session, they use its current run and say so in one line (stderr).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import deploy as D
import monitor as MON
import pegasus_deploy as PD
import spec as S
from conftest import ok
from test_start import source_run, state  # noqa: F401  (fixture)

PLUGIN = Path(__file__).resolve().parents[1]


def _help(script: str, *argv: str) -> str:
    return subprocess.run([sys.executable, str(PLUGIN / "scripts" / f"{script}.py"), *argv, "--help"],
                          capture_output=True, text=True).stdout


@pytest.mark.parametrize("script,argv", [("scratch", ["build"]), ("emit", ["bundle"]), ("stubtest", ["run"]),
                                         ("catalog", ["save-workflow"])])
def test_session_scripts_show_session_and_hide_the_old_run(script, argv):
    h = _help(script, *argv)
    assert "--session SESSION" in h and "--run" not in h


def test_spec_names_both_a_session_and_a_run():
    h = _help("spec", "status")
    assert "--session SESSION" in h and "--run RUN" in h and "a run's own stages" in h


def test_the_old_run_flag_still_names_a_session(source_run, capsys):
    new = ok(S, ["status", "--session", str(source_run)], capsys)
    old = ok(S, ["status", "--run", str(source_run)], capsys)
    assert new == old and new["kind"] == "generation"


@pytest.mark.parametrize("script", ["deploy", "pegasus_deploy", "monitor"])
def test_run_scripts_show_what_run_means(script):
    assert "<workflow-id>/<run-id>" in " ".join(_help(script, "status").split())


@pytest.fixture
def deployed(source_run, capsys, monkeypatch):
    """A session whose Gate 4 opened a FaaSr run; commands run in the project."""
    s = state(source_run)
    monkeypatch.chdir(s["project_dir"])
    s["stage"] = "gate4"
    import runstate
    runstate.save(source_run, s)
    ok(S, ["approve", "--session", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "yes"], capsys)
    return source_run


def _status(capsys, mod, ref) -> tuple[dict, str]:
    mod.main(["status", "--run", ref])
    out = capsys.readouterr()
    return json.loads(out.out), out.err


def test_deploy_and_monitor_given_a_session_use_its_run_and_say_so(deployed, capsys):
    s = state(deployed)
    ref, sid = s["execution"]["reference"], s["run_id"]
    for mod in (D, MON):
        by_run, err = _status(capsys, mod, ref)
        assert err == ""
        by_session, err = _status(capsys, mod, sid)
        assert by_session == by_run
        assert err.strip() == f"note: {sid} is a session; using its current run {ref}"


def test_pegasus_deploy_given_a_session_says_so(source_run, capsys, monkeypatch):
    s = state(source_run)
    monkeypatch.chdir(s["project_dir"])
    s["stage"] = "gate4"
    import runstate
    runstate.save(source_run, s)
    ok(S, ["approve", "--session", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus"],
       capsys)
    s = state(source_run)
    PD.main(["status", "--run", s["run_id"]])
    out = capsys.readouterr()
    assert json.loads(out.out)["ok"] and out.err.strip() == \
        f"note: {s['run_id']} is a session; using its current run {s['execution']['reference']}"


def test_a_session_without_a_run_says_it_has_none(source_run, capsys, monkeypatch):
    monkeypatch.chdir(state(source_run)["project_dir"])  # approved without deploying: no run
    D.main(["status", "--run", state(source_run)["run_id"]])
    err = capsys.readouterr().err
    assert "is a session and has no run yet" in err


# ── a bare id that names both a session and a run ────────────────────────────

def _clash(deployed):
    """Give the session's run a second sibling run whose id is the session's own id."""
    import shutil
    s = state(deployed)
    run_dir = Path(s["execution"]["dir"])
    twin = run_dir.parent / s["run_id"]
    shutil.copytree(run_dir, twin)
    wid = run_dir.parent.parent.name
    return s["run_id"], f"{wid}/{s['run_id']}", twin


def test_an_id_naming_a_session_and_a_run_is_refused(deployed, capsys):
    sid, ref, _ = _clash(deployed)
    import runstate
    with pytest.raises(runstate.AmbiguousRef, match=f"--session {sid}.*--run {ref}"):
        runstate.resolve_run(sid)
    for mod in (D, MON, PD):
        assert mod.main(["status", "--run", sid]) == 2
        out = capsys.readouterr()
        err = json.loads(out.out)["error"]
        assert "names both a session and a run" in err and f"--run {ref}" in err
        assert out.err == ""  # no "using its current run" note for an id that was refused
    assert S.main(["status", "--run", sid]) == 2
    assert "names both a session and a run" in json.loads(capsys.readouterr().out)["error"]


def test_session_flag_or_wid_rid_settles_it(deployed, capsys):
    sid, ref, twin = _clash(deployed)
    assert ok(S, ["status", "--session", sid], capsys)["kind"] == "generation"
    import runstate
    assert runstate.resolve_run(ref) == twin.resolve()
    assert runstate.resolve_run(sid, prefer="session") == Path(deployed).resolve()


def test_a_run_id_under_two_versions_is_refused(deployed, capsys):
    import shutil
    import runstate
    run_dir = Path(state(deployed)["execution"]["dir"])
    other = run_dir.parent.parent.parent / "other-version" / "runs" / run_dir.name
    shutil.copytree(run_dir, other)
    with pytest.raises(runstate.AmbiguousRef, match="more than one version"):
        runstate.resolve_run(run_dir.name)
