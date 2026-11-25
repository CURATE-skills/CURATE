"""Every generated name's step and output parts are ids in workflow.yml.

Both builders name a step output `<step>__<output>[__<values>|__{rank}]__<curate:name>`, with
<step> a key of workflow.yml's `steps` and <output> one of that step's `out` ids (not a spec slot
label or a function name, unless the workflow's ids happen to be those). Checked against the raw
YAML, not the loader, for the e2 and weather specs and the pre-curate:name weather version.
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "scripts"))
sys.path.insert(0, str(PLUGIN / "curate_ir"))
sys.path.insert(0, str(PLUGIN / "tests"))
import cwl_faasr as CF  # noqa: E402
import cwl_pegasus as CP  # noqa: E402
import execution as X  # noqa: E402
import spec_cwl  # noqa: E402
from curate_models import WorkflowSpec  # noqa: E402

FIXTURES = PLUGIN / "tests" / "fixtures"
SPECS = {"e2": FIXTURES / "e2-stages" / "spec-with-code.json", "weather": FIXTURES / "weather-spec.json"}


def folder_for(which, tmp_path) -> tuple[Path, dict]:
    data = json.loads((SPECS["weather"] if which == "weather-v1" else SPECS[which]).read_text())
    if which == "weather-v1":
        return FIXTURES / "pre-curate-name-weather", data
    out = tmp_path / which
    spec_cwl.write(spec_cwl.build(WorkflowSpec.model_validate(data)), out)
    return out, data


def ids_in(folder: Path) -> dict[str, set[str]]:
    doc = yaml.safe_load((folder / "workflow.yml").read_text())
    return {sid: {o if isinstance(o, str) else o["id"] for o in step["out"]} for sid, step in doc["steps"].items()}


def check(name: str, want_step: str, want_out: str, ids: dict, where: str):
    step, out = name.split("__")[:2]
    assert (step, out) == (want_step, want_out), f"{where}: {name}"
    assert step in ids and out in ids[step], f"{where}: {name} is not <step id>__<out id> of workflow.yml"


@pytest.mark.parametrize("which", ["e2", "weather", "weather-v1"])
def test_generated_names_use_the_workflow_ids(tmp_path, which):
    folder, data = folder_for(which, tmp_path)
    ids = ids_in(folder)
    wf = X.load_workflow(folder, {"spec": data})
    spec = WorkflowSpec.model_validate(data)

    faasr = CF.build(wf, spec.name, spec.folder, spec.compute_target, spec.data_store)
    step_outputs = {src: n for src, n in faasr["names"].items() if src.split("/")[0] in ids}
    assert step_outputs and {tuple(s.split("/")) for s in step_outputs} == {(s, o) for s, os_ in ids.items() for o in os_}
    for src, name in step_outputs.items():
        check(name, *src.split("/"), ids, f"FaaSr {src}")

    seen = set()
    for job in CP.build(wf, "t")["ir"]["nodes"]:
        sid = job["_origin"]
        for slot, value in job["outputs"].items():
            if slot not in ids[sid]:
                continue  # markers
            for f in value if isinstance(value, list) else [value]:
                check(f, sid, slot, ids, f"Pegasus {sid}.{slot}")
            seen.add((sid, slot))
    assert seen == {(s, o) for s, os_ in ids.items() for o in os_}
