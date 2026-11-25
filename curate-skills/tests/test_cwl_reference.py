"""Fixed reference outputs for the CWL workflows (fixtures/cwl-ref/<case>/), recorded from the
phase-7-verified code. They replace the phase-4 parity tests against IR v0.2's expand.py, so the
same cases stay covered once the IR v0.2 code is gone.

Each case is a CWL folder (workflow.yml, inputs.yml, functions/, input files) with expected.json:
  jobs     cwl/expand_cwl.py's expansion: every job, its files, params and order
  pegasus  the Pegasus build (or the problems it refuses the workflow with)
  faasr    the FaaSr build: workflow, generated names, outputs, external inputs (or problems)
and, for e2-stages, runs.json: what both bundles write when they run (see the last test).
The spec cases (spec.json present) also check that spec_cwl builds that CWL byte for byte.
Paths inside the fixture are recorded as `<fixture>`.
"""
import json
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "scripts"))
sys.path.insert(0, str(PLUGIN / "curate_ir"))
import cwl_faasr as CF  # noqa: E402
import cwl_pegasus as CP  # noqa: E402
import spec_cwl  # noqa: E402
from curate_models import WorkflowSpec  # noqa: E402
from cwl.expand_cwl import expand_cwl  # noqa: E402
from cwl.validate import validate  # noqa: E402

REF = PLUGIN / "tests" / "fixtures" / "cwl-ref"
CASES = sorted(p.name for p in REF.iterdir() if (p / "workflow.yml").is_file())
SPEC_CASES = [c for c in CASES if (REF / c / "spec.json").is_file()]
RECORDS = ("spec.json", "expected.json", "runs.json")


def norm(obj, folder: Path):
    return json.loads(json.dumps(obj).replace(str(folder.resolve()), "<fixture>"))


def load(case):
    d = REF / case
    wf, errors, _ = validate(d / "workflow.yml", d / "inputs.yml")
    assert errors == [], errors
    return d, wf, json.loads((d / "expected.json").read_text())


def test_every_old_parity_case_has_a_reference():
    assert set(CASES) == {"diamond", "fanout", "e2-stages", "catalog-e2-mapreduce", "weather", "file-less-edge",
                          "list-folder-markers", "external-family", "two-axis-gather", "axis-types",
                          "partial-gather"}
    assert set(SPEC_CASES) == {"e2-stages", "catalog-e2-mapreduce", "weather", "file-less-edge"}


@pytest.mark.parametrize("case", CASES)
def test_jobs_match_the_reference(case):
    d, wf, want = load(case)
    assert norm(expand_cwl(wf), d) == want["jobs"]
    assert want["jobs"]["nodes"], "the reference has jobs"


@pytest.mark.parametrize("case", CASES)
def test_pegasus_build_matches_the_reference(case):
    d, wf, want = load(case)
    try:
        b = CP.build(wf, "t")
        got = norm({k: b[k] for k in ("ir", "copy", "needed")}, d)
    except CP.BuildError as e:
        got = {"problems": e.problems}
    assert got == want["pegasus"]


@pytest.mark.parametrize("case", CASES)
def test_faasr_build_matches_the_reference(case):
    d, wf, want = load(case)
    try:
        b = CF.build(wf, "t", "tf", "My_GitHub_Account", "S3")
        got = norm({k: b[k] for k in ("workflow", "names", "outputs", "external_inputs")}, d)
    except CF.BuildError as e:
        got = {"problems": e.problems}
    assert got == want["faasr"]


@pytest.mark.parametrize("case", SPEC_CASES)
def test_spec_cwl_builds_the_fixture_byte_for_byte(tmp_path, case):
    d = REF / case
    spec = WorkflowSpec.model_validate(json.loads((d / "spec.json").read_text()))
    out = spec_cwl.write(spec_cwl.build(spec), tmp_path / "cwl")
    got = {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    want = {p.relative_to(d).as_posix(): p.read_bytes() for p in d.rglob("*")
            if p.is_file() and p.name not in RECORDS}
    assert got == want


def test_the_mapreduce_reference_in_detail():
    """The references are not vacuous: the e2 workflow's 11 jobs, rank params as ints, the shuffle's
    folder input listing the three map outputs in rank order."""
    _, _, want = load("e2-stages")
    nodes = want["jobs"]["nodes"]
    assert len(nodes) == 11
    reduce3 = next(n for n in nodes if n["_origin"] == "reduce" and n["params"].get("rank") == 3)
    assert reduce3["params"] == {"rank": 3, "max_rank": 5}
    shuffle = next(n for n in nodes if n["_origin"] == "shuffle")
    folder = shuffle["inputs"]["input1"]["folder"]
    assert len(folder) == 3 and folder == sorted(folder)


def test_the_e2_runs_match_the_reference(tmp_path):
    """Both bundles run: FaaSr through test_faasr_wrapper's stand-in, Pegasus through ir_local (the
    runner shim, job by job). runs.json records every file each target writes and the sha256 of the
    five word counts (the rest depends on generate_text's unseeded shuffle)."""
    import hashlib
    sys.path.insert(0, str(PLUGIN / "tests"))
    from ir_local import run_locally
    from test_faasr_wrapper import run_bundle

    d, wf, _ = load("e2-stages")
    want = json.loads((d / "runs.json").read_text())
    spec = WorkflowSpec.model_validate(json.loads((d / "spec.json").read_text()))
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731

    bundle = CF.write(CF.build(wf, spec.name, spec.folder, spec.compute_target, spec.data_store), tmp_path / "bundle")
    run_bundle(tmp_path / "faasr", bundle, tmp_path / "s3")
    objects = {p.relative_to(tmp_path / "s3").as_posix(): p for p in (tmp_path / "s3").rglob("*") if p.is_file()}
    assert sorted(objects) == want["faasr_objects"]
    assert {k.split("/", 1)[1]: sha(p) for k, p in objects.items() if "word_count_" in k} == want["results_sha256"]

    folder = CP.write_folder(tmp_path / "pegasus", CP.build(wf, "t-flow"))
    files = run_locally(folder / "ir.yaml", tmp_path / "jobs")
    assert sorted(files) == want["pegasus_files"]
    assert {k: sha(p) for k, p in files.items() if "word_count_" in k} == want["results_sha256"]
