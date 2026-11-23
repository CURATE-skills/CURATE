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
    from spec import spec_hash
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


def load_workflow(folder: Path):
    from cwl.validate import validate
    inputs = folder / "inputs.yml"
    wf, errors, _ = validate(folder / "workflow.yml", inputs if inputs.is_file() else None)
    if errors:
        raise ExecutionError("the workflow does not validate: " + " | ".join(errors))
    return wf


def _spec(s: dict):
    from curate_models import WorkflowSpec
    return WorkflowSpec.model_validate(s["spec"])


def faasr_built(s: dict, run) -> dict:
    """The FaaSr bundle in memory (cwl_faasr.build). The deploy settings that are not part of the
    workflow come from the spec, as emit's bundle took them."""
    import cwl_faasr
    spec = _spec(s)
    try:
        return cwl_faasr.build(load_workflow(source_dir(s, run)), spec.name, spec.folder, spec.compute_target,
                               spec.data_store)
    except cwl_faasr.BuildError as e:
        raise ExecutionError("cannot be built for FaaSr: " + " | ".join(e.problems), e.problems) from e


def pegasus_built(s: dict, run, name: str, image: str | None) -> dict:
    import cwl_pegasus
    try:
        return cwl_pegasus.build(load_workflow(source_dir(s, run)), name, image)
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
