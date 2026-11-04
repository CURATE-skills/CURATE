"""conftest clears deployment settings and credentials for every test."""
import os

from conftest import ENV_VARS, isolated_env_names


def test_names_matched():
    names = ["FAASR_S3_BUCKET", "FAASR_GH_USERNAME", "faasr_action_repo", "GH_PAT", "S3_AccessKey",
             "S3_SECRETKEY", "CURATE_DEPLOY_NAME_SUFFIX", "CURATE_MONITOR_MINUTES", "CURATE_CATALOG_DIR",
             "AWS_AccessKey", "AWS_SECRET_ACCESS_KEY", "AWS_ARN", "GITHUB_REPOSITORY",
             "PATH", "HOME", "GH_PATH", "MY_FAASR_X", "CURATE_GUARD_DEBUG", "GITHUB_REPOSITORY_X"]
    assert isolated_env_names(names) == names[:13]


def test_none_left_in_a_test():
    assert isolated_env_names(list(os.environ)) == []
    assert not any(v in os.environ for v in ENV_VARS)


def test_no_test_can_reach_a_real_github_token(monkeypatch):
    import shutil
    import subprocess

    import deploy
    import monitor

    # Even with a gh binary "on PATH", get_token must not run it: the conftest stub answers first.
    calls = []
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
        AssertionError("subprocess.run reached: " + repr(a))))
    assert deploy.get_token() == (None, "none")
    assert monitor.D.get_token() == (None, "none")
    assert calls == []
    # GH_PAT is cleared, so nothing else can supply one.
    assert "GH_PAT" not in os.environ and deploy.gh_cli_token() is None


def test_gh_fallback_still_works_when_a_test_opts_in(monkeypatch):
    import deploy
    monkeypatch.setattr(deploy, "gh_cli_token", lambda: "fake-token-from-gh")
    assert deploy.get_token() == ("fake-token-from-gh", "gh auth token")
    monkeypatch.setenv("GH_PAT", "fake-pat")
    assert deploy.get_token() == ("fake-pat", "GH_PAT")
