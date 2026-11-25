"""Execution runs: a target's bundle, built from the approved workflow version after Gate 4.

  <project>/.curate/workflows/<workflow-id>/runs/<run-id>/bundle/          the builder's output
  <project>/.curate/workflows/<workflow-id>/runs/<run-id>/manifest.json    ids.write_manifest

A run is "versioned" once emit wrote its draft (phase 6c on); Gate 4 approval publishes the draft as
a version, and every bundle is built from that version folder, never from the spec. The FaaSr
bundle is built when Gate 4 (or "Start this run?") chooses FaaSr; the Pegasus folder is built by
pegasus_deploy.py prepare (it needs the input files). Before approval, Gate 4 previews both targets
by building them in memory from the draft.

Runs approved before versions (no draft) keep the old path: the bundle emit wrote, checked by its
Gate 4 bundle hash. That path is removed with the old IR (CWL IR brief, phase 8).
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent / "curate_ir"))
import ids  # noqa: E402


class ExecutionError(Exception):
    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or [message]


def is_versioned(s: dict) -> bool:
    return bool(s.get("workflow_id") or s.get("draft_dir"))


def project_of(s: dict, run) -> Path:
    return Path(s.get("project_dir") or Path(run).resolve().parent.parent.parent)


def stale_draft(s: dict) -> str | None:
    """Why the draft no longer matches the spec (it was changed after emit), or None."""
    if s.get("version_dir") or not s.get("draft_spec_hash"):
        return None
    from runstate import spec_hash  # not spec.py: that would pull spec's whole import tree in
    if s["draft_spec_hash"] != spec_hash(s.get("spec")):
        return (f"the workflow draft was built from an earlier version of the spec; run /curate-skills:curate-emit "
                f"--run {s.get('run_id')} again")
    return None


def source_dir(s: dict, run) -> Path:
    """The published version once Gate 4 approved; before that the draft (for previews)."""
    if s.get("version_dir"):
        return Path(s["version_dir"])
    if (why := stale_draft(s)):
        raise ExecutionError(why)
    return Path(s.get("draft_dir") or ids.draft_dir(run))


def load_workflow(folder: Path, s: dict | None = None):
    """The validated workflow; for a version published before curate:name, with the names recovered
    from the run's spec (recover_names)."""
    from cwl.validate import validate
    inputs = folder / "inputs.yml"
    wf, errors, _ = validate(folder / "workflow.yml", inputs if inputs.is_file() else None)
    if errors:
        raise ExecutionError("the workflow does not validate: " + " | ".join(errors))
    if s is not None:
        recover_names(wf, folder, s)
    return wf


def _without_names(doc: dict) -> dict:
    """The CWL as it was written before curate:name (8e8c9f9): no curate:name on Operation outputs,
    and an output left with only its type written as that type."""
    import copy
    doc = copy.deepcopy(doc)
    for step in (doc.get("steps") or {}).values():
        outs = ((step.get("run") or {}).get("outputs")) or {}
        for k, port in outs.items():
            if isinstance(port, dict) and "curate:name" in port:
                port.pop("curate:name")
                if set(port) == {"type"}:
                    outs[k] = port["type"]
    return doc


def recover_names(wf, folder: Path, s: dict) -> None:
    """A version published before curate:name has none, so the builders would drop the spec's file
    names and extensions (a function's savefig would write the wrong file). Versions are never
    edited, so the names are recovered in memory: the run's approved spec, built again and with
    curate:name taken out, must have exactly the version's content hash (so the spec is the one the
    version was built from); then each output gets its curate:name from that build. The version
    folder, its content hash and its version hash are unchanged. Otherwise the deploy is refused."""
    missing = [(sid, out) for sid, st in wf.steps.items() for out, port in st.op_outputs.items() if not port.name]
    if not missing:
        return
    import spec_cwl
    from cwl.validate import validate
    try:
        built = spec_cwl.build(_spec(s))
    except (spec_cwl.BuildError, KeyError, ValueError) as e:
        built, why = None, f"the run's spec does not build ({e})"
    if built is not None:
        if spec_cwl.content_hash(_without_names(built["workflow"]), built["functions"]) != \
                ids.draft_content_hash(folder):
            built, why = None, "the run's spec no longer builds exactly this version"
    if built is None:
        raise ExecutionError(
            f"{folder.name} was published before generated file names kept the spec's file names "
            f"(curate:name), and {why}, so its names cannot be recovered; without them a function "
            "would get paths without their extensions. Publish a new version: start from the run that published it with "
            "--start-at emit --from-run <run_id> and approve Gate 4 again.")
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        new, errors, _ = validate(spec_cwl.write(built, Path(tmp) / "w") / "workflow.yml")
    if errors:
        raise ExecutionError("recovering curate:name: the run's spec builds a workflow that does not "
                             "validate: " + " | ".join(errors))
    for sid, out in missing:
        wf.steps[sid].op_outputs[out].name = new.steps[sid].op_outputs[out].name


def _spec(s: dict):
    from curate_models import WorkflowSpec
    return WorkflowSpec.model_validate(s["spec"])


def faasr_built(s: dict, run) -> dict:
    """The FaaSr bundle in memory (cwl_faasr.build). The deploy settings that are not part of the
    workflow come from the spec, as emit's bundle took them."""
    import cwl_faasr
    spec = _spec(s)
    try:
        return cwl_faasr.build(load_workflow(source_dir(s, run), s), spec.name, spec.folder, spec.compute_target,
                               spec.data_store)
    except cwl_faasr.BuildError as e:
        raise ExecutionError("cannot be built for FaaSr: " + " | ".join(e.problems), e.problems) from e


def pegasus_built(s: dict, run, name: str, image: str | None) -> dict:
    import cwl_pegasus
    try:
        return cwl_pegasus.build(load_workflow(source_dir(s, run), s), name, image)
    except cwl_pegasus.BuildError as e:
        raise ExecutionError("cannot run on Pegasus: " + " | ".join(e.problems), e.problems) from e


def new_execution(s: dict, run, target: str) -> dict:
    """A new execution run of the approved version (nothing written yet)."""
    wid = s.get("workflow_id")
    if not wid:
        raise ExecutionError("no approved version: Gate 4 approval publishes it")
    rid = ids.new_run_id()
    d = ids.execution_dir(project_of(s, run), wid, rid)
    while d.exists():
        rid = ids.new_run_id()
        d = ids.execution_dir(project_of(s, run), wid, rid)
    return {"run_id": rid, "reference": ids.reference(wid, rid), "dir": str(d), "target": target,
            "bundle_dir": str(d / "bundle")}


def build_faasr(s: dict, run) -> dict:
    """Write the FaaSr bundle of the approved version and its manifest; returns the execution record."""
    import cwl_faasr
    built = faasr_built(s, run)
    ex = new_execution(s, run, "faasr")
    cwl_faasr.write(built, ex["bundle_dir"])
    m = ids.write_manifest(ex["dir"], s["version_hash"], "faasr")
    return {**ex, "bundle_hash": m["bundle_hash"], "curate_version": m["curate_version"]}


def finish_pegasus(s: dict, ex: dict) -> dict:
    """After pegasus_deploy prepare wrote the folder (inputs copied in): the manifest."""
    m = ids.write_manifest(ex["dir"], s["version_hash"], "pegasus")
    return {**ex, "bundle_hash": m["bundle_hash"], "curate_version": m["curate_version"]}


def check(s: dict) -> str | None:
    """Deploy's check of an execution bundle: built from the approved version, unchanged since."""
    ex = s.get("execution") or {}
    if not ex.get("dir"):
        return "this run has no execution bundle (it is built when Gate 4 or \"Start this run?\" chooses a target)"
    try:
        ids.check_manifest(ex["dir"], s.get("version_hash") or "")
    except (ids.IdError, OSError) as e:
        return f"{e}; approve Gate 4 again to rebuild it"
    return None
