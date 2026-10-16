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
                        code=code if code is not None else f"def {name}(folder):\n    pass\n",
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
    for v in ("", "  ", "${user_config.catalog_dir}"):
        assert catalog.resolve_catalog_dir(None, None, v)[1] == "default"


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
    assert catalog.main(["search-nodes", "--run", str(run), "--catalog-dir", str(tmp_path / "cat")]) == 0
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


def test_save_workflow_upserts_by_name(tmp_path):
    cat = catalog.Catalog(tmp_path)
    first = catalog.save_workflow(cat, _spec())
    wf = cat.workflows[first["id"]]
    wf.usage_count = 4
    cat.save_workflow(wf)
    second = catalog.save_workflow(catalog.Catalog(tmp_path), _spec())
    assert second == {"ok": True, "id": first["id"], "updated": True}
    assert catalog.Catalog(tmp_path).workflows[first["id"]].usage_count == 4


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
    catalog.save_workflow(src, _spec())
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
