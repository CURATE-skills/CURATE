"""emit.py: the FaaSr workflow JSON checks (structural and FaaSr_py), placeholders and secrets in the
FaaSr workflow cwl_faasr builds from a spec's CWL, and `emit.py bundle`, which writes the run's
workflow draft for Gate 4. (Until phase 8 emit also turned a spec straight into FaaSr JSON and a bundle
folder, emit_faasr_json / write_bundle; the FaaSr workflow now always comes from the CWL.)"""
import json
import sys
from pathlib import Path

import pytest

import cwl_faasr as CF
import emit
import spec_cwl
from curate_models import FunctionSpec, IOSpec, WorkflowEdge, WorkflowSpec, clean_dependencies

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "curate_ir"))
from cwl.validate import validate as cwl_validate  # noqa: E402

PLAIN = "def main(inputs, outputs, params):\n    pass\n"

DEPLOY_VARS = emit.PLACEHOLDER_VARS + ("FAASR_DEFAULT_CONTAINER",)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in DEPLOY_VARS + emit.BASE_SECRET_NAMES + ("API_TOKEN",):
        monkeypatch.delenv(v, raising=False)


def single_node_spec():
    return WorkflowSpec(
        name="local_function_test",
        entry="calculate_number_summary",
        nodes=[FunctionSpec(
            name="calculate_number_summary",
            inputs=[IOSpec(name="run_params.json", type="json")],
            outputs=[IOSpec(name="number_summary.json", type="json")],
            code=PLAIN,
        )],
    )


def mapreduce_spec():
    return WorkflowSpec(
        name="word_count",
        entry="split_words",
        nodes=[
            FunctionSpec(name="split_words", inputs=[IOSpec(name="words.txt")],
                         outputs=[IOSpec(name="chunk_{rank}.txt")], code=PLAIN),
            FunctionSpec(name="map_words", rank=5, inputs=[IOSpec(name="chunk_{rank}.txt")],
                         outputs=[IOSpec(name="counts_{rank}.json")], code=PLAIN),
            FunctionSpec(name="reduce_counts", inputs=[IOSpec(name="counts_1.json")],
                         outputs=[IOSpec(name="totals.json")], code=PLAIN),
        ],
        edges=[WorkflowEdge(from_node="split_words", to_node="map_words"),
               WorkflowEdge(from_node="map_words", to_node="reduce_counts")],
    )


def faasr_wf(spec: WorkflowSpec) -> dict:
    """The FaaSr workflow JSON for a spec, the way a deploy gets it: spec -> CWL -> cwl_faasr."""
    import tempfile
    folder = spec_cwl.write(spec_cwl.build(spec), Path(tempfile.mkdtemp()) / "cwl")
    wf, errors, _ = cwl_validate(folder / "workflow.yml", folder / "inputs.yml")
    assert errors == [], errors
    return CF.build(wf, spec.name, spec.folder, spec.compute_target, spec.data_store, "structural")


# ── ported from ref/curate tests/test_faasr_validation.py ──

def test_single_terminal_action_is_a_valid_dag():
    wf = faasr_wf(single_node_spec())["workflow"]
    assert wf["FunctionInvoke"] == "calculate-number-summary"
    assert wf["ActionList"]["calculate-number-summary"]["InvokeNext"] == []
    assert emit.structural_validate(wf) == (True, None)


def test_single_action_still_requires_a_valid_entry():
    wf = faasr_wf(single_node_spec())["workflow"]
    wf["FunctionInvoke"] = "missing-action"
    ok, err = emit.structural_validate(wf)
    assert not ok and "FunctionInvoke" in err


@pytest.fixture(scope="module")
def faasr_py_available():
    res = emit.faasr_py_validate(faasr_wf(single_node_spec())["workflow"])
    if res is None:
        pytest.skip("FaaSr_py validator could not run")
    return res


def test_faasr_py_accepts_single_action_with_placeholders(faasr_py_available):
    assert faasr_py_available == (True, None)


def test_faasr_py_rejects_bad_entry(faasr_py_available):
    wf = faasr_wf(single_node_spec())["workflow"]
    wf["FunctionInvoke"] = "missing-action"
    ok, err = emit.faasr_py_validate(wf)
    assert not ok and "FunctionInvoke" in err


def test_faasr_py_accepts_ranked_workflow(faasr_py_available):
    assert emit.faasr_py_validate(faasr_wf(mapreduce_spec())["workflow"]) == (True, None)


# ── emitter behaviour ──

def test_ranked_target_is_invoked_as_name_n():
    wf = faasr_wf(mapreduce_spec())["workflow"]
    assert wf["ActionList"]["split-words"]["InvokeNext"] == ["map-words(5)"]
    assert wf["ActionList"]["map-words"]["InvokeNext"] == ["reduce-counts"]


def test_conditional_edges_become_true_false_branches():
    spec = WorkflowSpec(
        name="branchy", entry="check",
        nodes=[FunctionSpec(name="check", code="def main(inputs, outputs, params):\n    return True\n"),
               FunctionSpec(name="good", code=PLAIN), FunctionSpec(name="bad", code=PLAIN)],
        edges=[WorkflowEdge(from_node="check", to_node="good", condition="True"),
               WorkflowEdge(from_node="check", to_node="bad", condition="False")],
    )
    wf = faasr_wf(spec)["workflow"]
    assert wf["ActionList"]["check"]["InvokeNext"] == [{"True": ["good"], "False": ["bad"]}]
    assert emit.structural_validate(wf) == (True, None)


def test_dependencies_drop_stdlib_and_faasr():
    assert clean_dependencies(["numpy", "os", "faasr", "FaaSr_py", "json", "pandas", "numpy"]) == [
        "numpy", "pandas"]
    spec = single_node_spec()
    spec.nodes[0].dependencies = ["re", "matplotlib"]
    assert faasr_wf(spec)["workflow"]["PyPIPackageDownloads"] == {
        "calculate_number_summary": ["matplotlib"]}


def test_external_inputs_tolerate_rank_templates():
    ext = emit.external_inputs(mapreduce_spec())
    assert [e["file"] for e in ext] == ["words.txt"]
    assert ext[0]["s3_path"] == "workflow_data/words.txt"


def test_secret_names_only_in_workflow():
    spec = single_node_spec()
    spec.nodes[0].secrets = ["API_TOKEN"]
    wf = faasr_wf(spec)["workflow"]
    assert wf["Secrets"] == ["API_TOKEN"]
    assert wf["DataStores"]["S3"]["AccessKey"] == "S3_AccessKey"


# ── placeholders ──

def test_unset_faasr_vars_become_placeholders():
    wf = faasr_wf(single_node_spec())["workflow"]
    assert wf["ComputeServers"]["GH"]["UserName"] == "<FAASR_GH_USERNAME>"
    assert wf["DataStores"]["S3"]["Bucket"] == "<FAASR_S3_BUCKET>"
    assert wf["FunctionGitRepo"]["calculate_number_summary"] == [
        "<FAASR_GH_USERNAME>/<FAASR_ACTION_REPO>/main/functions/calculate_number_summary.py",
        "<FAASR_GH_USERNAME>/<FAASR_ACTION_REPO>/main/functions/calculate_number_summary_main.py"]
    assert emit.placeholders_used() == list(emit.PLACEHOLDER_VARS)


def test_set_faasr_vars_are_used(monkeypatch):
    monkeypatch.setenv("FAASR_GH_USERNAME", "octo")
    monkeypatch.setenv("FAASR_S3_BUCKET", "my-bucket")
    wf = faasr_wf(single_node_spec())["workflow"]
    assert wf["ComputeServers"]["GH"]["UserName"] == "octo"
    assert wf["DataStores"]["S3"]["Bucket"] == "my-bucket"
    assert "FAASR_GH_USERNAME" not in emit.placeholders_used()


# ── validation fallback ──

def test_structural_fallback_when_faasr_py_unavailable(monkeypatch):
    monkeypatch.setattr(emit, "faasr_py_validate", lambda wf, timeout=300: None)
    res = emit.validate(faasr_wf(mapreduce_spec())["workflow"])
    assert res["ok"] and res["level"] == "structural" and res["fallback_reason"]
    forced = emit.validate(faasr_wf(mapreduce_spec())["workflow"], level="faasr_py")
    assert not forced["ok"] and forced["level"] == "faasr_py"


def test_structural_catches_cycle_unreachable_and_bad_target():
    wf = faasr_wf(mapreduce_spec())["workflow"]
    wf["ActionList"]["reduce-counts"]["InvokeNext"] = ["split-words"]
    assert "cycle" in emit.structural_validate(wf)[1]

    wf = faasr_wf(mapreduce_spec())["workflow"]
    wf["ActionList"]["map-words"]["InvokeNext"] = []
    assert "unreachable" in emit.structural_validate(wf)[1]

    wf = faasr_wf(mapreduce_spec())["workflow"]
    wf["ActionList"]["map-words"]["InvokeNext"] = ["nowhere"]
    assert "nowhere" in emit.structural_validate(wf)[1]


# ── bundle + secrets guard ──

@pytest.fixture
def structural_only(monkeypatch):
    monkeypatch.setattr(emit, "faasr_py_validate", lambda wf, timeout=300: None)


def _all_text(d):
    return "".join(p.read_text() for p in d.rglob("*") if p.is_file())


def test_faasr_bundle_has_secret_names_only(tmp_path, monkeypatch, structural_only):
    monkeypatch.setenv("S3_SecretKey", "s3-secret-value-xyz")
    monkeypatch.setenv("API_TOKEN", "api-token-value-123")
    spec = single_node_spec()
    spec.nodes[0].secrets = ["API_TOKEN"]
    spec.nodes[0].dependencies = ["numpy"]
    out = CF.write(faasr_wf(spec), tmp_path / "bundle")
    for f in ("workflow.json", "outputs.json", "external_inputs.json", "README.md",
              "functions/calculate_number_summary.py", "functions/calculate_number_summary_main.py"):
        assert (out / f).is_file(), f
    text = _all_text(out)
    assert "s3-secret-value-xyz" not in text and "api-token-value-123" not in text
    assert "`API_TOKEN`" in (out / "README.md").read_text()


def run_with(tmp_path, spec, run_id="20261018-run-a"):
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({"run_id": run_id, "spec": json.loads(spec.model_dump_json())}))
    return run


def test_draft_refuses_a_leaked_secret_value(tmp_path, capsys, monkeypatch, structural_only):
    monkeypatch.setenv("API_TOKEN", "api-token-value-123")
    spec = single_node_spec()
    spec.nodes[0].secrets = ["API_TOKEN"]
    spec.nodes[0].code += 'TOKEN = "api-token-value-123"\n'
    run = run_with(tmp_path, spec)
    code = emit.main(["bundle", "--run", str(run)])
    out = capsys.readouterr().out
    res = json.loads(out)
    assert code == 1 and not res["ok"] and res["draft"]["leaked_secret_names"] == ["API_TOKEN"]
    assert not (run / "draft").exists() and "api-token-value-123" not in out


def test_draft_reports_missing_code(tmp_path, capsys, structural_only):
    spec = mapreduce_spec()
    for n in spec.nodes:
        n.code = None
    code = emit.main(["bundle", "--run", str(run_with(tmp_path, spec))])
    res = json.loads(capsys.readouterr().out)
    assert code == 1 and not res["ok"] and sum("has no code" in p for p in res["draft"]["problems"]) == 3


def test_cli_bundle_updates_run_state(tmp_path, capsys, structural_only):
    run = run_with(tmp_path, single_node_spec())
    code = emit.main(["bundle", "--run", str(run)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"]
    state = json.loads((run / "state.json").read_text())
    assert "bundle_dir" not in state and "bundle_dir" not in out  # no FaaSr bundle before Gate 4
    v = state["validation"]  # from the draft and the FaaSr workflow built from it
    assert {k: v[k] for k in ("ok", "level", "error", "source")} == {
        "ok": True, "level": "cwl: passed, faasr: passed (structural), pegasus: passed", "error": None,
        "source": "draft"}
    assert v["faasr"] == {"ok": True, "level": "structural", "error": None, "problems": []}
    assert v["pegasus"] == {"ok": True, "error": None, "problems": []}
    assert state["draft_dir"] == str(run / "draft")
    assert v["spec_hash"] and state["draft_spec_hash"] == v["spec_hash"]  # which spec the draft was built from
    assert state["stage"] == "gate4"
    before = (run / "draft" / "workflow.yml").read_text()
    assert emit.main(["bundle", "--run", str(run)]) == 0  # an unapproved draft is simply replaced
    assert (run / "draft" / "workflow.yml").read_text() == before


def test_cli_rejects_bad_spec(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "x", "nodes": [{"name": "a"}],
                               "edges": [{"from_node": "a", "to_node": "ghost"}]}))
    assert emit.main(["external-inputs", "--spec", str(bad)]) == 2
    assert "ghost" in capsys.readouterr().out


def test_old_emit_subcommands_are_gone(capsys):
    for argv in (["emit", "--spec", "x.json"], ["validate", "--spec", "x.json"], ["bundle", "--spec", "x.json"]):
        with pytest.raises(SystemExit):
            emit.main(argv)
    capsys.readouterr()


def test_cli_bundle_writes_the_draft_when_faasr_cannot_wrap_a_step(tmp_path, capsys, structural_only):
    """A step FaaSr cannot wrap (its function defines a top-level name equal to the step's) still gets a
    valid draft; only the FaaSr target is marked as not buildable."""
    spec = single_node_spec()
    spec.nodes[0].code = "calculate_number_summary = 1\n\ndef main(inputs, outputs, params):\n    pass\n"
    run = run_with(tmp_path, spec)
    code = emit.main(["bundle", "--run", str(run)])
    out = json.loads(capsys.readouterr().out)
    state = json.loads((run / "state.json").read_text())
    assert state["stage"] == "gate4" and state["draft_dir"] and "bundle_dir" not in state
    v = state["validation"]
    assert v["ok"] is True and v["faasr"]["ok"] is False and "top-level" in v["faasr"]["error"]
    assert code == 0 and out["ok"]
