import json
import uuid
from pathlib import Path

import pytest

import catalog
from curate_models import CatalogEntry, FunctionSpec, IOSpec, WorkflowSpec


@pytest.fixture(autouse=True)
def no_env_catalog(monkeypatch):
    monkeypatch.delenv("CURATE_CATALOG_DIR", raising=False)


def fn(name, desc, ins=(), outs=(), code=None, source="new"):
    return FunctionSpec(name=name, description=desc,
                        inputs=[IOSpec(name=i) for i in ins],
                        outputs=[IOSpec(name=o) for o in outs],
                        code=code if code is not None else
                        f"def main(inputs, outputs, params):\n    pass  # {name}\n",
                        source=source)


def seeded(root: Path) -> catalog.Catalog:
    cat = catalog.Catalog(root)
    specs = [
        fn("split_words", "split a text file into word chunks", ["words.txt"], ["chunk_{rank}.txt"]),
        fn("map_words", "count words in one chunk", ["chunk_{rank}.txt"], ["counts_{rank}.json"]),
        fn("shuffle_counts", "group partial counts by word", ["counts_{rank}.json"], ["groups.json"]),
        fn("reduce_counts", "sum grouped counts into totals", ["groups.json"], ["totals.json"]),
        fn("plot_histogram", "draw a bar chart histogram", ["totals.json"], ["hist.png"]),
        fn("train_svm", "train a support vector machine classifier", ["train.csv"], ["svm.json"]),
    ]
    for s in specs:
        cat.save_function(CatalogEntry(function_spec=s, keywords=s.name.split("_")))
    return cat


# ── catalog dir resolution ──

def test_resolution_order(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "state.json").write_text(json.dumps({"catalog_dir": str(tmp_path / "pinned")}))
    monkeypatch.setenv("CURATE_CATALOG_DIR", str(tmp_path / "env"))

    r = catalog.resolve_catalog_dir
    assert r(str(tmp_path / "flag"), str(run), str(tmp_path / "cfg")) == (tmp_path / "flag", "--catalog-dir")
    assert r(None, str(run), str(tmp_path / "cfg")) == (tmp_path / "pinned", "run state")
    assert r(None, None, str(tmp_path / "cfg")) == (tmp_path / "env", "CURATE_CATALOG_DIR")
    monkeypatch.delenv("CURATE_CATALOG_DIR")
    assert r(None, None, str(tmp_path / "cfg")) == (tmp_path / "cfg", "plugin setting")
    assert r(None, None, None) == (catalog.DEFAULT_CATALOG_DIR, "default")


def test_unset_plugin_setting_is_ignored():
    for v in ("", "  "):
        assert catalog.resolve_catalog_dir(None, None, v)[1] == "default"
    assert catalog.resolve_catalog_dir(None, None, "${user_config.catalog_dir}")[1] == \
        "default (plugin setting not filled in)"


# ── BM25 search ──

def test_search_ranks_best_match_first(tmp_path):
    cat = seeded(tmp_path)
    hits = cat.search("split words text file words.txt")
    assert hits[0][1].function_spec.name == "split_words"
    assert len(hits) <= 5


def test_search_drops_zero_scores(tmp_path):
    cat = seeded(tmp_path)
    assert cat.search("quantum chromodynamics lattice") == []
    assert catalog.Catalog(tmp_path / "empty").search("anything") == []


def test_node_query_matches_original_format():
    q = catalog.node_query(fn("map_words", "count words", ["a.txt"], ["b.json"]))
    assert q == "map words count words a.txt b.json"


def test_search_nodes_cli(tmp_path, capsys):
    seeded(tmp_path / "cat")
    run = tmp_path / "run"
    run.mkdir()
    spec = WorkflowSpec(name="wc", nodes=[fn("reduce_counts", "sum grouped counts", code="")])
    (run / "state.json").write_text(json.dumps({"skeleton_draft": {"spec": json.loads(spec.model_dump_json())}}))
    assert catalog.main(["search-nodes", "--session", str(run), "--catalog-dir", str(tmp_path / "cat")]) == 0
    out = json.loads(capsys.readouterr().out)
    hit = out["nodes"]["reduce_counts"]["hits"][0]
    assert hit["name"] == "reduce_counts" and hit["line"].startswith(f"[{hit['id']}] reduce_counts:")


# ── save rules (orchestrator._do_save) ──

def _spec():
    return WorkflowSpec(name="wc", nodes=[
        fn("split_words", "Split the input text into chunks for mapping"),
        fn("no_code_yet", "x", code=""),
        fn("reused_one", "from catalog", source="catalog"),
        fn("map_words", "count words"),
    ])


def test_save_functions_rules_and_metadata(tmp_path):
    cat = catalog.Catalog(tmp_path)
    spec = _spec()
    res = catalog.save_functions(cat, spec, {}, only_verified=False)
    assert [s["name"] for s in res["stored"]] == ["split_words", "map_words"]
    reasons = {s["name"]: s["reason"] for s in res["skipped"]}
    assert reasons == {"no_code_yet": "no code", "reused_one": "already in catalog"}

    stored_id = res["stored"][0]["id"]
    on_disk = json.loads((tmp_path / "functions" / f"{stored_id}.json").read_text())
    assert on_disk["keywords"] == ["split", "words", "split", "the", "input", "text", "into"]
    assert on_disk["provenance"] == "workflow:wc"
    assert on_disk["usage_count"] == 0
    assert not set(catalog.CATALOG_EXCLUDED_SPEC_FIELDS) & set(on_disk["function_spec"])

    again = catalog.save_functions(catalog.Catalog(tmp_path), spec, {}, only_verified=False)
    assert again["stored"] == []  # identical code is never stored twice


def test_save_only_verified(tmp_path):
    state = {"mga": {"split_words": {"status": "passed"}, "map_words": {"status": "failed"}}}
    res = catalog.save_functions(catalog.Catalog(tmp_path), _spec(), state, only_verified=True)
    assert [s["name"] for s in res["stored"]] == ["split_words"]
    assert {"name": "map_words", "reason": "not verified"} in res["skipped"]


def test_only_verified_requires_run(tmp_path, capsys):
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(_spec().model_dump_json())
    assert catalog.main(["save-functions", "--spec", str(spec_file), "--only-verified",
                         "--catalog-dir", str(tmp_path / "c")]) == 2


def _e2():
    import json as _json
    from curate_models import WorkflowSpec
    return WorkflowSpec.model_validate(_json.loads(
        (Path(__file__).parent / "fixtures" / "e2-stages" / "spec-with-code.json").read_text()))


def test_save_workflow_stores_the_published_version_never_its_own(tmp_path):
    cat = catalog.Catalog(tmp_path)
    e2 = _e2()
    assert catalog.save_workflow(cat, e2)["ok"] is False  # no published version: nothing is numbered here
    first = catalog.save_workflow(cat, e2, workflow_id="mapreduce-word-count-v1")
    assert first["ok"] and first["stored"] and first["version"] == 1
    assert (tmp_path / "workflows" / "mapreduce-word-count-v1.json").is_file()
    wf = cat.workflows[first["id"]]
    wf.usage_count = 4
    cat.save_workflow(wf)
    again = catalog.save_workflow(catalog.Catalog(tmp_path), e2, workflow_id="mapreduce-word-count-v1")
    assert again["ok"] and again["stored"] is False and again["already_in_catalog"]
    changed = e2.model_copy(deep=True)
    changed.nodes[2].code += "\n# changed\n"
    clash = catalog.save_workflow(catalog.Catalog(tmp_path), changed, workflow_id="mapreduce-word-count-v1")
    assert clash["ok"] is False and "never updated" in clash["error"]
    second = catalog.save_workflow(catalog.Catalog(tmp_path), e2, workflow_id="mapreduce-word-count-v2")
    assert second["stored"] and second["duplicates"] == ["mapreduce-word-count-v1"]
    reloaded = catalog.Catalog(tmp_path)
    assert reloaded.workflows[first["id"]].usage_count == 4
    assert reloaded.workflow_by_name(e2.name).workflow_id == "mapreduce-word-count-v2"


def _approved_run(tmp_path, name, spec, on_duplicate=None, new_version_of=None, catalog_dir=None):
    """A session at Gate 4 with a draft, approved through spec.publish_version (what Gate 4
    approval does); returns the run folder with state.json written."""
    import emit
    import spec as S
    project = tmp_path / "project"
    run = project / ".curate" / "runs" / name
    run.mkdir(parents=True)
    s = {"run_id": name, "project_dir": str(project), "catalog_dir": str(catalog_dir or tmp_path / "cat"),
         "spec": json.loads(spec.model_dump_json()), "draft_dir": emit.write_draft(spec, run)["dir"]}
    if new_version_of:  # what spec.py set-name NAME --new-version records
        s.update(workflow_name=new_version_of, workflow_lineage={"new_version_of": new_version_of})
    s.update(S.publish_version(s, run, on_duplicate))
    (run / "state.json").write_text(json.dumps(s))
    return run, s


def test_publishing_then_saving_gives_the_same_versions(tmp_path, capsys):
    """The catalog entry and the version folder always carry the same workflow ID and version, also
    when the user drops a duplicate at Gate 4 (the existing version is used and saved, not a new one)."""
    import spec as S
    cat_dir = tmp_path / "cat"
    run1, s1 = _approved_run(tmp_path, "r1", _e2())
    assert s1["workflow_id"] == "mapreduce-word-count-v1" and Path(s1["version_dir"]).is_dir()
    assert catalog.main(["save-workflow", "--session", str(run1), "--catalog-dir", str(cat_dir)]) == 0
    saved = json.loads(capsys.readouterr().out)
    assert (saved["workflow_id"], saved["version"]) == (s1["workflow_id"], s1["workflow_version"])
    # an identical generation: Gate 4 asks; dropping it reuses v1, and saving stores nothing new
    with pytest.raises(S.InputError, match="already used"):  # a taken name with no lineage: ask first
        _approved_run(tmp_path, "r2a", _e2())
    with pytest.raises(S.InputError, match="identical to mapreduce-word-count-v1"):
        _approved_run(tmp_path, "r2", _e2(), new_version_of="mapreduce-word-count")
    run3, s3 = _approved_run(tmp_path, "r3", _e2(), on_duplicate="drop", new_version_of="mapreduce-word-count")
    assert s3["workflow_id"] == "mapreduce-word-count-v1" and s3["version_reused"]
    assert not (Path(s1["version_dir"]).parent / "mapreduce-word-count-v2").exists()
    assert catalog.main(["save-workflow", "--session", str(run3), "--catalog-dir", str(cat_dir)]) == 0
    dropped = json.loads(capsys.readouterr().out)
    assert dropped["workflow_id"] == "mapreduce-word-count-v1" and dropped["stored"] is False
    # keeping it makes v2, in the project and then in the catalog
    run4, s4 = _approved_run(tmp_path, "r4", _e2(), on_duplicate="keep", new_version_of="mapreduce-word-count")
    assert s4["workflow_id"] == "mapreduce-word-count-v2"
    assert catalog.main(["save-workflow", "--session", str(run4), "--catalog-dir", str(cat_dir)]) == 0
    kept = json.loads(capsys.readouterr().out)
    assert (kept["workflow_id"], kept["version"], kept["stored"]) == ("mapreduce-word-count-v2", 2, True)
    # a version already in the catalog counts when numbering the next one (also from another project)
    other = tmp_path / "other"
    other.mkdir()
    changed = _e2()
    changed.nodes[2].code += "\n# changed\n"
    run5, s5 = _approved_run(other, "r5", changed)
    assert s5["workflow_id"] == "mapreduce-word-count-v1"  # its own catalog is empty
    import ids
    plan = ids.plan_publish(ids.draft_dir(run5), "mapreduce-word-count", other / "project",
                            catalog=catalog.Catalog(cat_dir))
    assert plan["workflow_id"] == "mapreduce-word-count-v3"

def test_increment_usage_cli(tmp_path, capsys):
    cat = seeded(tmp_path)
    eid = next(iter(cat.functions))
    assert catalog.main(["increment-usage", "--id", eid, "--catalog-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["usage_count"] == 1
    assert catalog.Catalog(tmp_path).functions[eid].usage_count == 1
    assert catalog.main(["increment-usage", "--id", "nope", "--catalog-dir", str(tmp_path)]) == 1


# ── import / export ──

def test_import_original_flat_layout_with_companion_py(tmp_path):
    legacy = tmp_path / "legacy_data"
    legacy.mkdir()
    eid = str(uuid.uuid4())
    (legacy / f"{eid}.json").write_text(json.dumps({
        "id": eid, "keywords": ["split"], "provenance": "workflow:old", "usage_count": 2,
        "function_spec": {"name": "split_words", "description": "split text", "source": "new"},
    }))
    (legacy / f"{eid}.py").write_text("# header\n# more\ndef split_words(folder):\n    pass\n")
    (legacy / "notes.json").write_text("[1, 2]")

    cat = catalog.Catalog(tmp_path / "cat")
    res = catalog.import_entries(cat, [str(legacy)], dedupe_code=False, overwrite=False)
    assert [e["id"] for e in res["imported"]] == [eid]
    assert [(Path(s["file"]).name, s["reason"]) for s in res["skipped"]] == [("notes.json", "not a catalog entry")]
    entry = catalog.Catalog(tmp_path / "cat").functions[eid]
    assert entry.function_spec.code.startswith("def split_words")
    assert entry.usage_count == 2

    again = catalog.import_entries(cat, [str(legacy)], dedupe_code=False, overwrite=False)
    assert again["imported"] == []
    assert sorted((Path(s["file"]).name, s["reason"]) for s in again["skipped"]) == [
        (f"{eid}.json", "id exists"), ("notes.json", "not a catalog entry")]


def test_import_dedupe_code(tmp_path):
    src = seeded(tmp_path / "src")
    dup = next(iter(src.functions.values())).model_copy(update={"id": str(uuid.uuid4())})
    src.save_function(dup)
    dst = catalog.Catalog(tmp_path / "dst")
    res = catalog.import_entries(dst, [str(tmp_path / "src")], dedupe_code=True, overwrite=False)
    assert len(res["imported"]) == 6
    assert sum(s["reason"] == "identical code" for s in res["skipped"]) == 1


def test_export_import_round_trip(tmp_path):
    src = seeded(tmp_path / "src")
    catalog.save_workflow(src, _spec(), workflow_id="word-count-v1")
    some = list(src.functions)[:2]
    res = catalog.export_entries(src, tmp_path / "out", some)
    assert res["ok"] and res["exported"] == some
    full = catalog.export_entries(src, tmp_path / "all", None)
    assert len(full["exported"]) == 7
    back = catalog.Catalog(tmp_path / "all")
    assert set(back.functions) == set(src.functions) and set(back.workflows) == set(src.workflows)
    assert not catalog.export_entries(src, tmp_path / "x", ["missing"])["ok"]


def test_where_does_not_create(tmp_path, capsys):
    target = tmp_path / "nothing"
    assert catalog.main(["where", "--catalog-dir", str(target)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["exists"] is False and not target.exists()


def test_inherited_bm25_quirk_single_entry_never_matches(tmp_path):
    # Same as the original store.py: BM25Okapi's IDF is <= 0 when a term occurs in
    # every document, so a one-entry catalog returns no hits even for an exact match.
    cat = catalog.Catalog(tmp_path)
    s = fn("split_words", "split text into chunks")
    cat.save_function(CatalogEntry(function_spec=s, keywords=["split", "words"]))
    assert cat.search("split text") == []


def test_dry_run_writes_nothing(tmp_path):
    cat = catalog.Catalog(tmp_path / "c")
    res = catalog.save_functions(cat, _spec(), {}, only_verified=False, dry_run=True)
    assert res["dry_run"] and [s["name"] for s in res["stored"]] == ["split_words", "map_words"]
    assert all(s["id"] is None for s in res["stored"]) and not (tmp_path / "c").exists()
    wf = catalog.save_workflow(cat, _spec(), dry_run=True)
    assert wf["ok"] and wf["dry_run"] and wf["workflow_id"] is None  # nothing published, nothing numbered
    assert not (tmp_path / "c").exists()


SEED = Path(__file__).parent / "fixtures" / "catalog-e2-mapreduce"


def test_e2_seed_catalog_is_complete():
    cat = catalog.Catalog(SEED)
    names = sorted(e.function_spec.name for e in cat.functions.values())
    assert names == ["generate_text", "map", "reduce", "shuffle", "split"]
    assert all(e.function_spec.code and e.provenance == "workflow:mapreduce_word_count"
               for e in cat.functions.values())
    assert [w.workflow_spec.name for w in cat.workflows.values()] == ["mapreduce_word_count"]
    from plain_contract import is_plain  # the seed catalog is migrated: plain code only
    assert all(is_plain(e.function_spec.code) for e in cat.functions.values())
    assert all(is_plain(n.code) for w in cat.workflows.values() for n in w.workflow_spec.nodes)


@pytest.mark.parametrize("query,expected,top", [
    ("split the word dataset", "split", True),
    ("map count words per batch", "map", True),
    ("reduce sum counts per word", "reduce", True),
    ("shuffle group partial counts by word", "shuffle", False),  # in the top 5, not first
])
def test_e2_seed_retrieval_for_e4_steps(query, expected, top):
    hits = [e.function_spec.name for _, e in catalog.Catalog(SEED).search(query, k=5)]
    assert expected in hits
    if top:
        assert hits[0] == expected


def test_curate_catalog_skill_commands_run_as_documented(tmp_path):
    """Every `catalog.py …` row of the skill's table, with the settings flag appended exactly as the
    skill says (single quotes, unsubstituted as under --plugin-dir), parses and runs in sh."""
    import os
    import re
    import subprocess
    import sys
    plugin = Path(__file__).resolve().parents[1]
    text = (plugin / "skills" / "curate-catalog" / "SKILL.md").read_text()
    shape = ("`uv run --quiet ${CLAUDE_PLUGIN_ROOT}/scripts/catalog.py <subcommand> [its arguments] "
             "--config-catalog-dir '${user_config.catalog_dir}'`")
    assert shape in text
    suffix = " --config-catalog-dir '${user_config.catalog_dir}'"
    rows = re.findall(r"\| `(catalog\.py [^`]+)`", text)
    assert {r.split()[1] for r in rows} == {"where", "list", "search", "init", "import", "export"}
    seed = plugin / "tests" / "fixtures" / "catalog-e2-mapreduce"
    fill = {"<query>": "count words", "<path>...": str(seed), "<dir>": str(tmp_path / "out"),
            " [--workflows]": " --workflows", " [--ids ...]": ""}
    cat = tmp_path / "cat"
    first = {"init": 0, "import": 1}  # create and seed the catalog before the read-only commands
    for row in sorted(rows, key=lambda r: first.get(r.split()[1], 2)):
        cmd = row
        for k, v in fill.items():
            cmd = cmd.replace(k, v)
        line = f'"{sys.executable}" "{plugin}/scripts/{cmd.split(" ", 1)[0]}" {cmd.split(" ", 1)[1]}{suffix}'
        proc = subprocess.run(["sh", "-c", line], capture_output=True, text=True, timeout=60,
                              env={**os.environ, "CURATE_CATALOG_DIR": str(cat)})
        assert proc.returncode == 0, (row, proc.stdout, proc.stderr)
        out = json.loads(proc.stdout)
        assert out.get("ok", True) is not False, (row, out)
    where = subprocess.run(["sh", "-c", f'"{sys.executable}" "{plugin}/scripts/catalog.py" where{suffix}'],
                           capture_output=True, text=True, env={**os.environ, "CURATE_CATALOG_DIR": str(cat)})
    info = json.loads(where.stdout)
    assert str(cat.resolve()) in json.dumps(info) and "CURATE_CATALOG_DIR" in json.dumps(info)
    # the old, wrong order is rejected by the script
    bad = subprocess.run(["sh", "-c", f'"{sys.executable}" "{plugin}/scripts/catalog.py"{suffix} where'],
                         capture_output=True, text=True)
    assert bad.returncode == 2


def test_only_plain_code_is_saved(tmp_path):
    cat = catalog.Catalog(tmp_path / "cat")
    old = fn("split_words", "split", code="def split_words(folder, output1):\n    faasr_log('x')\n")
    spec = WorkflowSpec(name="wc", nodes=[old, fn("map_words", "map")])
    res = catalog.save_functions(cat, spec, {}, only_verified=False)
    assert [s["name"] for s in res["stored"]] == ["map_words"]
    assert res["skipped"][0]["name"] == "split_words" and "not a plain function" in res["skipped"][0]["reason"]
    res = catalog.save_workflow(cat, spec)
    assert res["ok"] is False and "split_words are not plain functions" in res["error"]
    assert catalog.Catalog(tmp_path / "cat").workflows == {}


@pytest.mark.parametrize("stored_folder", [True, False], ids=["catalog-folder", "rebuilt-from-spec"])
def test_dropping_a_duplicate_only_in_the_catalog_copies_that_version(tmp_path, capsys, stored_folder):
    """Gate 4's "drop the new one" when the identical version is only in the global catalog: that
    version (workflow.yml, inputs.yml, functions/; not runs/) is copied into this project with the same
    ID, read-only, checked against the catalog's content hash."""
    import ids
    cat_dir = tmp_path / "cat"
    run1, s1 = _approved_run(tmp_path / "a", "r1", _e2())
    if stored_folder:
        (Path(s1["version_dir"]) / "runs" / "x").mkdir(parents=True)
        assert catalog.main(["save-workflow", "--session", str(run1), "--catalog-dir", str(cat_dir)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert Path(out["version_folder"]) == cat_dir / "versions" / "mapreduce-word-count-v1"
        assert not (cat_dir / "versions" / "mapreduce-word-count-v1" / "runs").exists()
    else:  # an entry saved without its folder: rebuilt from the stored spec
        catalog.save_workflow(catalog.Catalog(cat_dir), _e2(), workflow_id="mapreduce-word-count-v1")
        assert not (cat_dir / "versions").exists()
    # another project, same global catalog: an identical generation, dropped
    other = tmp_path / "b"
    run2, s2 = _approved_run(other, "r2", _e2(), on_duplicate="drop", new_version_of="mapreduce-word-count",
                             catalog_dir=cat_dir)
    vdir = Path(s2["version_dir"])
    assert s2["workflow_id"] == "mapreduce-word-count-v1" and s2["version_reused"]
    assert vdir == other / "project" / "workflows" / "mapreduce-word-count-v1"
    # the version as published, with the approved.json it carries (when the catalog kept its folder)
    assert sorted(p.name for p in vdir.iterdir()) == \
        (["approved.json"] if stored_folder else []) + ["functions", "inputs.yml", "workflow.yml"]
    assert not (vdir / "functions" / "map.py").stat().st_mode & 0o222
    assert ids.draft_content_hash(vdir) == s1["workflow_content_hash"]
    assert s2["version_hash"] == ids.version_hash(vdir)


def test_a_tampered_catalog_copy_is_not_used(tmp_path, capsys):
    import spec as S
    cat_dir = tmp_path / "cat"
    run1, _ = _approved_run(tmp_path / "a", "r1", _e2())
    assert catalog.main(["save-workflow", "--session", str(run1), "--catalog-dir", str(cat_dir)]) == 0
    capsys.readouterr()
    f = cat_dir / "versions" / "mapreduce-word-count-v1" / "functions" / "map.py"
    f.chmod(0o644)
    f.write_text(f.read_text() + "\n# tampered\n")
    with pytest.raises(S.InputError, match="does not match its content hash"):
        _approved_run(tmp_path / "b", "r2", _e2(), on_duplicate="drop", new_version_of="mapreduce-word-count",
                      catalog_dir=cat_dir)
    assert not (tmp_path / "b" / "project" / "workflows" / "mapreduce-word-count-v1").exists()
