"""Phase 6d: Gate 4 previews the targets by building them in memory from the draft; a draft older
than the spec is refused with what to do; runs approved before versions are refused (phase 8)."""
import json

import pytest
from pathlib import Path

import spec as S
from conftest import call, ok, redraft
from test_start import source_run  # noqa: F401
from test_target_pegasus import where_section


def state(run):
    return json.loads((run / "state.json").read_text())


def at_gate4_with_draft(run, edit=None):
    """The run back at Gate 4 with a fresh draft (as a rewind to emit and emit again leave it)."""
    s = state(run)
    if edit:
        edit(s)
    s["validation"]["spec_hash"] = S.spec_hash(s["spec"])
    s["stage"] = "gate4"
    redraft(run, s)
    (run / "state.json").write_text(json.dumps(s))
    return s


def test_gate4_names_the_deployment_for_both_outcomes_of_the_duplicate_question(source_run, capsys):  # noqa: F811
    """An identical version exists: Gate 4 would publish the next id, or, if the user drops the new
    one as a duplicate, use the existing version, whose deployment then runs. Both ids are shown."""
    at_gate4_with_draft(source_run)
    s = state(source_run)
    import ids
    draft = Path(s.get("draft_dir") or ids.draft_dir(source_run))
    name = s.get("workflow_name") or ids.slug(s["spec"]["name"])
    plan = ids.plan_publish(draft, name, s["project_dir"])
    ids.publish(draft, name, s["project_dir"], plan["version"], plan["draft_hash"])  # the identical version
    nxt = ids.workflow_id(name, plan["version"] + 1)
    md = ok(S, ["show", "--session", str(source_run), "--gate", "4"], capsys)["markdown"]
    sec = where_section(md)
    assert f"deploys as `{nxt}` (as `{plan['workflow_id']}` if you drop it as a duplicate)" in sec
    import deploy as D
    first = D.plan_lines(str(source_run))[0]
    assert first.startswith(f"Deploys as: `{nxt}`") and f"if you drop it at Gate 4 as a duplicate of " \
        f"{plan['workflow_id']}, as `{plan['workflow_id']}` instead" in first


def test_gate4_shows_both_targets_built_from_the_draft(source_run, capsys):  # noqa: F811
    def add_input(s):  # a file the user must provide, so the inputs line has something to count
        s["spec"]["nodes"][0]["inputs"] = [{"name": "seed.txt"}]
    at_gate4_with_draft(source_run, add_input)
    md = ok(S, ["show", "--session", str(source_run), "--gate", "4"], capsys)["markdown"]
    sec = where_section(md)
    assert "**FaaSr (cloud)**" in sec and "deploys as `mapreduce-word-count-v" in sec  # the version Gate 4 publishes
    assert "- input files you must provide in S3: 1 (listed in the full review)" in sec
    assert "**Pegasus** on this machine:" in sec and "11 jobs" in sec
    assert "- input files you must provide: 1 (listed in the full review)" in sec
    assert not state(source_run).get("execution")  # nothing written: built in memory


def test_a_draft_older_than_the_spec_is_refused_with_what_to_do(source_run, capsys):  # noqa: F811
    s = at_gate4_with_draft(source_run)
    s["spec"]["nodes"][1]["description"] = "changed after emit"
    s["validation"]["spec_hash"] = S.spec_hash(s["spec"])
    (source_run / "state.json").write_text(json.dumps(s))
    code, out = call(S, ["approve", "--session", str(source_run), "--gate", "4", "--on-duplicate", "keep"], capsys)
    assert code == 2 and "built from an earlier version of the spec" in out["error"]
    assert f"curate-emit --session {s['run_id']} again" in out["error"]
    sec = where_section(ok(S, ["show", "--session", str(source_run), "--gate", "4"], capsys)["markdown"])
    assert "built from an earlier version of the spec" in sec


def test_a_run_approved_before_versions_is_no_longer_deployed(source_run, capsys):  # noqa: F811
    """A run with no draft and no workflow ID (approved before 6c, with the old emit's bundle): since phase 8
    deploy refuses it and says how to get a version (start a new run from it at emit)."""
    import deploy as D
    s = state(source_run)
    for k in ("draft_dir", "draft_spec_hash", "workflow_id", "workflow_version", "draft_hash", "version_dir",
              "version_hash", "version_reused", "workflow_content_hash", "execution"):
        s.pop(k, None)
    s["bundle_dir"] = str(source_run / "old-bundle")
    (source_run / "state.json").write_text(json.dumps(s))
    with pytest.raises(D.InputError) as e:
        D.load_run(str(source_run))
    assert "approved before workflow versions" in str(e.value)
    assert f"--start-at emit --from-run {s['run_id']}" in str(e.value)
    # approving Gate 4 again needs a draft: the refusal says to run emit again
    s["stage"] = "gate4"
    (source_run / "state.json").write_text(json.dumps(s))
    code, out = call(S, ["approve", "--session", str(source_run), "--gate", "4"], capsys)
    assert code == 2 and "no workflow draft" in out["error"] and "curate-emit" in out["error"]


def test_gate4_review_lists_the_built_inputs_and_the_draft(source_run, capsys):  # noqa: F811
    def add_input(s):
        s["spec"]["nodes"][0]["inputs"] = [{"name": "seed.txt"}]
    s = at_gate4_with_draft(source_run, add_input)
    md = S.show_gate({**state(source_run), "run_dir": str(source_run)}, 4, detailed=True)
    assert f"- `{s["spec"]["folder"]}/seed.txt` — tested with `inputs/seed.txt`" in md
    assert "Files in the workflow draft (what Gate 4 approves):" in md and "workflow.yml" in md


def test_emit_validates_both_targets_and_gate4_shows_them(source_run, capsys):  # noqa: F811
    """A conditional step: FaaSr takes it, Pegasus refuses it. Validation passes (FaaSr can deploy it);
    Gate 4 shows both results, and its "where it can run" summary uses the recorded Pegasus refusal."""
    def conditional(s):
        s["spec"]["edges"][0]["condition"] = "True"
    s = at_gate4_with_draft(source_run, conditional)
    v = s["validation"]
    e = s["spec"]["edges"][0]
    assert v["ok"] and v["faasr"]["ok"] and v["pegasus"]["ok"] is False
    assert f"step {e['to_node']} runs only if {e['from_node']} returns True (curate:run_if)" in v["pegasus"]["error"]
    assert v["level"].startswith("cwl: passed, faasr: passed (structural), pegasus: refused (step ")
    md = ok(S, ["show", "--session", str(source_run), "--gate", "4"], capsys)["markdown"]
    assert f"- validation: passed — {v['level']}" in md
    sec = where_section(md)
    assert "**Pegasus** on this machine: **cannot run on Pegasus:**" in sec and "(curate:run_if)" in sec
    assert "deploys as `" in sec  # FaaSr still can


def test_gate4_uses_the_recorded_refusal_without_rebuilding(source_run, capsys, monkeypatch):  # noqa: F811
    import execution as X

    def conditional(s):
        s["spec"]["edges"][0]["condition"] = "True"
    at_gate4_with_draft(source_run, conditional)
    monkeypatch.setattr(X, "pegasus_built", lambda *a, **k: (_ for _ in ()).throw(AssertionError("rebuilt")))
    sec = where_section(ok(S, ["show", "--session", str(source_run), "--gate", "4"], capsys)["markdown"])
    assert "cannot run on Pegasus" in sec
