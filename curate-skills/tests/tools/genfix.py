"""Regenerate the reference fixtures from the current code. Not run by the test suite.

  uv run --python 3.13 --with-requirements tests/requirements.txt python tests/tools/genfix.py \
      [expected] [cwl] [real --ref ~/curate-ref] [--update]

Without --update nothing is written: each reference that would change is listed and the exit code
is 1 (0 when everything already matches). With --update the changed files are written. A reference
changes only after you have checked why (e.g. commit 4 compares the native Pegasus workflow with the
stored phase-7 ir.yaml before updating).

  expected  tests/fixtures/cwl-ref/<case>/expected.json (expanded jobs, Pegasus build, FaaSr build)
            and cwl-ref/e2-stages/runs.json (what both e2 bundles write when they run)
  cwl       the CWL folders of the spec cases (spec.json present), rebuilt by spec_cwl
  real      tests/fixtures/real/<workflow>/ from the published versions and runs in --ref:
            <ref>/<workflow>-v1 (version folder, runs/<id>/bundle) and <ref>/<workflow>-run (the
            run that approved it); the host image path in the Pegasus bundle is replaced by
            NEUTRAL_IMAGE

The CWL folders of the other cases (diamond, fanout and the constructed ones: list-folder-markers,
external-family, two-axis-gather, axis-types, partial-gather) were converted once from IR v0.2
(curate_ir/cwl/from_ir.py, removed in phase 8); they are fixed inputs now and are not regenerated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(PLUGIN / "scripts"), str(PLUGIN / "curate_ir"), str(PLUGIN / "tests")]
FX = PLUGIN / "tests" / "fixtures"
REF = FX / "cwl-ref"
REAL = FX / "real"
NEUTRAL_IMAGE = "file:///images/curate-weather.tar"
REAL_RUNS = {"weather": ("20261124-204455-0e28", "20261124-210945-a884"),   # (Pegasus run, FaaSr run)
             "mapreduce": ("20261124-220635-7263", "20261124-221815-6052")}
RECORDS = ("spec.json", "expected.json", "runs.json")


# ── what the current code makes ──────────────────────────────────────────────

def norm(obj, folder: Path):
    return json.loads(json.dumps(obj).replace(str(Path(folder).resolve()), "<fixture>"))


def dump(obj) -> str:
    return json.dumps(obj, indent=1) + "\n"


def load_cwl(folder: Path):
    from cwl.validate import validate
    wf, errors, _ = validate(folder / "workflow.yml", folder / "inputs.yml")
    if errors:
        raise SystemExit(f"{folder}: the workflow does not validate: {errors}")
    return wf


def expected_of(folder: Path) -> dict:
    import cwl_faasr as CF
    import cwl_pegasus as CP
    from cwl.expand_cwl import expand_cwl
    wf = load_cwl(folder)
    exp = {"jobs": norm(expand_cwl(wf), folder)}
    try:
        b = CP.build(wf, "t")
        exp["pegasus"] = norm({k: b[k] for k in ("ir", "copy", "needed")}, folder)
    except CP.BuildError as e:
        exp["pegasus"] = {"problems": e.problems}
    try:
        b = CF.build(wf, "t", "tf", "My_GitHub_Account", "S3")
        exp["faasr"] = norm({k: b[k] for k in ("workflow", "names", "outputs", "external_inputs")}, folder)
    except CF.BuildError as e:
        exp["faasr"] = {"problems": e.problems}
    return exp


E2_SEED = 20261123  # generate_text shuffles with the global random; the FaaSr stand-in runs in-process


def e2_runs() -> dict:
    """Run both e2 bundles. FaaSr (the stand-in, in-process, random seeded): every object and its
    sha256. Pegasus (ir_local, one subprocess per job, so unseeded): every file, and the word counts'
    sha256, which match FaaSr's."""
    import random
    import cwl_faasr as CF
    import cwl_pegasus as CP
    from curate_models import WorkflowSpec
    from ir_local import run_locally
    from test_faasr_wrapper import run_bundle
    d = REF / "e2-stages"
    wf = load_cwl(d)
    spec = WorkflowSpec.model_validate(json.loads((d / "spec.json").read_text()))
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    t = Path(tempfile.mkdtemp())
    b = CF.write(CF.build(wf, spec.name, spec.folder, spec.compute_target, spec.data_store), t / "bundle")
    random.seed(E2_SEED)
    run_bundle(t / "faasr", b, t / "s3")
    faasr = {p.relative_to(t / "s3").as_posix(): sha(p) for p in (t / "s3").rglob("*") if p.is_file()}
    files = run_locally(CP.write_folder(t / "peg", CP.build(wf, "t-flow")) / "ir.yaml", t / "jobs")
    peg = {k: sha(v) for k, v in files.items()}
    det = {k: v for k, v in sorted(peg.items()) if "word_count_" in k}
    if {k.split("/", 1)[1]: v for k, v in faasr.items() if "word_count_" in k} != det:
        raise SystemExit("the e2 word counts differ between FaaSr and Pegasus; not recording them")
    return {"faasr_objects": dict(sorted(faasr.items())), "pegasus_files": sorted(peg), "results_sha256": det}


def spec_cwl_files(folder: Path) -> dict[str, bytes]:
    import spec_cwl
    from curate_models import WorkflowSpec
    spec = WorkflowSpec.model_validate(json.loads((folder / "spec.json").read_text()))
    out = spec_cwl.write(spec_cwl.build(spec), Path(tempfile.mkdtemp()) / "cwl")
    return {p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()}


def real_files(ref: Path, which: str) -> dict[str, bytes]:
    import yaml
    pr, fr = REAL_RUNS[which]
    vdir, run = ref / f"{which}-v1", ref / f"{which}-run"
    snap = json.loads((run / "history" / "004-gate4-approve.json").read_text())
    st = json.loads((run / "state.json").read_text())
    out = {"spec.json": (json.dumps(snap["spec"], indent=2) + "\n").encode()}
    for p in [vdir / "workflow.yml", vdir / "inputs.yml", *sorted((vdir / "functions").rglob("*"))]:
        if p.is_file():
            out["version/" + p.relative_to(vdir).as_posix()] = p.read_bytes()
    ir = (vdir / "runs" / pr / "bundle" / "ir.yaml").read_text()
    doc = yaml.safe_load(ir)
    img = doc["defaults"]["image"]
    if "/home/" in img or "/Users/" in img:
        ir, img = ir.replace(img, NEUTRAL_IMAGE), NEUTRAL_IMAGE
    out["pegasus-ir.yaml"] = ir.encode()
    fb = vdir / "runs" / fr / "bundle"
    for p in sorted(fb.rglob("*")):
        if p.is_file():
            out["faasr-bundle/" + p.relative_to(fb).as_posix()] = p.read_bytes()
    ident = {"workflow_name": st["workflow_name"], "workflow_id": st["workflow_id"], "version": st["workflow_version"],
             "content_hash": st["workflow_content_hash"],
             "pegasus": {"run": f"{st['workflow_id']}/{pr}", "name": doc["name"], "image": img},
             "faasr": {"run": f"{st['workflow_id']}/{fr}"}, "version_hash": st["version_hash"]}
    out["identity.json"] = (json.dumps(ident, indent=2) + "\n").encode()
    return out


# ── compare / write ──────────────────────────────────────────────────────────

class Plan:
    def __init__(self):
        self.changes: list[tuple[Path, bytes | None]] = []  # (path, new bytes or None = delete)

    def file(self, path: Path, data: bytes | str):
        data = data.encode() if isinstance(data, str) else data
        if not path.is_file() or path.read_bytes() != data:
            self.changes.append((path, data))

    def tree(self, root: Path, files: dict[str, bytes], keep=RECORDS):
        """root's files become exactly `files` (besides the record files in `keep`)."""
        for rel, data in files.items():
            self.file(root / rel, data)
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                rel = p.relative_to(root).as_posix()
                if p.is_file() and rel not in files and p.name not in keep:
                    self.changes.append((p, None))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", nargs="*", choices=["expected", "cwl", "real"], default=["expected"])
    ap.add_argument("--ref", type=Path, help="folder with <workflow>-v1 and <workflow>-run (for `real`)")
    ap.add_argument("--update", action="store_true", help="write the references that changed")
    a = ap.parse_args(argv)
    plan = Plan()
    if "cwl" in a.what:
        for d in sorted(p for p in REF.iterdir() if (p / "spec.json").is_file()):
            plan.tree(d, spec_cwl_files(d))
        if a.update and "expected" in a.what:  # the expected outputs are made from the CWL just written
            n = apply(plan)
            print(f"{n} CWL file(s) written; now the expected outputs")
            plan = Plan()
    if "expected" in a.what:
        for d in sorted(p for p in REF.iterdir() if (p / "workflow.yml").is_file()):
            plan.file(d / "expected.json", dump(expected_of(d)))
        plan.file(REF / "e2-stages" / "runs.json", dump(e2_runs()))
    if "real" in a.what:
        if not a.ref:
            raise SystemExit("`real` needs --ref <folder with weather-v1, weather-run, mapreduce-v1, mapreduce-run>")
        for which in REAL_RUNS:
            plan.tree(REAL / which, real_files(a.ref.expanduser(), which), keep=())
    if not a.update:
        for path, data in plan.changes:
            print(("delete " if data is None else "write  " if not path.exists() else "change ")
                  + str(path.relative_to(PLUGIN)))
    if not plan.changes:
        print("all references match the current code")
        return 0
    if not a.update:
        print(f"{len(plan.changes)} reference file(s) would change; nothing written (pass --update to write them)")
        return 1
    print(f"{apply(plan)} reference file(s) written")
    return 0


def apply(plan: Plan) -> int:
    for path, data in plan.changes:
        print(("delete " if data is None else "write  ") + str(path.relative_to(PLUGIN)))
        if data is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    return len(plan.changes)


if __name__ == "__main__":
    sys.exit(main())
