"""docs/CWL_IR.md: its examples are the checked-in example files, use the one namespace constant,
and pass `cwltool --validate` (cwltool is a test dependency: tests/requirements.txt)."""
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PLUGIN = Path(__file__).resolve().parents[1]
REPO = PLUGIN.parent
DOC = REPO / "docs" / "CWL_IR.md"
sys.path.insert(0, str(PLUGIN / "curate_ir"))
from cwl import EXTENSIONS, NS, PREFIX  # noqa: E402

EXAMPLE = re.compile(r"<!-- example: (\S+) -->\n```yaml\n(.*?)```", re.S)


def examples():
    return [(REPO / path, body) for path, body in EXAMPLE.findall(DOC.read_text())]


def test_doc_embeds_every_example_file_verbatim():
    found = examples()
    assert {p.parent.name for p, _ in found} == {"weather", "features"}
    for path, body in found:
        assert path.read_text() == body, f"{path} differs from its copy in docs/CWL_IR.md"


@pytest.mark.parametrize("name", ["weather", "features"])
def test_example_shape(name):
    wf = yaml.safe_load((PLUGIN / "curate_ir/cwl/examples" / name / "workflow.yml").read_text())
    assert wf["cwlVersion"] == "v1.2" and wf["class"] == "Workflow"
    assert wf["$namespaces"][PREFIX] == NS
    ids = list(wf["inputs"]) + list(wf["outputs"]) + list(wf["steps"])
    assert len(ids) == len(set(ids)), "workflow inputs, outputs and steps share one id namespace"
    for sid, step in wf["steps"].items():
        assert step["run"]["class"] == "Operation"
        fn = step["hints"][f"{PREFIX}:function"]
        assert fn["code"] == f"functions/{sid}.py" and fn["entrypoint"] == "main"
        for key, value in step["hints"].items():
            assert isinstance(value, dict), f"{sid}: hint {key} must be an object"
            if key.startswith(PREFIX + ":"):
                assert EXTENSIONS[key.split(":", 1)[1]] == "step hints"


def test_doc_lists_exactly_the_extensions():
    text = DOC.read_text()
    table = re.findall(r"^\| `curate:(\w+)` \|", text, re.M)
    assert table == list(EXTENSIONS)
    assert NS in text


@pytest.mark.parametrize("name", ["weather", "features"])
def test_examples_pass_cwltool_validate(name):
    wf = PLUGIN / "curate_ir/cwl/examples" / name / "workflow.yml"
    r = subprocess.run([sys.executable, "-m", "cwltool", "--validate", str(wf)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and "is valid CWL" in r.stdout + r.stderr, r.stderr[-2000:]
