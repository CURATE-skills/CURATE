"""curate-skills/.env.template: every deploy variable, empty, documented; safe to commit."""
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import doctor
import emit
import leakcheck

PLUGIN = Path(__file__).resolve().parents[1]
TEMPLATE = PLUGIN / ".env.template"
REPO = PLUGIN.parent
SECRETS = ("GH_PAT", "S3_AccessKey", "S3_SecretKey")


def assignments():
    """[(key, value, the comment block right above it)]"""
    out, block = [], []
    for line in TEMPLATE.read_text().splitlines():
        if line.startswith("#"):
            block.append(line)
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            out.append((m.group(1), m.group(2), "\n".join(block)))
        block = []
    return out


def test_every_deploy_variable_present_and_empty():
    got = {k: v for k, v, _ in assignments()}
    needed = set(doctor.DEPLOY_VARS) | set(doctor.S3_KEY_VARS) | set(emit.PLACEHOLDER_VARS) | set(SECRETS) \
        | {"FAASR_ACTION_BRANCH", "CURATE_DEPLOY_NAME_SUFFIX", "CURATE_MONITOR_MINUTES"}
    assert needed <= set(got)
    assert all(v == "" for v in got.values())  # no values, not even examples


def test_each_variable_has_a_comment_with_an_example():
    for key, _, block in assignments():
        assert block, key
        assert "Example format:" in block, key


def test_secrets_marked_and_classic_token_explained():
    blocks = {k: b for k, _, b in assignments()}
    for key in SECRETS:
        assert "SECRET" in blocks[key], key
    assert not any("SECRET" in b for k, b in blocks.items() if k not in SECRETS)
    pat = " ".join(blocks["GH_PAT"].replace("#", " ").split())
    assert "CLASSIC GitHub personal access token with scopes repo and workflow" in pat
    assert ("GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)") in pat
    head = " ".join(TEMPLATE.read_text().split("# ── GitHub")[0].replace("#", " ").split())
    assert "must ALSO be set as GitHub Actions secrets on your action repo" in head
    assert "Never paste them into a Claude Code session" in head


def test_leakcheck_does_not_flag_the_template():
    assert leakcheck.find_leaks(TEMPLATE.read_text()) == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on PATH")
def test_gitignore_ignores_env_but_not_the_template():
    def ignored(path):
        return subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", path]).returncode == 0
    if subprocess.run(["git", "-C", str(REPO), "rev-parse"], capture_output=True).returncode != 0:
        pytest.skip("not in the git checkout")
    assert not ignored("curate-skills/.env.template")
    assert ignored("curate-skills/.env") and ignored(".env") and ignored("x/prod.env")


def test_template_sources_cleanly_in_sh(tmp_path):
    proc = subprocess.run(["sh", "-c", f'set -e; set -a; . "{TEMPLATE}"; set +a; echo "GH_PAT=[$GH_PAT]"'],
                          capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert proc.returncode == 0 and proc.stderr == "" and proc.stdout.strip() == "GH_PAT=[]"
