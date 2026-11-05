"""The README's commands match the plugin: skills exist, the two aliases work as described."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
README = (PLUGIN / "README.md").read_text()
PLACEHOLDER = "/absolute/path/to/skill-curate/curate-skills"


def test_every_skill_named_exists_and_every_user_skill_is_named():
    named = set(re.findall(r"/curate-skills:([a-z][a-z-]*[a-z])(?![a-z<-])", README))  # not curate-<stage>
    named |= set(re.findall(r"`(curate-[a-z]+|wda-finalize)`", README)) & {
        p.name for p in (PLUGIN / "skills").iterdir()}
    have = {p.name for p in (PLUGIN / "skills").iterdir() if (p / "SKILL.md").exists()}
    assert named <= have, named - have
    assert have - {"faasr-reference"} <= named, have - named  # faasr-reference is the subagent's


def test_no_removed_setup_skill_or_old_claims():
    for gone in ("curate-setup", "It does not deploy", "v2-files", "CURATE_ENV_FILE"):
        assert gone not in README


def _alias(name):
    (line,) = [l for l in README.splitlines() if l.startswith(f"alias {name}=")]
    return line


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
def test_one_alias_as_written(tmp_path):
    # the curate-deploy *skill* is mentioned; there is no deploy alias or deploy session any more
    for gone in ("alias curate-deploy", "curate-deploy` alias", "curate-deploy` session", "Start `curate-deploy`"):
        assert gone not in README
    home, bindir = tmp_path / "home", tmp_path / "bin"
    home.mkdir()
    bindir.mkdir()
    (bindir / "claude").write_text('#!/bin/sh\necho "in: $* bg=$CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"\n')
    (bindir / "claude").chmod(0o755)
    rc = home / ".bashrc"
    rc.write_text(_alias("curate").replace(PLACEHOLDER, str(PLUGIN)) + "\n")
    out = subprocess.run(["bash", "--rcfile", str(rc), "-i", "-c", "curate"], capture_output=True, text=True,
                         timeout=30, env={"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home)}).stdout
    assert f"in: --plugin-dir {PLUGIN} bg=1" in out


def test_template_copy_command_matches_the_template():
    assert ("mkdir -p ~/.config/curate && cp .env.template ~/.config/curate/.env && "
            "chmod 600 ~/.config/curate/.env") in README
    assert (PLUGIN / ".env.template").exists()
    assert "Never paste a secret into Claude Code." in README
    assert "/curate-skills:curate-doctor --deploy --online" in README
    deploy = README[README.index("## Deploy"):README.index("## Settings")]
    assert "Deploy, monitor and doctor read this file themselves; environment variables override it." \
        in " ".join(deploy.split())


def test_permission_rules_live_in_docs_not_the_install_flow():
    assert "Permissions (recommended" not in README and '"permissions"' not in README
    assert "Edit(./.curate/**)" not in README and "docs/permissions.md" not in README
    doc = (PLUGIN / "docs" / "permissions.md").read_text()
    for rule in ('"Bash(uv run --quiet /absolute/path/to/skill-curate/curate-skills/scripts/*)"',
                 '"Read(//absolute/path/to/skill-curate/curate-skills/**)"', '"Edit(./.curate/**)"'):
        assert rule in doc


def test_root_readme_points_to_the_plugin_readme():
    root = (PLUGIN.parent / "README.md").read_text()
    assert "git clone https://github.com/laurakuo1006/skill-curate.git" in root
    assert "](curate-skills/README.md)" in root and len(root.splitlines()) < 15


def test_quick_start_is_short_and_the_readme_has_no_cost_or_time_figures():
    quick = README[README.index("## Quick start"):README.index("## Gates")]
    flat = " ".join(quick.split())
    assert "A **sketch** is a plain-language description of the workflow you want." in flat
    heads = re.findall(r"^### (.+)$", quick, re.M)
    assert heads == ["Run your own workflow", "Not sure what to try? Use the weather example",
                     "Reproduce E2 from the CURATE paper"]
    blocks = re.findall(r"```text\n(.*?)\n```", quick, re.S)
    assert len(blocks) == 2 and quick.count("```") == 4  # your sketch, the weather sketch; nothing else
    assert blocks[0] == "/curate-skills:curate <your sketch>"
    # the weather sketch from the real runs, verbatim (20 Oct and 1 Nov runs' user_request)
    assert blocks[1].startswith("/curate-skills:curate Build me a FaaSr workflow that compares this winter's "
                                "weather in Corvallis, Oregon with the past 10 years. Download the NOAA "
                                "GHCN-Daily CSV for station USC00351862.")
    assert blocks[1].endswith("Temperatures in the data are in tenths of a degree Celsius.")
    assert ("This command runs the entire pipeline (sketch → outline → reuse → design → code + tests → "
            "bundle → [deploy → monitor, if you choose it at Gate 4] → catalog), stopping at each gate for your "
            "review (see [Gates](#gates))") in flat
    assert "faasr_output/<workflow>-<run_id>/` in the folder you started in" in flat
    link = re.search(r"\]\((\.\./experiments/prompts/e2-mapreduce\.md)\)", quick)
    assert link and (PLUGIN / link.group(1)).resolve().exists()
    assert "The sketch is reconstructed, not the paper's verbatim prompt." in flat
    assert "approve" not in quick and "| Turn |" not in quick
    assert not re.search(r"\$\d|~\s?\d+\s?(s|min)\b|\d+\s?minutes? and \$|minute or two", README)


def test_sketch_not_prompt():
    # "prompt" only in file/folder names and in "the paper's verbatim prompt"
    rest = README.replace("experiments/prompts/", "").replace("the paper's verbatim prompt", "")
    assert not re.search(r"\bprompts?\b", rest, re.I)


def test_after_gate_4_matches_the_stage_flow():
    """README's after-Gate-4 text matches the code: invoke → monitor, monitor success → finalize."""
    gates = " ".join(README[README.index("## Gates"):README.index("## Start at a stage")].split())
    assert ("**approve & deploy** → deploy → after invoke, monitor watches the run → on success, finalize "
            "(Gate 5).") in gates
    assert "**approve, no deploy** → straight to finalize (Gate 5)." in gates
    assert "stops after its stage and prints the next command" in gates
    deploy_src = (PLUGIN / "scripts" / "deploy.py").read_text()
    monitor_src = (PLUGIN / "scripts" / "monitor.py").read_text()
    assert 'st.update(stage="monitor", begun=None)' in deploy_src  # after invoke
    assert 'st.update(stage="finalize", begun=None)' in monitor_src  # after success
    skill = (PLUGIN / "skills" / "curate" / "SKILL.md").read_text()
    assert "do not stop — go back to step 1" in skill  # /curate chains the stages


def test_auto_mode_advice_matches_doctor():
    import doctor
    root = "/absolute/path/to/skill-curate/curate-skills"
    deploy = README.split("## Deploy", 1)[1].split("\n## ", 1)[0]
    for script in ("deploy.py", "monitor.py"):
        assert doctor.auto_mode_rule(root, script) in deploy
    assert "blocks deploy after the first step" in deploy and "manual mode" in deploy and "default mode" not in deploy
    assert "`! uv run --quiet /absolute/path/to/skill-curate/curate-skills/scripts/allow_deploy.py`" in deploy
