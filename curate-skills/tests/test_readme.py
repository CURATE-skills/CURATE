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
    named = set(re.findall(r"/curate-skills:([a-z][a-z-]*)", README))
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
def test_aliases_as_written(tmp_path):
    home, bindir = tmp_path / "home", tmp_path / "bin"
    (home / ".config" / "curate").mkdir(parents=True)
    bindir.mkdir()
    env_file = home / ".config" / "curate" / ".env"
    text = (PLUGIN / ".env.template").read_text()
    env_file.write_text(text.replace("GH_PAT=\n", "GH_PAT=dummy-value-x\n")
                        .replace("FAASR_S3_BUCKET=\n", "FAASR_S3_BUCKET=my-faasr-data\n"))
    (bindir / "claude").write_text('#!/bin/sh\necho "in: $* bg=$CLAUDE_CODE_DISABLE_BACKGROUND_TASKS '
                                   'pat=${GH_PAT:+set} bucket=$FAASR_S3_BUCKET"\n')
    (bindir / "claude").chmod(0o755)
    rc = home / ".bashrc"
    rc.write_text("\n".join(_alias(n).replace(PLACEHOLDER, str(PLUGIN)) for n in ("curate", "curate-deploy")) + "\n")
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(home)}

    def run(cmd):
        return subprocess.run(["bash", "--rcfile", str(rc), "-i", "-c",
                               cmd + '; echo "after: pat=${GH_PAT:-unset} bucket=${FAASR_S3_BUCKET:-unset}"'],
                              capture_output=True, text=True, timeout=30, env=env).stdout

    out = run("curate")
    assert f"in: --plugin-dir {PLUGIN} bg=1 pat= bucket=" in out and "after: pat=unset bucket=unset" in out
    out = run("curate-deploy")
    assert f"in: --plugin-dir {PLUGIN} bg=1 pat=set bucket=my-faasr-data" in out
    assert "after: pat=unset bucket=unset" in out  # the subshell leaves the shell clean


def test_template_copy_command_matches_the_template():
    assert ("mkdir -p ~/.config/curate && cp .env.template ~/.config/curate/.env && "
            "chmod 600 ~/.config/curate/.env") in README
    assert (PLUGIN / ".env.template").exists()
    assert "Never paste a secret into a Claude Code session" in README
    assert "/curate-skills:curate-doctor --deploy --online" in README


def test_permission_rules_live_in_docs_not_the_install_flow():
    assert "Permissions (recommended" not in README and '"permissions"' not in README
    assert "Edit(./.curate/**)" not in README and "docs/permissions.md" not in README
    assert ("| Claude Code asks for permission during a run? | That's expected. Choose **Yes** (or **Yes, and "
            "don't ask again** for this project). Choosing **No** stops that step. |") in README
    doc = (PLUGIN / "docs" / "permissions.md").read_text()
    for rule in ('"Bash(uv run --quiet /absolute/path/to/skill-curate/curate-skills/scripts/*)"',
                 '"Read(//absolute/path/to/skill-curate/curate-skills/**)"', '"Edit(./.curate/**)"'):
        assert rule in doc


def test_root_readme_points_to_the_plugin_readme():
    root = (PLUGIN.parent / "README.md").read_text()
    assert "git clone https://github.com/laurakuo1006/skill-curate.git" in root
    assert "](curate-skills/README.md)" in root and len(root.splitlines()) < 15
