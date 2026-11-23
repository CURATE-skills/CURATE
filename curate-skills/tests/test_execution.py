"""Phase 6d: Gate 4 previews the targets by building them in memory from the draft; a draft older
than the spec is refused with what to do; runs approved before versions keep the old path."""
import json
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


def test_gate4_shows_both_targets_built_from_the_draft(source_run, capsys):  # noqa: F811
    def add_input(s):  # a file the user must provide, so the inputs line has something to count
        s["spec"]["nodes"][0]["inputs"] = [{"name": "seed.txt"}]
    at_gate4_with_draft(source_run, add_input)
    md = ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)["markdown"]
    sec = where_section(md)
    assert "**FaaSr (cloud)**" in sec and "deploys as `mapreduce-word-count-" in sec
    assert "- input files you must provide in S3: 1 (listed in the full review)" in sec
    assert "**Pegasus** on this machine:" in sec and "11 jobs" in sec
    assert "- input files you must provide: 1 (listed in the full review)" in sec
    assert not state(source_run).get("execution")  # nothing written: built in memory


def test_a_draft_older_than_the_spec_is_refused_with_what_to_do(source_run, capsys):  # noqa: F811
    s = at_gate4_with_draft(source_run)
    s["spec"]["nodes"][1]["description"] = "changed after emit"
    s["validation"]["spec_hash"] = S.spec_hash(s["spec"])
    (source_run / "state.json").write_text(json.dumps(s))
    code, out = call(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep"], capsys)
    assert code == 2 and "built from an earlier version of the spec" in out["error"]
    assert f"curate-emit --run {s['run_id']} again" in out["error"]
    sec = where_section(ok(S, ["show", "--run", str(source_run), "--gate", "4"], capsys)["markdown"])
    assert "built from an earlier version of the spec" in sec


def test_a_run_approved_before_versions_deploys_its_emit_bundle(source_run, capsys):  # noqa: F811
    """A run with no draft and no workflow ID (approved before 6c): deploy reads the bundle emit wrote,
    checked by its Gate 4 bundle hash, as before (removed with the old IR in phase 8)."""
    import deploy as D
    s = state(source_run)
    for k in ("draft_dir", "draft_spec_hash", "workflow_id", "workflow_version", "draft_hash", "version_dir",
              "version_hash", "version_reused", "workflow_content_hash", "execution"):
        s.pop(k, None)
    (source_run / "state.json").write_text(json.dumps(s))
    st, bundle = D.load_run(str(source_run))
    assert bundle == Path(s["bundle_dir"]) and (bundle / "workflow.json").is_file()
    # approving Gate 4 again needs a draft: the refusal says to run emit again
    s["stage"] = "gate4"
    (source_run / "state.json").write_text(json.dumps(s))
    code, out = call(S, ["approve", "--run", str(source_run), "--gate", "4"], capsys)
    assert code == 2 and "no workflow draft" in out["error"] and "curate-emit" in out["error"]


def test_gate4_review_lists_the_built_inputs_and_the_draft(source_run, capsys):  # noqa: F811
    def add_input(s):
        s["spec"]["nodes"][0]["inputs"] = [{"name": "seed.txt"}]
    s = at_gate4_with_draft(source_run, add_input)
    md = S.show_gate({**state(source_run), "run_dir": str(source_run)}, 4, detailed=True)
    assert f"- `{s["spec"]["folder"]}/seed.txt` — tested with `inputs/seed.txt`" in md
    assert "Files in the workflow draft (what Gate 4 approves):" in md and "workflow.yml" in md
