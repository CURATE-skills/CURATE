"""scripts/ids.py: workflow and run IDs, publish on approval, bundle manifests (phase 6b)."""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "curate_ir"))
import catalog as C  # noqa: E402
import ids  # noqa: E402
import spec_cwl as SC  # noqa: E402
from curate_models import WorkflowSpec  # noqa: E402

E2 = PLUGIN / "tests" / "fixtures" / "e2-stages" / "spec-with-code.json"


def e2():
    return WorkflowSpec.model_validate(json.loads(E2.read_text()))


def draft(run_dir, spec=None):
    return SC.write(SC.build(spec or e2()), ids.draft_dir(run_dir))


def test_names_and_ids():
    assert ids.slug("Corvallis Winter: ice!") == "corvallis-winter-ice"
    assert ids.workflow_id("corvallis-winter", 3) == "corvallis-winter-v3"
    assert ids.parse_workflow_id("wfh-1063-v2") == ("wfh-1063", 2)
    for bad in ("Bad Name", "a--b", "-a", "name-v2"):
        with pytest.raises(ids.IdError):
            ids.check_name(bad)
    with pytest.raises(ids.IdError):
        ids.parse_workflow_id("no-version")


def test_run_ids_keep_the_timestamp_prefix_the_deploy_names_parse():
    rid = ids.new_run_id(datetime(2026, 11, 18, 1, 29, 3), "a3f9")
    assert rid == "20261118-012903-a3f9" and ids.RUN_ID.match(rid)
    assert ids.RUN_ID.match(ids.new_run_id())
    import deploy
    import pegasus_ir
    assert deploy.RUN_TS_RX.match(rid) and pegasus_ir.run_ir_name("t", rid) == "t-1118-0129"
    assert ids.reference("corvallis-winter-v3", rid) == "corvallis-winter-v3/20261118-012903-a3f9"


def test_publish_only_the_approved_draft_atomically_and_read_only(tmp_path):
    project, run = tmp_path / "p", tmp_path / "p" / ".curate" / "runs" / "r1"
    d = draft(run)
    plan = ids.plan_publish(d, "mapreduce", project)
    assert plan["workflow_id"] == "mapreduce-v1" and plan["duplicates"] == []
    approved = plan["draft_hash"]
    pub = ids.publish(d, "mapreduce", project, plan["version"], approved)
    dest = ids.version_dir(project, "mapreduce-v1")
    assert pub["dir"] == str(dest)
    doc = yaml.safe_load((dest / "workflow.yml").read_text())
    assert doc["s:identifier"] == "mapreduce-v1" and doc["s:version"] == "1"
    assert doc["curate:content_hash"] == plan["content_hash"] and doc["$namespaces"]["s"] == "https://schema.org/"
    assert ids.draft_content_hash(dest) == plan["content_hash"]  # the published folder hashes like its draft
    assert list(doc)[:3] == ["cwlVersion", "class", "$namespaces"]
    assert not (dest / "functions" / "map.py").stat().st_mode & 0o222
    assert not any(p.name.startswith(".mapreduce-v1.tmp") for p in dest.parent.iterdir())
    ids.check_version(project, "mapreduce-v1", pub["version_hash"])
    with pytest.raises(ids.IdError, match="never overwritten"):
        ids.publish(d, "mapreduce", project, 1, approved)
    # a draft changed after approval is not published
    (d / "functions" / "map.py").write_text("# edited\n")
    with pytest.raises(ids.IdError, match="changed since Gate 4 approved it"):
        ids.publish(d, "mapreduce", project, 2, approved)


def test_the_published_workflow_is_valid_cwl(tmp_path):
    from cwl.validate import validate
    project = tmp_path / "p"
    d = draft(tmp_path / "r")
    plan = ids.plan_publish(d, "mapreduce", project)
    pub = ids.publish(d, "mapreduce", project, plan["version"], plan["draft_hash"])
    wf, errors, _ = validate(Path(pub["dir"]) / "workflow.yml")
    assert errors == [] and wf.identifier == "mapreduce-v1" and wf.version == "1"


def test_next_versions_and_duplicates_from_project_and_catalog(tmp_path):
    project = tmp_path / "p"
    d1 = draft(tmp_path / "r1")
    p1 = ids.plan_publish(d1, "mapreduce", project)
    ids.publish(d1, "mapreduce", project, p1["version"], p1["draft_hash"])
    d2 = draft(tmp_path / "r2")
    p2 = ids.plan_publish(d2, "mapreduce", project)
    assert p2["workflow_id"] == "mapreduce-v2" and p2["duplicates"] == ["mapreduce-v1"]
    changed = e2()
    changed.nodes[2].code += "\n# changed\n"
    d3 = draft(tmp_path / "r3", changed)
    assert ids.plan_publish(d3, "mapreduce", project)["duplicates"] == []
    cat = C.Catalog(tmp_path / "cat")
    C.save_workflow(cat, e2(), workflow_id="mapreduce-v7")
    assert ids.plan_publish(d3, "mapreduce", project, catalog=cat)["workflow_id"] == "mapreduce-v8"
    assert ids.plan_publish(d2, "mapreduce", project, catalog=cat)["duplicates"] == ["mapreduce-v1", "mapreduce-v7"]


def test_name_status_against_the_global_catalog(tmp_path):
    cat = C.Catalog(tmp_path / "cat")
    assert ids.name_status("mapreduce-word-count", cat)["taken"] is False
    C.save_workflow(cat, e2(), workflow_id="mapreduce-word-count-v1")
    st = ids.name_status("mapreduce-word-count", cat)
    assert st["taken"] and st["versions"] == [1]
    legacy = C.WorkflowEntry(workflow_spec=e2().model_copy(update={"name": "Old Weather"}))
    cat.save_workflow(legacy)
    assert ids.name_status("old-weather", cat) == {"name": "old-weather", "taken": True, "versions": [],
                                                   "legacy_entries": [legacy.id]}


def test_bundle_manifest_and_deploys_check(tmp_path):
    ex = tmp_path / "runs" / "20261118-012903-a3f9"
    (ex / "bundle").mkdir(parents=True)
    (ex / "bundle" / "workflow.json").write_text("{}")
    m = ids.write_manifest(ex, "v-hash", "faasr")
    assert set(m) == {"version_hash", "curate_version", "target", "bundle", "bundle_hash"}
    assert m["target"] == "faasr" and m["curate_version"] and m["bundle_hash"] == ids.tree_hash(ex / "bundle")
    assert re.match(r"^([0-9a-f]{40}(-dirty)?|plugin .+|unknown)$", m["curate_version"])
    assert ids.check_manifest(ex, "v-hash") == m
    with pytest.raises(ids.IdError, match="another version"):
        ids.check_manifest(ex, "other")
    (ex / "bundle" / "workflow.json").write_text('{"x": 1}')
    with pytest.raises(ids.IdError, match="changed after it was built"):
        ids.check_manifest(ex, "v-hash")


def test_set_name_checks_the_catalog_and_records_lineage(make_run, capsys):
    import spec as S
    from conftest import call
    run = make_run()
    s = json.loads((run / "state.json").read_text())
    cat = C.Catalog(Path(s["catalog_dir"]))
    C.save_workflow(cat, e2(), workflow_id="mapreduce-word-count-v1")
    code, out = call(S, ["set-name", "--run", str(run), "Bad Name"], capsys)
    assert code == 2 and "lower-case" in out["error"]
    code, out = call(S, ["set-name", "--run", str(run), "mapreduce-word-count"], capsys)
    assert code == 1 and out["needs_choice"] and out["taken"]["versions"] == [1]
    assert json.loads((run / "state.json").read_text()).get("workflow_name") is None
    code, out = call(S, ["set-name", "--run", str(run), "mapreduce-word-count", "--version-of", "other-v2"], capsys)
    assert code == 2 and "is a version of other" in out["error"]
    code, out = call(S, ["set-name", "--run", str(run), "mapreduce-word-count", "--new-version"], capsys)
    assert code == 0 and out["lineage"] == {"new_version_of": "mapreduce-word-count"}
    code, out = call(S, ["set-name", "--run", str(run), "fresh-name"], capsys)
    assert code == 0 and out["taken"] is False
    st = json.loads((run / "state.json").read_text())
    assert st["workflow_name"] == "fresh-name" and st["workflow_lineage"] is None  # the lineage was about another name
    code, out = call(S, ["set-name", "--run", str(run), "mapreduce-word-count"], capsys)
    assert code == 1 and out["needs_choice"]
