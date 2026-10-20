import json
from pathlib import Path

import pytest

import spec as S
from conftest import call, ok
from runstate import spec_hash

FIX = Path(__file__).parent / "fixtures" / "e2-stages"


def state(run):
    return json.loads((Path(run) / "state.json").read_text())


@pytest.fixture
def imp(tmp_path, capsys):
    """Run `spec.py import` in a fresh project; returns (exit code, output, run path or None)."""
    def _imp(stage, src, *extra):
        argv = ["import", "--stage", stage, "--from", str(src), "--project-dir", str(tmp_path / "proj"),
                "--catalog-dir", str(tmp_path / "cat"), *map(str, extra)]
        code, out = call(S, argv, capsys)
        return code, out, Path(out["run"]) if out.get("run") else None
    return _imp


def runs_dir(tmp_path):
    d = tmp_path / "proj" / ".curate" / "runs"
    return sorted(d.iterdir()) if d.is_dir() else []


def last_history(run):
    return state(run)["history"][-1]


# ── per-stage formats ──

def test_skeleton_from_sketch(imp):
    code, out, run = imp("skeleton", FIX / "sketch.txt")
    s = state(run)
    assert code == 0 and s["stage"] == "skeleton" and s["user_request"].startswith("MapReduce:")
    assert out["next_skill"] == "curate-skills:curate-skeleton"
    assert last_history(run)["decision"] == "imported" and len(last_history(run)["sha256"]) == 64


def test_reuse_from_skeleton_is_approved(imp):
    code, out, run = imp("reuse", FIX / "skeleton.json")
    s = state(run)
    assert code == 0 and s["stage"] == "reuse" and s["skeleton_draft"] is None
    assert [n["name"] for n in s["spec"]["nodes"]] == ["generate_text", "split", "map", "shuffle", "reduce"]
    assert all(not n["inputs"] for n in s["spec"]["nodes"])
    assert [(h["gate"], h["decision"]) for h in s["history"]] == [(1, "approve"), (1, "imported")]
    assert out["next_skill"] == "curate-skills:curate-reuse" and out["warnings"]  # request built from spec


def test_reuse_review_stops_at_gate1(imp):
    code, out, run = imp("reuse", FIX / "skeleton.json", "--review")
    s = state(run)
    assert code == 0 and s["stage"] == "gate1" and s["skeleton_draft"]["spec"] and s["spec"] is None
    assert out["next_skill"] == "curate-skills:curate-skeleton"


def test_reuse_from_full_spec_keeps_io_as_seed(imp):
    code, out, run = imp("reuse", FIX / "spec-resolved.json")
    s = state(run)
    assert code == 0 and s["stage"] == "reuse" and s["spec_is_seed"]
    assert s["spec"]["nodes"][1]["inputs"]  # not stripped
    code, out, _ = imp("reuse", FIX / "spec-resolved.json", "--review")
    assert code == 2 and "skeleton.json" in out["error"]


def test_resolve_needs_candidates_and_known_catalog_ids(imp, tmp_path):
    code, out, run = imp("resolve", FIX / "skeleton.json", "--candidates", FIX / "candidates.json")
    s = state(run)
    assert code == 0 and s["stage"] == "resolve" and s["enriched_basis"] == spec_hash(s["spec"])
    assert out["next_skill"] == "curate-skills:curate-resolve"

    before = runs_dir(tmp_path)
    code, out, _ = imp("resolve", FIX / "skeleton.json")
    assert code == 2 and "--candidates" in out["error"] and runs_dir(tmp_path) == before  # run removed

    bad = json.loads((FIX / "candidates.json").read_text())
    bad[0].update(decision="reuse", catalog_id="00000000-0000-0000-0000-000000000000")
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    code, out, _ = imp("resolve", FIX / "skeleton.json", "--candidates", tmp_path / "bad.json")
    assert code == 2 and "not in this run's catalog" in out["error"] and runs_dir(tmp_path) == before


def test_resolve_has_no_review(imp):
    code, out, _ = imp("resolve", FIX / "skeleton.json", "--candidates", FIX / "candidates.json", "--review")
    assert code == 2 and "has none" in out["error"]


def test_generate_from_resolved_spec(imp, monkeypatch):
    code, out, run = imp("generate", FIX / "spec-resolved.json")
    s = state(run)
    assert code == 0 and s["stage"] == "generate" and out["next_skill"] == "curate-skills:curate-generate"
    assert [(h["gate"], h["decision"]) for h in s["history"]] == [(2, "imported"), (2, "approve")]
    code, out, run = imp("generate", FIX / "spec-resolved.json", "--review")
    assert code == 0 and state(run)["stage"] == "gate2"


def test_generate_refuses_incomplete_spec(imp, tmp_path):
    code, out, _ = imp("generate", FIX / "skeleton.json")
    assert code == 2 and "no inputs or outputs" in out["error"] and "curate-resolve" in out["error"]
    broken = json.loads((FIX / "spec-resolved.json").read_text())
    broken["nodes"][2]["description"] = ""
    broken["edges"] = broken["edges"][:-1]  # reduce no longer triggered
    (tmp_path / "broken.json").write_text(json.dumps(broken))
    code, out, _ = imp("generate", tmp_path / "broken.json")
    assert code == 2 and "need a description" in out["error"] and "errors" in out["error"]


def test_generate_with_functions(imp):
    code, out, _ = imp("generate", FIX / "spec-resolved.json", "--functions", FIX / "functions")
    assert code == 2 and "--existing-code keep" in out["error"]

    code, out, run = imp("generate", FIX / "spec-resolved.json", "--functions", FIX / "functions",
                         "--existing-code", "keep")
    nodes = {n["name"]: n for n in state(run)["spec"]["nodes"]}
    assert code == 0 and sorted(out["functions_imported"]) == sorted(nodes)
    assert all(n["source"] == "cached" and n["origin_kind"] == "import" for n in nodes.values())
    assert not nodes["map"]["code"].startswith("# Generated by curate-skills")
    assert nodes["map"]["code"].lstrip().startswith(("def ", "import ", "from ", '"""'))

    code, out, run = imp("generate", FIX / "spec-resolved.json", "--functions", FIX / "functions",
                         "--existing-code", "rewrite")
    assert {n["source"] for n in state(run)["spec"]["nodes"]} == {"adapt"}


def test_emit_from_spec_with_code(imp):
    code, out, run = imp("emit", FIX / "spec-with-code.json")
    s = state(run)
    assert code == 0 and s["stage"] == "emit" and out["next_skill"] == "curate-skills:curate-emit"
    assert any("not been stub-tested" in w for w in out["warnings"])
    code, out, _ = imp("emit", FIX / "spec-resolved.json")
    assert code == 2 and "have no code" in out["error"] and "curate-generate" in out["error"]
    code, out, run = imp("emit", FIX / "spec-resolved.json", "--functions", FIX / "functions")
    assert code == 0 and all(n["code"] for n in state(run)["spec"]["nodes"])


def test_finalize_import_refused(imp):
    code, out, _ = imp("finalize", FIX / "spec-with-code.json")
    assert code == 2 and "not supported" in out["error"]


def test_request_file_overrides_built_request(imp, tmp_path):
    (tmp_path / "req.txt").write_text("my own sketch")
    code, out, run = imp("reuse", FIX / "skeleton.json", "--request-file", tmp_path / "req.txt")
    assert state(run)["user_request"] == "my own sketch" and not out["warnings"]


def test_import_honours_init_settings(imp):
    code, out, run = imp("reuse", FIX / "skeleton.json", "--mga-retries", "0", "--stubtest-mode", "parity")
    s = state(run)
    assert s["mga_max_retries"] == 0 and s["stubtest_mode"] == "parity"


def test_review_import_can_be_resumed_at_its_gate(imp, capsys):
    code, out, run = imp("generate", FIX / "spec-resolved.json", "--review")
    out = ok(S, ["begin", "--run", str(run), "--stage", "resolve"], capsys)
    assert out["mode"] == "resume" and out["stage"] == "gate2"


# ── regression: a bundle spec.json with inline code (results log #17) ──

def test_generate_spec_with_inline_code_needs_explicit_choice(imp, tmp_path):
    before = runs_dir(tmp_path)
    code, out, _ = imp("generate", FIX / "spec-with-code.json")
    assert code == 2 and "already have code (from the spec itself)" in out["error"]
    assert all(f"--existing-code {m}" in out["error"] for m in ("keep", "rewrite", "ignore"))
    assert runs_dir(tmp_path) == before  # nothing half-created


@pytest.mark.parametrize("mode,source,tag", [("keep", "cached", "kept: your code"),
                                             ("rewrite", "adapt", "rewritten from your code"),
                                             ("ignore", "new", "[new]")])
def test_generate_inline_code_labels_match_what_happens(imp, capsys, mode, source, tag):
    import scratch
    code, out, run = imp("generate", FIX / "spec-with-code.json", "--existing-code", mode)
    nodes = state(run)["spec"]["nodes"]
    assert code == 0 and sorted(out["inline_code"]) == sorted(n["name"] for n in nodes)
    assert out["existing_code"] == mode and {n["source"] for n in nodes} == {source}
    assert all(bool(n["code"]) is (mode != "ignore") for n in nodes)
    built = ok(scratch, ["build", "--run", str(run)], capsys)
    # keep → no node is generated (no subagents); rewrite/ignore → every node gets a subagent
    assert all(n["generate"] is (mode != "keep") for n in built["nodes"])
    md = ok(S, ["show", "--run", str(run), "--gate", "3"], capsys)["markdown"]
    assert f"| `map` | {tag} |" in md and "[import]" not in md
    if mode == "rewrite":
        assert "seeded from import `map`" in (run / "functions" / "map" / "spec.md").read_text()


def test_old_functions_as_spelling_still_works(imp):
    for old, new_source in (("reuse", "cached"), ("seed", "adapt")):
        code, out, run = imp("generate", FIX / "spec-with-code.json", "--functions-as", old)
        assert code == 0 and out["existing_code"] == {"reuse": "keep", "seed": "rewrite"}[old]
        assert {n["source"] for n in state(run)["spec"]["nodes"]} == {new_source}


def test_existing_code_misuse_is_refused(imp, tmp_path):
    before = runs_dir(tmp_path)
    code, out, _ = imp("generate", FIX / "spec-with-code.json", "--existing-code", "keep",
                       "--functions-as", "seed")
    assert code == 2 and "contradicts" in out["error"]
    code, out, _ = imp("generate", FIX / "spec-resolved.json", "--functions", FIX / "functions",
                       "--existing-code", "ignore")
    assert code == 2 and "--functions has no effect" in out["error"]
    code, out, _ = imp("emit", FIX / "spec-with-code.json", "--existing-code", "keep")
    assert code == 2 and "only applies when starting at generate" in out["error"]
    assert runs_dir(tmp_path) == before


def test_existing_code_hidden_alias_not_in_help(capsys):
    with pytest.raises(SystemExit):
        S.main(["import", "--help"])
    help_text = capsys.readouterr().out
    assert "--existing-code" in help_text and "--functions-as" not in help_text
