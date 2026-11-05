"""allow_deploy.py: adds deploy's two allow rules; merges, backs up, idempotent. Never the real ~/.claude."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

import allow_deploy as A

ROOT = "/opt/skill-curate/curate-skills"
RULES = [f"Bash(uv run --quiet {ROOT}/scripts/deploy.py *)", f"Bash(uv run --quiet {ROOT}/scripts/monitor.py *)"]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.chdir(project)
    return home / ".claude" / "settings.json"


def run(capsys, *argv):
    code = A.main(["--plugin-root", ROOT, *argv])
    return code, capsys.readouterr().out


def test_creates_the_user_settings_file_with_both_rules(isolated, capsys):
    code, out = run(capsys)
    assert code == 0 and json.loads(isolated.read_text()) == {"permissions": {"allow": RULES}}
    assert "+" in out and RULES[0] in out and "Backup:" not in out  # nothing to back up
    assert "applies to the running session; no restart needed" in out


def test_merges_into_an_existing_file_never_removing_anything_and_backs_it_up(isolated, capsys):
    before = {"theme": "dark", "permissions": {"allow": ["Bash(npm test)"], "deny": ["Read(./.env)"]},
              "enabledPlugins": {"x@y": True}}
    isolated.write_text(json.dumps(before, indent=4))
    isolated.chmod(0o600)
    original = isolated.read_text()
    code, out = run(capsys)
    after = json.loads(isolated.read_text())
    assert after["permissions"]["allow"] == ["Bash(npm test)", *RULES]
    assert after["permissions"]["deny"] == ["Read(./.env)"] and after["theme"] == "dark"
    assert after["enabledPlugins"] == {"x@y": True}
    backups = list(isolated.parent.glob("settings.json.bak-*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert f"Backup: {backups[0]}" in out
    added = [l for l in out.splitlines() if l.startswith("+") and not l.startswith("+++")]
    removed = [l for l in out.splitlines() if l.startswith("-") and not l.startswith("---")]
    assert added == ['+            "Bash(npm test)",', f'+            "{RULES[0]}",', f'+            "{RULES[1]}"']
    assert removed == ['-            "Bash(npm test)"']  # only the comma moved; the file kept its 4-space indent
    assert oct(isolated.stat().st_mode)[-3:] == "600"


def test_running_it_again_changes_nothing(isolated, capsys):
    run(capsys)
    text = isolated.read_text()
    code, out = run(capsys)
    assert code == 0 and "already in" in out and "nothing changed" in out and isolated.read_text() == text
    assert not list(isolated.parent.glob("settings.json.bak-*"))


def test_an_existing_rule_in_another_form_counts(isolated, capsys):
    isolated.write_text(json.dumps({"permissions": {"allow": [f"Bash(uv run --quiet {ROOT}/scripts/deploy.py:*)"]}}))
    run(capsys)
    allow = json.loads(isolated.read_text())["permissions"]["allow"]
    assert allow == [f"Bash(uv run --quiet {ROOT}/scripts/deploy.py:*)", RULES[1]]


@pytest.mark.parametrize("content,why", [("{not json", "is not valid JSON"),
                                         ('{"permissions": {"allow": "Bash(x)"}}', "is not a list"),
                                         ("[1, 2]", "is not a JSON object")])
def test_refuses_a_file_it_cannot_merge_and_leaves_it_alone(isolated, capsys, content, why):
    isolated.write_text(content)
    code, out = run(capsys)
    assert code == 1 and why in out and "nothing" in out and isolated.read_text() == content
    assert not list(isolated.parent.glob("settings.json.bak-*"))


def test_project_writes_the_local_settings_file_instead(isolated, capsys, tmp_path):
    code, out = run(capsys, "--project")
    local = tmp_path / "proj" / ".claude" / "settings.local.json"
    assert code == 0 and json.loads(local.read_text())["permissions"]["allow"] == RULES
    assert not isolated.exists()


def test_project_in_a_git_repository_uses_the_repository_root(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    assert A.project_settings(repo / "sub").resolve() == (repo / ".claude" / "settings.local.json").resolve()


def test_check_reports_what_is_missing_and_reads_only(isolated, capsys, tmp_path):
    code, out = run(capsys, "--check")
    res = json.loads(out)
    assert res["ok"] is False and res["missing"] == RULES and not isolated.exists()
    assert res["command"] == f"! uv run --quiet {ROOT}/scripts/allow_deploy.py"
    assert "no restart needed" in res["takes_effect"]
    run(capsys, "--project")  # rules in the project's local file count too
    code, out = run(capsys, "--check")
    assert json.loads(out) | {"missing": []} == json.loads(out) and json.loads(out)["ok"] is True


def test_default_plugin_root_is_this_plugin_and_the_rule_matches_doctor(isolated):
    assert A.plugin_root() == str(Path(A.__file__).parent.parent)
    import doctor
    assert A.missing_rules([], ROOT) == [doctor.auto_mode_rule(ROOT, s) for s in doctor.AUTO_MODE_SCRIPTS]


def test_runs_on_the_system_python_with_the_standard_library(isolated, tmp_path):
    script = Path(A.__file__)
    out = subprocess.run([sys.executable, "-I", str(script), "--check", "--plugin-root", ROOT],
                         capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0 and json.loads(out.stdout)["missing"] == RULES


def test_the_deploy_stage_asks_once_and_never_runs_it():
    plugin = Path(__file__).resolve().parents[1]
    text = " ".join((plugin / "references" / "stages" / "deploy.md").read_text().split())
    assert "`Deploy needs permission to run its GitHub scripts in later turns. Add it to your settings?`" in text
    assert "`! uv run --quiet <plugin>/scripts/allow_deploy.py`" in text and "**Never run it yourself**" in text
    assert "in auto mode, deploy will stop after the first step" in text
    assert "manual mode (Shift+Tab) asks before each command instead" in text
    assert "Never edit a settings file yourself." in text
    assert text.index("permission for later turns") < text.index("## 0. Invoke only")
    for f in ("README.md", "scripts/doctor.py", "skills/curate-deploy/SKILL.md", "references/stages/deploy.md"):
        assert "default mode" not in (plugin / f).read_text(), f
