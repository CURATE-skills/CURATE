"""Plugin settings reach the scripts through `--config-<key> '<value>'`, filled in by Claude Code from
`${user_config.<key>}` in skill text. A set value must reach the script; a value left unfilled
(the placeholder as written, or a bare name the model made of it) must fall back to the default and
never be used, or shown, as a value."""
import json
from pathlib import Path

import pytest

import catalog
import doctor
import monitor as M
import pegasus_monitor as PM
import settings
import spec as S
from conftest import call

KEYS = ["catalog_dir", "mga_retries", "monitor_minutes"]  # stubtest_mode: not a user setting any more


def leftovers(key):
    return [f"${{user_config.{key}}}", f"user_config.{key}", key, f"<{key}>", settings.ENV[key]]


@pytest.mark.parametrize("key", KEYS)
def test_every_leftover_form_is_not_a_value(key):
    for raw in leftovers(key) + ["", "  ", None]:
        assert settings.plugin_value(raw, key) is None, raw


def test_a_real_value_containing_the_words_is_kept(tmp_path):
    # Only the exact leftover forms are refused: a real path with "user_config" or the key in it works.
    for real in (tmp_path / "user_config" / "catalog", tmp_path / "user_config.catalog_dir.d",
                 tmp_path / "catalog_dir"):
        assert settings.plugin_value(str(real), "catalog_dir") == str(real)
        assert catalog.resolve_catalog_dir(None, None, str(real)) == (real.resolve(), "plugin setting")
        assert doctor.resolve_catalog(None, {}, str(real)) == (real.resolve(), "plugin setting")


# ── catalog_dir ──

@pytest.mark.parametrize("raw", leftovers("catalog_dir"))
def test_catalog_dir_unfilled_falls_back(monkeypatch, tmp_path, raw):
    monkeypatch.chdir(tmp_path)
    assert catalog.resolve_catalog_dir(None, None, raw) == (catalog.DEFAULT_CATALOG_DIR, settings.NOT_FILLED)
    path, source = doctor.resolve_catalog(None, {}, raw)
    assert source == settings.NOT_FILLED and "user_config" not in str(path) and path.name == "catalog"
    assert not any(tmp_path.iterdir())  # no folder named after the setting


def test_catalog_dir_set_value_reaches_catalog_py(tmp_path, capsys):
    cat = tmp_path / "my catalog"
    code, out = call(catalog, ["where", "--config-catalog-dir", str(cat)], capsys)
    assert code == 0 and out["catalog_dir"] == str(cat.resolve()) and out["catalog_dir_source"] == "plugin setting"
    code, out = call(catalog, ["where", "--config-catalog-dir", "user_config.catalog_dir"], capsys)
    assert out["catalog_dir"] == str(catalog.DEFAULT_CATALOG_DIR) and out["catalog_dir_source"] == settings.NOT_FILLED


# ── mga_retries / stubtest_mode, through spec.py init ──

def _init(tmp_path, capsys, *config):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "request.txt").write_text("count words")
    code, out = call(S, ["init", "--request-file", str(project / "request.txt"), "--project-dir", str(project),
                         "--catalog-dir", str(tmp_path / "catalog"), *config], capsys)
    assert code == 0, out
    return out, json.loads((Path(out["run"]) / "state.json").read_text())


def test_set_values_reach_the_run(tmp_path, capsys):
    out, s = _init(tmp_path, capsys, "--config-mga-retries", "5")
    assert s["mga_max_retries"] == 5
    assert out["settings"]["mga_max_retries"] == {"value": 5, "source": "plugin setting"}


@pytest.mark.parametrize("i", range(5))
def test_unfilled_values_fall_back_and_the_name_is_not_passed(tmp_path, capsys, i):
    r = leftovers("mga_retries")[i]
    out, s = _init(tmp_path, capsys, "--config-mga-retries", r)
    assert s["mga_max_retries"] == S.DEFAULT_MGA_RETRIES
    assert out["settings"]["mga_max_retries"]["source"] == settings.NOT_FILLED
    for text in (json.dumps(s), json.dumps(out)):
        assert "user_config" not in text and "<mga_retries>" not in text


def test_a_bad_set_value_is_an_input_error(tmp_path, capsys):
    project = tmp_path / "project"
    project.mkdir()
    (project / "request.txt").write_text("count words")
    code, out = call(S, ["init", "--request-file", str(project / "request.txt"), "--project-dir", str(project),
                         "--config-mga-retries", "three"], capsys)
    assert code == 2 and "plugin setting" in out["error"]


# ── monitor_minutes (FaaSr and Pegasus monitors) ──

@pytest.mark.parametrize("mod", [M, PM])
def test_monitor_minutes(monkeypatch, mod):
    monkeypatch.delenv("CURATE_MONITOR_MINUTES", raising=False)
    assert mod._setting(None, "CURATE_MONITOR_MINUTES", "4", 10.0, float) == (4.0, "plugin setting")
    for raw in leftovers("monitor_minutes"):
        assert mod._setting(None, "CURATE_MONITOR_MINUTES", raw, 10.0, float) == (10.0, settings.NOT_FILLED)
    assert mod._setting(None, "CURATE_MONITOR_MINUTES", "", 10.0, float) == (10.0, "default")
    with pytest.raises(mod.InputError, match="plugin setting"):  # not a traceback
        mod._setting(None, "CURATE_MONITOR_MINUTES", "ten", 10.0, float)
