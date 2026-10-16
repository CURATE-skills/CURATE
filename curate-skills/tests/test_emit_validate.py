import json

import pytest

import emit
from curate_models import FunctionSpec, IOSpec, WorkflowEdge, WorkflowSpec, clean_dependencies

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
            code="def calculate_number_summary(folder, input1, output1):\n    pass\n",
        )],
    )


def mapreduce_spec():
    return WorkflowSpec(
        name="word_count",
        entry="split_words",
        nodes=[
            FunctionSpec(name="split_words", inputs=[IOSpec(name="words.txt")],
                         outputs=[IOSpec(name="chunk_{rank}.txt")]),
            FunctionSpec(name="map_words", rank=5, inputs=[IOSpec(name="chunk_{rank}.txt")],
                         outputs=[IOSpec(name="counts_{rank}.json")]),
            FunctionSpec(name="reduce_counts", inputs=[IOSpec(name="counts_1.json")],
                         outputs=[IOSpec(name="totals.json")]),
        ],
        edges=[WorkflowEdge(from_node="split_words", to_node="map_words"),
               WorkflowEdge(from_node="map_words", to_node="reduce_counts")],
    )


# ── ported from ref/curate tests/test_faasr_validation.py ──

def test_single_terminal_action_is_a_valid_dag():
    wf = emit.emit_faasr_json(single_node_spec())
    assert wf["FunctionInvoke"] == "calculate-number-summary"
    assert wf["ActionList"]["calculate-number-summary"]["InvokeNext"] == []
    assert emit.structural_validate(wf) == (True, None)


def test_single_action_still_requires_a_valid_entry():
    wf = emit.emit_faasr_json(single_node_spec())
    wf["FunctionInvoke"] = "missing-action"
    ok, err = emit.structural_validate(wf)
    assert not ok and "FunctionInvoke" in err


@pytest.fixture(scope="module")
def faasr_py_available():
    res = emit.faasr_py_validate(emit.emit_faasr_json(single_node_spec()))
    if res is None:
        pytest.skip("FaaSr_py validator could not run")
    return res


def test_faasr_py_accepts_single_action_with_placeholders(faasr_py_available):
    assert faasr_py_available == (True, None)


def test_faasr_py_rejects_bad_entry(faasr_py_available):
    wf = emit.emit_faasr_json(single_node_spec())
    wf["FunctionInvoke"] = "missing-action"
    ok, err = emit.faasr_py_validate(wf)
    assert not ok and "FunctionInvoke" in err


def test_faasr_py_accepts_ranked_workflow(faasr_py_available):
    assert emit.faasr_py_validate(emit.emit_faasr_json(mapreduce_spec())) == (True, None)


# ── emitter behaviour ──

def test_ranked_target_is_invoked_as_name_n():
    wf = emit.emit_faasr_json(mapreduce_spec())
    assert wf["ActionList"]["split-words"]["InvokeNext"] == ["map-words(5)"]
    assert wf["ActionList"]["map-words"]["InvokeNext"] == ["reduce-counts"]


def test_conditional_edges_become_true_false_branches():
    spec = WorkflowSpec(
        name="branchy", entry="check",
        nodes=[FunctionSpec(name="check"), FunctionSpec(name="good"), FunctionSpec(name="bad")],
        edges=[WorkflowEdge(from_node="check", to_node="good", condition="True"),
               WorkflowEdge(from_node="check", to_node="bad", condition="False")],
    )
    wf = emit.emit_faasr_json(spec)
    assert wf["ActionList"]["check"]["InvokeNext"] == [{"True": ["good"], "False": ["bad"]}]
    assert emit.structural_validate(wf) == (True, None)


def test_dependencies_drop_stdlib_and_faasr():
    assert clean_dependencies(["numpy", "os", "faasr", "FaaSr_py", "json", "pandas", "numpy"]) == [
        "numpy", "pandas"]
    spec = single_node_spec()
    spec.nodes[0].dependencies = ["re", "matplotlib"]
    assert emit.emit_faasr_json(spec)["PyPIPackageDownloads"] == {
        "calculate_number_summary": ["matplotlib"]}


def test_external_inputs_tolerate_rank_templates():
    ext = emit.external_inputs(mapreduce_spec())
    assert [e["file"] for e in ext] == ["words.txt"]
    assert ext[0]["s3_path"] == "workflow_data/words.txt"


def test_secret_names_only_in_workflow():
    spec = single_node_spec()
    spec.nodes[0].secrets = ["API_TOKEN"]
    wf = emit.emit_faasr_json(spec)
    assert wf["Secrets"] == ["API_TOKEN"]
    assert wf["DataStores"]["S3"]["AccessKey"] == "S3_AccessKey"


# ── placeholders ──

def test_unset_faasr_vars_become_placeholders():
    wf = emit.emit_faasr_json(single_node_spec())
    assert wf["ComputeServers"]["GH"]["UserName"] == "<FAASR_GH_USERNAME>"
    assert wf["DataStores"]["S3"]["Bucket"] == "<FAASR_S3_BUCKET>"
    assert wf["FunctionGitRepo"]["calculate_number_summary"].startswith(
        "<FAASR_GH_USERNAME>/<FAASR_ACTION_REPO>/main/")
    assert emit.placeholders_used() == list(emit.PLACEHOLDER_VARS)


def test_set_faasr_vars_are_used(monkeypatch):
    monkeypatch.setenv("FAASR_GH_USERNAME", "octo")
    monkeypatch.setenv("FAASR_S3_BUCKET", "my-bucket")
    wf = emit.emit_faasr_json(single_node_spec())
    assert wf["ComputeServers"]["GH"]["UserName"] == "octo"
    assert wf["DataStores"]["S3"]["Bucket"] == "my-bucket"
    assert "FAASR_GH_USERNAME" not in emit.placeholders_used()


# ── validation fallback ──

def test_structural_fallback_when_faasr_py_unavailable(monkeypatch):
    monkeypatch.setattr(emit, "faasr_py_validate", lambda wf, timeout=300: None)
    res = emit.validate(emit.emit_faasr_json(mapreduce_spec()))
    assert res["ok"] and res["level"] == "structural" and res["fallback_reason"]
    forced = emit.validate(emit.emit_faasr_json(mapreduce_spec()), level="faasr_py")
    assert not forced["ok"] and forced["level"] == "faasr_py"


def test_structural_catches_cycle_unreachable_and_bad_target():
    wf = emit.emit_faasr_json(mapreduce_spec())
    wf["ActionList"]["reduce-counts"]["InvokeNext"] = ["split-words"]
    assert "cycle" in emit.structural_validate(wf)[1]

    wf = emit.emit_faasr_json(mapreduce_spec())
    wf["ActionList"]["map-words"]["InvokeNext"] = []
    assert "unreachable" in emit.structural_validate(wf)[1]

    wf = emit.emit_faasr_json(mapreduce_spec())
    wf["ActionList"]["map-words"]["InvokeNext"] = ["nowhere"]
    assert "nowhere" in emit.structural_validate(wf)[1]


# ── bundle + secrets guard ──

@pytest.fixture
def structural_only(monkeypatch):
    monkeypatch.setattr(emit, "faasr_py_validate", lambda wf, timeout=300: None)


def _all_text(d):
    return "".join(p.read_text() for p in d.rglob("*") if p.is_file())


def test_bundle_layout_and_names_only(tmp_path, monkeypatch, structural_only):
    monkeypatch.setenv("S3_SecretKey", "s3-secret-value-xyz")
    monkeypatch.setenv("API_TOKEN", "api-token-value-123")
    spec = single_node_spec()
    spec.nodes[0].secrets = ["API_TOKEN"]
    spec.nodes[0].dependencies = ["numpy"]
    res = emit.write_bundle(spec, tmp_path)
    out = tmp_path / "local_function_test"
    assert res["bundle_dir"] == str(out)
    for f in ("workflow.json", "spec.json", "external_inputs.json", "README.md",
              "functions/calculate_number_summary.py",
              "functions/calculate_number_summary.deps.txt"):
        assert (out / f).is_file(), f
    text = _all_text(out)
    assert "s3-secret-value-xyz" not in text and "api-token-value-123" not in text
    readme = (out / "README.md").read_text()
    assert "`API_TOKEN`" in readme and "`S3_SecretKey`" in readme
    assert "<FAASR_S3_BUCKET>" in readme and "structural" in readme


def test_bundle_refuses_leaked_secret_value(tmp_path, monkeypatch, structural_only):
    monkeypatch.setenv("API_TOKEN", "api-token-value-123")
    spec = single_node_spec()
    spec.nodes[0].secrets = ["API_TOKEN"]
    spec.nodes[0].code += 'TOKEN = "api-token-value-123"\n'
    res = emit.write_bundle(spec, tmp_path)
    assert not res["ok"] and res["leaked_secret_names"] == ["API_TOKEN"]
    assert not (tmp_path / "local_function_test").exists()
    assert "api-token-value-123" not in json.dumps(res)


def test_bundle_never_overwrites(tmp_path, structural_only):
    first = emit.write_bundle(single_node_spec(), tmp_path)["bundle_dir"]
    second = emit.write_bundle(single_node_spec(), tmp_path)["bundle_dir"]
    assert second == first + "-2"


def test_bundle_reports_missing_code(tmp_path, structural_only):
    res = emit.write_bundle(mapreduce_spec(), tmp_path)
    assert not res["ok"] and len(res["missing_code"]) == 3


def test_cli_bundle_updates_run_state(tmp_path, capsys, structural_only):
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({"spec": json.loads(single_node_spec().model_dump_json())}))
    code = emit.main(["bundle", "--run", str(run), "--out", str(tmp_path / "out")])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"]
    state = json.loads((run / "state.json").read_text())
    assert state["bundle_dir"] == out["bundle_dir"]
    assert state["validation"] == {"ok": True, "level": "structural", "error": None}
    assert state["stage"] == "gate4"


def test_cli_rejects_bad_spec(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "x", "nodes": [{"name": "a"}],
                               "edges": [{"from_node": "a", "to_node": "ghost"}]}))
    assert emit.main(["emit", "--spec", str(bad)]) == 2
    assert "ghost" in capsys.readouterr().out
