import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# Cleared for every test, so a developer's shell (settings, a loaded deploy .env) can't change
# results. Names are matched without regard to case, as the scripts do (S3_ACCESSKEY).
ENV_VARS = ("CURATE_CATALOG_DIR", "CURATE_MGA_RETRIES", "CURATE_STUBTEST_MODE",
            "CURATE_DEPLOY_NAME_SUFFIX", "CURATE_MONITOR_MINUTES", "GH_PAT", "GITHUB_REPOSITORY")
ENV_PREFIXES = ("FAASR_", "S3_", "AWS_")


def isolated_env_names(names) -> list[str]:
    exact = {v.upper() for v in ENV_VARS}
    return [n for n in names if n.upper() in exact or n.upper().startswith(ENV_PREFIXES)]


@pytest.fixture(autouse=True)
def _isolated_curate_env(monkeypatch):
    import os
    for v in isolated_env_names(list(os.environ)):
        monkeypatch.delenv(v, raising=False)


@pytest.fixture(autouse=True)
def _no_real_github_token(monkeypatch):
    """With GH_PAT cleared, deploy.get_token would fall back to `gh auth token` and could pick up
    the developer's real token. Every test gets a stub that returns None; a test that needs the
    fallback replaces it again with monkeypatch."""
    import deploy
    monkeypatch.setattr(deploy, "gh_cli_token", lambda: None)


@pytest.fixture
def make_run(tmp_path, capsys):
    """Create a run with spec.py init; optionally set state fields directly."""
    import spec as spec_mod

    def _make(request="count words in a text file", spec=None, project=None, extra_args=(), **state):
        project = Path(project or tmp_path / "project")
        project.mkdir(parents=True, exist_ok=True)
        req = project / "request.txt"
        req.write_text(request)
        args = ["init", "--request-file", str(req), "--project-dir", str(project),
                "--catalog-dir", str(tmp_path / "catalog"), *extra_args]
        assert spec_mod.main(args) == 0, capsys.readouterr().out
        run = Path(json.loads(capsys.readouterr().out)["run"])
        if spec is not None or state:
            s = json.loads((run / "state.json").read_text())
            if spec is not None:
                s["spec"] = spec if isinstance(spec, dict) else json.loads(spec.model_dump_json())
            s.update(state)
            (run / "state.json").write_text(json.dumps(s, indent=2))
        return run

    return _make


def call(module, argv, capsys):
    """Run a script's main() and return (exit_code, parsed JSON stdout)."""
    code = module.main(argv)
    return code, json.loads(capsys.readouterr().out)


def ok(module, argv, capsys):
    """Like call(), but the command must succeed (exit 0)."""
    code, out = call(module, argv, capsys)
    assert code == 0, out
    return out
