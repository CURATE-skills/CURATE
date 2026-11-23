"""The paths a function gets at deploy have the same file extensions as in its local stub test.

A function may rely on its paths' extensions (the weather plot calls fig.savefig(out_path), and
matplotlib picks the format from the extension), so the builders keep the spec's file name at the
end of every name they generate (curate:name). For every step and slot of the e2 and weather specs:
the names the stub test uses (plain_contract's layout, as stubtest.py builds it), the paths the
Pegasus build gives each job, and the names the FaaSr build passes in each action's Arguments all
end in the same extension.
"""
import json
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN / "curate_ir"))
sys.path.insert(0, str(PLUGIN / "tests"))
import cwl_faasr as CF  # noqa: E402
import cwl_pegasus as CP  # noqa: E402
import emit  # noqa: E402
import plain_contract as PC  # noqa: E402
from curate_models import WorkflowSpec  # noqa: E402
from test_cwl_builders import cwl_of  # noqa: E402
from test_cwl_from_ir import FIXTURES  # noqa: E402

SPECS = {"e2": FIXTURES / "e2-stages" / "spec-with-code.json", "weather": FIXTURES / "weather-spec.json"}


def ext(name: str) -> str:
    return Path(str(name).replace("{rank}", "1")).suffix


def files(value) -> list[str]:
    if isinstance(value, dict):
        return list(value.get("folder") or [])
    return value if isinstance(value, list) else [value]


def stub_extensions(spec) -> dict:
    """{(step, slot): extension} as the stub test names the files (spec names, {rank} filled)."""
    layout, _ = PC.io_layout(spec)
    out = {}
    for step, lay in layout.items():
        for s in lay["inputs"] + lay["outputs"]:
            names = s.get("matches") if s["kind"] == "folder" else [s["file"]]
            exts = {ext(PC.resolve(n, 1)) for n in names}
            assert len(exts) == 1, (step, s)
            out[(step, s["slot"])] = exts.pop()
    return out


@pytest.mark.parametrize("which", sorted(SPECS))
def test_deploy_paths_keep_the_stub_tests_extensions(tmp_path, which):
    data = json.loads(SPECS[which].read_text())
    spec = WorkflowSpec.model_validate(data)
    want = stub_extensions(spec)
    assert any(e for e in want.values()), "the fixture has file names with extensions"
    wf = cwl_of(data, tmp_path)

    # Pegasus: every job's paths
    seen = set()
    for job in CP.build(wf, "t")["ir"]["nodes"]:
        step = job["_origin"]
        for slot, value in list(job["inputs"].items()) + list(job["outputs"].items()):
            if (step, slot) not in want:
                continue  # params and markers
            for f in files(value):
                assert ext(f) == want[(step, slot)], f"Pegasus {step}.{slot}: {f} (stub test: {want[(step, slot)]})"
            seen.add((step, slot))
    assert seen == set(want)

    # FaaSr: every action's Arguments (the wrapper fills {rank})
    built = CF.build(wf, spec.name, spec.folder, spec.compute_target, spec.data_store)
    seen = set()
    for action in built["workflow"]["ActionList"].values():
        step = action["FunctionName"]
        for slot, name in action["Arguments"].items():
            if slot == "folder":
                continue
            assert ext(name) == want[(step, slot)], f"FaaSr {step}.{slot}: {name} (stub test: {want[(step, slot)]})"
            seen.add((step, slot))
    assert seen == set(want)


def test_the_weather_plot_keeps_png(tmp_path):
    """The case found on maxwell: the plot's output must end in .png at deploy too."""
    data = json.loads(SPECS["weather"].read_text())
    wf = cwl_of(data, tmp_path)
    plot = next(j for j in CP.build(wf, "t")["ir"]["nodes"] if j["_origin"] == "plot_weather_comparison")
    assert plot["outputs"]["output1"] == "plot_weather_comparison__output1__corvallis_winter_comparison.png"
    spec = WorkflowSpec.model_validate(data)
    built = CF.build(wf, spec.name, spec.folder, spec.compute_target, spec.data_store)
    args = next(a["Arguments"] for a in built["workflow"]["ActionList"].values()
                if a["FunctionName"] == "plot_weather_comparison")
    assert args["output1"] == "plot_weather_comparison__output1__corvallis_winter_comparison.png"
    assert args["input1"] == "process_weather_variable__output1__{rank}__winter_comparison_{rank}.csv"
    # the folder input still lists only its own files: the prefix ends before the spec name
    assert built["names"]["process_weather_variable/output1"].split("{rank}")[0] == \
        "process_weather_variable__output1__"
