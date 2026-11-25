"""A version published before curate:name (8e8c9f9) still deploys with the spec's file names.

fixtures/pre-curate-name-weather/ is the weather spec's workflow as the code at 2d22eec wrote it
(no curate:name; outputs written as plain `File`). Versions are never edited, so execution.py
recovers the names in memory from the run's approved spec, after checking that the spec builds
exactly that version (same content hash once curate:name is taken out).
"""
import json
import shutil
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "scripts"))
sys.path.insert(0, str(PLUGIN / "curate_ir"))
import cwl_faasr as CF  # noqa: E402
import execution as X  # noqa: E402
import ids  # noqa: E402
import spec_cwl  # noqa: E402
from curate_models import WorkflowSpec  # noqa: E402

FIXTURES = PLUGIN / "tests" / "fixtures"
OLD = FIXTURES / "pre-curate-name-weather"
SPEC = json.loads((FIXTURES / "weather-spec.json").read_text())
PNG = "plot_weather_comparison__output1__corvallis_winter_comparison.png"


@pytest.fixture
def version(tmp_path):
    d = tmp_path / "corvallis-winter-weather-v1"
    shutil.copytree(OLD, d)
    return d


def state(version, spec=SPEC):
    return {"version_dir": str(version), "spec": spec}


def test_the_fixture_is_what_the_old_code_wrote():
    assert "curate:name" not in (OLD / "workflow.yml").read_text()
    built = spec_cwl.build(WorkflowSpec.model_validate(SPEC))
    # taking curate:name out of today's build gives the old version's content hash
    assert spec_cwl.content_hash(X._without_names(built["workflow"]), built["functions"]) == \
        ids.draft_content_hash(OLD)
    assert spec_cwl.content_hash(built["workflow"], built["functions"]) != ids.draft_content_hash(OLD)


def test_without_the_run_state_the_old_version_has_no_names(version):
    wf = X.load_workflow(version)
    assert all(p.name is None for s in wf.steps.values() for p in s.op_outputs.values())


def test_both_builders_get_the_spec_names_back_and_the_version_is_unchanged(version):
    before = (ids.tree_hash(version), ids.draft_content_hash(version))
    s = state(version)
    plot = next(j for j in X.pegasus_built(s, None, "t", None)["ir"]["nodes"]
                if j["_origin"] == "plot_weather_comparison")
    assert plot["outputs"]["output1"] == PNG
    spec = WorkflowSpec.model_validate(SPEC)
    built = X.faasr_built(s, None)
    args = next(a["Arguments"] for a in built["workflow"]["ActionList"].values()
                if a["FunctionName"] == "plot_weather_comparison")
    assert args["output1"] == PNG
    assert args["input1"] == "process_weather_variable__output1__{rank}__winter_comparison_{rank}.csv"
    assert (ids.tree_hash(version), ids.draft_content_hash(version)) == before
    # the same as building today's workflow (with curate:name) from the spec
    tmp = version.parent / "new"
    spec_cwl.write(spec_cwl.build(spec), tmp)
    assert built["workflow"] == X.faasr_built({"version_dir": str(tmp), "spec": SPEC}, None)["workflow"]


def test_every_generated_name_keeps_its_extension(version):
    wf = X.load_workflow(version, state(version))
    for sid, step in wf.steps.items():
        for out, port in step.op_outputs.items():
            assert port.name and Path(port.name).suffix, (sid, out, port.name)
    built = CF.build(wf, "t", "f", "My_GitHub_Account", "S3")
    assert all(Path(n.replace("{rank}", "1")).suffix for n in built["names"].values())


def test_a_spec_that_did_not_build_this_version_is_refused(version):
    other = json.loads(json.dumps(SPEC))
    node = next(n for n in other["nodes"] if n["name"] == "plot_weather_comparison")
    node["code"] = (node.get("code") or "") + "\n# changed after v1\n"
    with pytest.raises(X.ExecutionError) as e:
        X.load_workflow(version, state(version, other))
    msg = str(e.value)
    assert "curate:name" in msg and "no longer builds exactly this version" in msg
    assert "--start-at emit --from-run" in msg


def test_a_version_with_names_is_not_rebuilt(tmp_path, monkeypatch):
    new = tmp_path / "v2"
    spec_cwl.write(spec_cwl.build(WorkflowSpec.model_validate(SPEC)), new)
    monkeypatch.setattr(spec_cwl, "build", lambda *a, **k: pytest.fail("rebuilt a version that has names"))
    wf = X.load_workflow(new, state(new))
    assert wf.steps["plot_weather_comparison"].op_outputs["output1"].name == "corvallis_winter_comparison.png"
