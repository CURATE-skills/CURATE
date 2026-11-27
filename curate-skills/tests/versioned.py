"""A hand-made FaaSr bundle as a versioned run has it (phase 8: deploy only takes bundles built from an
approved version): <project>/workflows/<wid>/ holds a minimal version folder, and the bundle
sits in a run of it (runs/<rid>/bundle/) with its manifest (ids.write_manifest)."""
import json
from pathlib import Path

import ids

RID = "20261124-120000-abcd"


def versioned_bundle(project: Path, files: dict[str, str], wid: str = "weather-v1", rid: str = RID,
                     target: str = "faasr") -> tuple[dict, Path]:
    """(the state fields of a run whose Gate 4 published `wid` and built this bundle, the bundle folder)."""
    ids.claim_workflows_root(project)  # as publishing does: workflows/ with its marker
    vdir = ids.version_dir(project, wid)
    vdir.mkdir(parents=True, exist_ok=True)
    if not (vdir / "workflow.yml").exists():
        (vdir / "workflow.yml").write_text("cwlVersion: v1.2\nclass: Workflow\n")
    vhash = ids.version_hash(vdir)
    ex = ids.execution_dir(project, wid, rid)
    bundle = ex / "bundle"
    for rel, text in files.items():
        (bundle / rel).parent.mkdir(parents=True, exist_ok=True)
        (bundle / rel).write_text(text)
    m = ids.write_manifest(ex, vhash, target)
    return {"workflow_id": wid, "version_dir": str(vdir), "version_hash": vhash,
            "execution": {"run_id": rid, "reference": ids.reference(wid, rid), "dir": str(ex), "target": target,
                          "bundle_dir": str(bundle), "bundle_hash": m["bundle_hash"]}}, bundle


def bundle_of(run: Path) -> Path:
    return Path(json.loads((Path(run) / "state.json").read_text())["execution"]["bundle_dir"])


def rebuilt(run: Path) -> str:
    """After a test edited the bundle on purpose: its manifest as if it had been built that way."""
    path = Path(run) / "state.json"
    s = json.loads(path.read_text())
    ex = s["execution"]
    m = ids.write_manifest(ex["dir"], s["version_hash"], ex["target"])
    ex["bundle_hash"] = m["bundle_hash"]
    path.write_text(json.dumps(s))
    return m["bundle_hash"]
