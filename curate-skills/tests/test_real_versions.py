"""The two published versions that ran on real Pegasus and real FaaSr in phase 7, as permanent
references (fixtures/real/<workflow>/):

  spec.json       the spec Gate 4 approved in the run that published the version
  version/        the published version folder (workflow.yml, inputs.yml, functions/)
  identity.json   workflow id, content hash, version hash, and the runs below
  pegasus-ir.yaml the Pegasus bundle of the phase-7 Pegasus run, the expanded IR v0.2 jobs the old
                  backend planned (weather: the host image path replaced by a neutral one, passed to
                  the build the same way); kept as the record of what ran
  pegasus-workflow.yml  the native Pegasus workflow the version builds today (scripts/cwl_pegasus.py)
  faasr-bundle/   the FaaSr bundle of the phase-7 FaaSr run

Rebuilding the approved spec gives the published workflow.yml byte for byte (publish's identity
fields included) and its content hash; building the version today gives the bundles that ran.
Weather v1 was published before curate:name, so its rebuild is compared with curate:name taken
out, the same way execution.recover_names checks it.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "scripts"))
sys.path.insert(0, str(PLUGIN / "curate_ir"))
import cwl_faasr as CF  # noqa: E402
import execution as X  # noqa: E402
import ids  # noqa: E402
import spec_cwl  # noqa: E402
from curate_models import WorkflowSpec  # noqa: E402

REAL = PLUGIN / "tests" / "fixtures" / "real"
WORKFLOWS = ["weather", "mapreduce"]


def ref(which):
    d = REAL / which
    return d, json.loads((d / "spec.json").read_text()), json.loads((d / "identity.json").read_text())


def files(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_the_references_are_the_two_kinds():
    assert "curate:name" not in (REAL / "weather" / "version" / "workflow.yml").read_text()
    assert "curate:name" in (REAL / "mapreduce" / "version" / "workflow.yml").read_text()


@pytest.mark.parametrize("which", WORKFLOWS)
def test_the_fixture_is_the_published_version(which):
    d, _, ident = ref(which)
    assert ids.version_hash(d / "version") == ident["version_hash"]
    assert ids.draft_content_hash(d / "version") == ident["content_hash"]
    doc = yaml.safe_load((d / "version" / "workflow.yml").read_text())
    assert (doc["s:identifier"], doc["s:version"], doc["curate:content_hash"]) == \
        (ident["workflow_id"], str(ident["version"]), ident["content_hash"])


@pytest.mark.parametrize("which", WORKFLOWS)
def test_rebuilding_the_approved_spec_gives_the_published_version_byte_for_byte(tmp_path, which):
    d, spec, ident = ref(which)
    built = spec_cwl.build(WorkflowSpec.model_validate(spec))
    if "curate:name" not in (d / "version" / "workflow.yml").read_text():
        built["workflow"] = X._without_names(built["workflow"])
    draft = spec_cwl.write(built, tmp_path / "draft")
    pub = ids.publish(draft, ident["workflow_name"], tmp_path / "project", ident["version"], ids.tree_hash(draft))
    assert pub["workflow_id"] == ident["workflow_id"]
    assert pub["content_hash"] == ident["content_hash"]
    assert files(Path(pub["dir"])) == files(d / "version")
    assert pub["version_hash"] == ident["version_hash"]


@pytest.mark.parametrize("which", WORKFLOWS)
def test_the_version_builds_the_pegasus_jobs_that_ran(which):
    """The jobs the version expands to today are exactly the phase-7 run's (its ir.yaml nodes: ids, steps,
    every file, params, order), with its deliverables and image; the native workflow is the reference."""
    import cwl_pegasus as CP
    d, spec, ident = ref(which)
    ran = yaml.safe_load((d / "pegasus-ir.yaml").read_text())
    s = {"version_dir": str(d / "version"), "spec": spec}
    built = X.pegasus_built(s, None, ident["pegasus"]["name"], ident["pegasus"]["image"])
    assert built["jobs"] == ran["nodes"]
    assert built["deliverables"] == ran["outputs"]
    assert {t["image"] for t in CP.metadata(built["workflow"])["transformations"].values()} == {ran["defaults"]["image"]}
    assert built["workflow"]["name"] == ran["name"] and len(ran["nodes"]) == {"weather": 5, "mapreduce": 11}[which]
    assert CP.dump(built["workflow"]) == (d / "pegasus-workflow.yml").read_text()


@pytest.mark.parametrize("which", WORKFLOWS)
def test_the_version_builds_the_faasr_bundle_that_ran(tmp_path, which):
    d, spec, _ = ref(which)
    s = {"version_dir": str(d / "version"), "spec": spec}
    out = CF.write(X.faasr_built(s, None), tmp_path / "bundle")
    assert files(out) == files(d / "faasr-bundle")


def fingerprint(which: str, tmp: str) -> dict:
    """What a fresh process builds from a real version: the published workflow.yml (rebuilt from the
    approved spec and published), its content and version hashes, the FaaSr bundle and the Pegasus
    workflow, as sha256s."""
    import cwl_pegasus as CP
    d, spec, ident = ref(which)
    tmp = Path(tmp)
    built = spec_cwl.build(WorkflowSpec.model_validate(spec))
    if "curate:name" not in (d / "version" / "workflow.yml").read_text():
        built["workflow"] = X._without_names(built["workflow"])
    draft = spec_cwl.write(built, tmp / "draft")
    pub = ids.publish(draft, ident["workflow_name"], tmp / "project", ident["version"], ids.tree_hash(draft))
    s = {"version_dir": str(d / "version"), "spec": spec}
    bundle = CF.write(X.faasr_built(s, None), tmp / "bundle")
    peg = X.pegasus_built(s, None, ident["pegasus"]["name"], ident["pegasus"]["image"])
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    return {"workflow.yml": sha((Path(pub["dir"]) / "workflow.yml").read_bytes()),
            "content_hash": pub["content_hash"], "version_hash": pub["version_hash"],
            "faasr-bundle": {k: sha(v) for k, v in files(bundle).items()},
            "pegasus-workflow.yml": sha(CP.dump(peg["workflow"]).encode())}


@pytest.mark.parametrize("which", WORKFLOWS)
def test_the_same_bytes_under_any_hash_seed(tmp_path, which):
    """Set and frozenset order follows PYTHONHASHSEED: three processes with different seeds build the
    same published workflow.yml, content hash, version hash, FaaSr bundle and Pegasus workflow, and
    they are the references."""
    code = ("import json, sys; sys.path[:0] = [sys.argv[1] + '/scripts', sys.argv[1] + '/curate_ir', "
            "sys.argv[1] + '/tests']\nfrom test_real_versions import fingerprint\n"
            "print(json.dumps(fingerprint(sys.argv[2], sys.argv[3])))\n")
    got = []
    for seed in ("1", "2", "3"):
        p = subprocess.run([sys.executable, "-c", code, str(PLUGIN), which, str(tmp_path / seed)],
                           capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": seed})
        assert p.returncode == 0, p.stderr[-3000:]
        got.append(json.loads(p.stdout))
    assert got[0] == got[1] == got[2]
    d, _, ident = ref(which)
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    assert got[0] == {"workflow.yml": sha((d / "version" / "workflow.yml").read_bytes()),
                      "content_hash": ident["content_hash"], "version_hash": ident["version_hash"],
                      "faasr-bundle": {k: sha(v) for k, v in files(d / "faasr-bundle").items()},
                      "pegasus-workflow.yml": sha((d / "pegasus-workflow.yml").read_bytes())}


def test_weather_v1_needs_the_curate_name_fallback():
    d, spec, _ = ref("weather")
    assert all(p.name is None for s in X.load_workflow(d / "version").steps.values() for p in s.op_outputs.values())
    wf = X.load_workflow(d / "version", {"spec": spec})
    assert all(p.name for s in wf.steps.values() for p in s.op_outputs.values())
