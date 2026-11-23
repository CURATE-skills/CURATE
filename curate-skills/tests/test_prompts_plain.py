"""The generation prompts describe only the plain contract: nothing tells an agent to use the
FaaSr API. FaaSr is still named as a target (and in the deploy and monitor texts)."""
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
GENERATION = ["agents/curate-mga.md", "skills/function-reference/SKILL.md", "references/prompts/mga-spawn.md",
              "references/prompts/wca-system.md", "references/prompts/wca-resolve.md",
              "references/prompts/mca-candidates.md", "references/prompts/mca-user-model.md",
              "references/prompts/classify.md"]
API = ("faasr_get_file", "faasr_put_file", "faasr_rank", "faasr_log", "faasr_secret", "faasr_get_folder_list",
       "remote_file", "faasr-reference")


@pytest.mark.parametrize("rel", GENERATION)
def test_no_faasr_api_in_generation_prompts(rel):
    text = (PLUGIN / rel).read_text()
    # function-reference lists the FaaSr names once, as what a function must never contain
    if rel.endswith("function-reference/SKILL.md"):
        text = text.split("## What the file must never contain", 1)[0] + \
            text.split("## What the file must never contain", 1)[1].split("\n## ", 1)[1]
    assert not [a for a in API if a in text], [a for a in API if a in text]


def test_the_mga_preloads_the_plain_reference():
    mga = (PLUGIN / "agents" / "curate-mga.md").read_text()
    assert "skills:\n  - function-reference\n" in mga
    assert not (PLUGIN / "skills" / "faasr-reference").exists()
    ref = (PLUGIN / "skills" / "function-reference" / "SKILL.md").read_text()
    assert "def main(inputs: dict, outputs: dict, params: dict)" in ref and 'os.environ["NAME"]' in ref


def test_resolve_prompt_explains_folder_and_read_all():
    resolve = (PLUGIN / "references" / "prompts" / "wca-resolve.md").read_text()
    assert '"kind": "folder"' in resolve and '"read": "all"' in resolve


def test_empty_fan_in_is_an_error_everywhere_the_agents_read():
    ref = (PLUGIN / "skills" / "function-reference" / "SKILL.md").read_text()
    assert "**A fan-in with no files is an error.**" in ref and "don't filter it by file name" in ref
    resolve = (PLUGIN / "references" / "prompts" / "wca-resolve.md").read_text()
    assert 'NEVER design\n    "no files at all" as a valid case' in resolve
    assert "no other file in the\n    workflow may start with that prefix" in resolve
