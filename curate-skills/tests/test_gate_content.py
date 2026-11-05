"""Every gate shows its full content (and saves it to review/gate<N>.md) before it asks."""
import json
from pathlib import Path

import pytest

import spec as S
from conftest import ok
from test_spec import seeded_catalog  # noqa: F401  (fixture)
from curate_models import FunctionSpec, WorkflowSpec

ROOT = Path(__file__).resolve().parents[1]


def weather():
    return WorkflowSpec(
        name="weather", description="compare winter weather", entry="download",
        nodes=[FunctionSpec(name="download", description="Download the station CSV.",
                            code="def download():\n    return 'csv'\n", dependencies=["requests"]),
               FunctionSpec(name="process", rank=3, description="One variable per copy.",
                            code="def process():\n    pass\n"),
               FunctionSpec(name="plot", description="Plot three panels.", source="catalog",
                            origin_kind="catalog", origin_name="plot0", code="def plot():\n    pass\n")],
        edges=[{"from_node": "download", "to_node": "process"}, {"from_node": "process", "to_node": "plot"}])


def show(run, gate, capsys):
    return ok(S, ["show", "--run", str(run), "--gate", str(gate)], capsys)


def test_gate1_shows_one_block_per_step_in_order(make_run, capsys):
    run = make_run(skeleton_draft={"spec": json.loads(weather().model_dump_json())})
    md = show(run, 1, capsys)["markdown"]
    blocks = md.split("```text\n")[1].split("\n```")[0].split("\n\n")
    assert [b.splitlines() for b in blocks] == [
        ["1. download  · 1 copy", "   Download the station CSV."],
        ["2. process  · 3 copies", "   One variable per copy."],
        ["3. plot  · 1 copy", "   Plot three panels."]]
    assert "| # |" not in md  # no wide table


def test_gate2_shows_one_block_per_function(make_run, capsys):
    run = make_run(spec=weather())
    md = show(run, 2, capsys)["markdown"]
    blocks = md.split("```text\n")[1].split("\n```")[0]
    assert blocks.split("\n\n")[0].splitlines() == [
        "download  [new] · 1 copy", "  in:  —", "  out: —", "  packages: requests · secrets: —"]
    assert "process  [new] · 3 copies" in blocks and "plot  [catalog] ← [catalog] plot0 · 1 copy" in blocks
    assert "| Function |" not in md  # no wide table: it wraps in a terminal


def test_gate3_is_short_per_function_and_full_code_is_on_request(make_run, capsys):
    run = make_run(spec=weather(), mga={"download": {"status": "passed", "attempts": 1},
                                        "process": {"status": "failed", "attempts": 2,
                                                    "last_output_tail": "=== rank 1/3 ===\nline a\nline b\nValueError: rank out of range\n"},
                                        "plot": {"status": "reused"}})
    out = show(run, 3, capsys)
    md = out["markdown"]
    blocks = md.split("```text\n")[1].split("\n```")[0].split("\n\n")
    run_rel = __import__("os").path.relpath(run, json.loads((run / "state.json").read_text())["project_dir"])
    assert blocks[0].splitlines() == [
        "download  [new] · 1 copy · stub test: passed", "  Download the station CSV.", "  in:  —", "  out: —",
        f"  code: 2 lines · {run_rel}/functions/download/download.py", "  assumptions made while coding: none"]
    assert blocks[1].splitlines()[0] == "process  [new] · 3 copies · stub test: failed (2 attempts)"
    assert "  last test lines:\n    line a\n    line b\n    ValueError: rank out of range" in blocks[1]
    assert "  kept (not regenerated)" in blocks[2] and "stub test: passed (not regenerated)" in blocks[2]
    assert "def download():" not in md and "```python" not in md  # no code in the default display
    assert "**Show the full code**" in md
    assert ok(S, ["ask", "--run", str(run), "--gate", "3"], capsys)["options"] == ["Approve", "Show the full code", "Stop here"]
    full = ok(S, ["show", "--run", str(run), "--gate", "3", "--full"], capsys)
    for body in ("def download():\n    return 'csv'", "def process():\n    pass", "def plot():\n    pass"):
        assert f"```python\n{body}\n```" in full["markdown"]
    assert full["markdown"].endswith(out["marker"])
    review = (run / "review" / "gate3.md").read_text()
    assert review.startswith(md) and "def download():\n    return 'csv'" in review  # full code kept on file
    code, err = __import__("conftest").call(S, ["show", "--run", str(run), "--gate", "2", "--full"], capsys)
    assert code == 2 and "only for Gate 3" in err["error"]


def test_gate4_shows_the_workflow_and_every_file_in_the_bundle(make_run, capsys, tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "functions").mkdir(parents=True)
    for f, text in {"workflow.json": "{}", "functions/download.py": "x", "README.md": "r",
                    "functions/.DS_Store": "junk"}.items():
        (bundle / f).write_text(text)
    run = make_run(spec=weather(), bundle_dir=str(bundle), validation={"ok": True, "level": "faasr_py"})
    md = show(run, 4, capsys)["markdown"]
    assert "**weather** — compare winter weather" in md
    blocks = md.split("```text\n")[1].split("\n```")[0].split("\n\n")
    assert blocks[1].splitlines() == ["2. process  [new] · 3 copies", "   One variable per copy.", "   in:  —", "   out: —"]
    files = md.split("Files in the bundle:")[1].split("\n\n")[0].strip().splitlines()
    assert files == ["- `README.md` (1 bytes)", "- `functions/download.py` (1 bytes)", "- `workflow.json` (2 bytes)"]


def test_gate5_shows_the_run_summary_and_what_would_be_saved(make_run, capsys):
    run = make_run(spec=weather(), stage="finalize", validation={"ok": True, "level": "faasr_py"},
                   mga={"download": {"status": "passed"}, "process": {"status": "failed"},
                        "plot": {"status": "reused"}})
    out = show(run, 5, capsys)
    md = out["markdown"]
    assert md.startswith("## Run summary — weather") and "### What would be saved" in md
    assert "- `download` — would be stored (keywords: download, download, the, station, csv.)" in md
    assert "- `process` — skipped: not verified" in md and "- `plot` — skipped: already in catalog" in md
    assert "Workflow `weather`: would be a new entry" in md
    q = ok(S, ["ask", "--run", str(run), "--gate", "5"], capsys)
    assert q["header"] == "Gate 5" and q["question"] == "Save to the catalog?"
    assert not list((Path(json.loads((run / "state.json").read_text())["catalog_dir"])).rglob("*.json"))  # dry run


@pytest.mark.parametrize("gate", [1, 2, 3, 4, 5])
def test_show_saves_the_same_content_to_the_runs_review_folder(make_run, capsys, gate):
    run = make_run(skeleton_draft={"spec": json.loads(weather().model_dump_json())}, spec=weather(),
                   stage="finalize", validation={"ok": True, "level": "faasr_py"}, bundle_dir="/b")
    out = show(run, gate, capsys)
    review = run / "review" / f"gate{gate}.md"
    assert review.read_text().startswith(out["markdown"]) and out["review_file"] == str(review)
    if gate != 3:
        assert review.read_text() == out["markdown"] + "\n"
    assert out["review_line"] == f"Full review: {review}"


def test_gate4_and_the_bundle_readme_give_current_deploy_advice(make_run, capsys, tmp_path):
    import emit
    md = show(make_run(spec=weather(), validation={"ok": True, "level": "faasr_py"}), 4, capsys)["markdown"]
    assert "Placeholders are filled from your settings at deploy; run `/curate-skills:curate-doctor --deploy --online` " \
           "if you haven't." in md and "before deploying" not in md
    readme = emit._bundle_readme(weather(), {"WorkflowName": "weather"}, [], {"level": "faasr_py", "ok": True},
                                 ["FAASR_S3_BUCKET"])
    assert "fills the placeholders below from your settings" in readme and "re-run bundle" not in readme
    assert "- `<FAASR_S3_BUCKET>` — from `FAASR_S3_BUCKET`" in readme


def test_every_gate_prints_the_content_then_asks():
    gates = " ".join((ROOT / "references" / "gates.md").read_text().split())
    assert "then its `review_line`** (`Full review: <path>`" in gates
    assert "**Print the content first, then ask — never ask before printing it**" in gates
    for stage in ("skeleton", "resolve", "generate", "emit", "finalize"):
        text = " ".join((ROOT / "references" / "stages" / f"{stage}.md").read_text().split())
        assert "`spec.py show --run <run> --gate" in text, stage
        assert "review_line" in text and "never ask before printing it" in text, stage
    fin = " ".join((ROOT / "references" / "stages" / "finalize.md").read_text().split())
    assert fin.index("`spec.py show --run <run> --gate 5`") < fin.index("**Gate 5 — ask**")


# ── assumptions: resolve records them (Gate 2), curate-mga reports more (Gate 3) ──

WINDOW = '"extend that window by 30 days" -> 30 days added after the end date, baseline only'
UNITS = "precipitation units -> tenths of mm in the data, converted to mm"


def test_merge_resolved_requires_the_assumptions_list(make_run, capsys, seeded_catalog, monkeypatch):
    import test_spec as T
    saved = dict(T.RESOLVED)
    monkeypatch.setattr(T, "RESOLVED", {k: v for k, v in saved.items() if k != "assumptions"})
    run, (code, out) = T.resolved_run(make_run, capsys, seeded_catalog)
    assert code == 2 and 'needs "assumptions": {"affects_results"' in out["error"] and "[] if there are none" in out["error"]
    monkeypatch.setattr(T, "RESOLVED", {**saved, "assumptions": ["  leap days ->  29 Feb dropped "]})
    run, (code, out) = T.resolved_run(make_run, capsys, seeded_catalog)
    assert code == 0, out
    assert json.loads((run / "state.json").read_text())["spec"]["assumptions"] == ["leap days -> 29 Feb dropped"]
    monkeypatch.setattr(T, "RESOLVED", {**saved, "assumptions": []})  # empty is fine; the field is required
    run, (code, out) = T.resolved_run(make_run, capsys, seeded_catalog)
    assert code == 0, out
    grouped = {"affects_results": [WINDOW], "presentation": ["panel layout -> stacked"]}
    monkeypatch.setattr(T, "RESOLVED", {**saved, "assumptions": grouped})
    run, (code, out) = T.resolved_run(make_run, capsys, seeded_catalog)
    sp = json.loads((run / "state.json").read_text())["spec"]
    assert code == 0 and sp["assumptions"] == [WINDOW] and sp["presentation_assumptions"] == ["panel layout -> stacked"]
    monkeypatch.setattr(T, "RESOLVED", {**saved, "assumptions": {"other": ["x"]}})
    run, (code, out) = T.resolved_run(make_run, capsys, seeded_catalog)
    assert code == 2


LAYOUT = "panel layout -> three panels stacked vertically, shared x-axis"
SOURCE = "data source -> https://www.ncei.noaa.gov/.../USC00351862.csv"


def test_gate2_prints_the_assumptions_by_impact(make_run, capsys):
    spec = weather().model_copy(update={"assumptions": [WINDOW, UNITS], "presentation_assumptions": [LAYOUT, SOURCE]})
    md = show(make_run(spec=spec), 2, capsys)["markdown"]
    section = md.split("### Assumptions")[1].split("\n\n— end of")[0]
    assert section.strip().splitlines()[1:] == ["", "**Affects the results**", f"- {WINDOW}", f"- {UNITS}", "",
                                                "**Presentation and format**", f"- {LAYOUT}", f"- {SOURCE}"]
    assert "ask for it as a change" in section and "?" not in section  # informational: no question
    md = show(make_run(spec=weather()), 2, capsys)["markdown"]
    assert md.split("### Assumptions")[1].split("\n\n— end of")[0].strip().endswith("- none")


def test_a_plain_list_from_an_older_run_is_sorted_by_topic():
    lines = [LAYOUT, WINDOW, "file formats -> CSV and PNG (150 dpi)", UNITS, SOURCE,
             "leap days -> Feb 29 dropped", "missing values -> skipped"]
    results, presentation = S.split_assumptions(lines)
    assert results == [WINDOW, UNITS, "leap days -> Feb 29 dropped", "missing values -> skipped"]
    assert presentation == [LAYOUT, "file formats -> CSV and PNG (150 dpi)", SOURCE]


def test_mga_assumptions_are_recorded_and_listed_at_gate3(make_run, capsys):
    import scratch
    run = make_run(spec=weather(), mga={"download": {"status": "pending"}, "process": {"status": "pending"},
                                        "plot": {"status": "reused"}})
    for name, text in (("download", "none\n"),
                       ("process", "- leap days -> 29 Feb dropped from the baseline\n\nmissing values -> skipped\n")):
        (run / "functions" / name).mkdir(parents=True, exist_ok=True)
        (run / "functions" / name / "assumptions.txt").write_text(text)
        ok(scratch, ["record", "--run", str(run), "--node", name, "--status", "passed"], capsys)
    mga = json.loads((run / "state.json").read_text())["mga"]
    assert mga["download"]["assumptions"] == []
    assert mga["process"]["assumptions"] == ["leap days -> 29 Feb dropped from the baseline", "missing values -> skipped"]
    md = show(run, 3, capsys)["markdown"]
    process = md.split("\nprocess  ")[1].split("\n\n")[0]
    assert process.endswith("  assumptions made while coding: \n    - leap days -> 29 Feb dropped from the baseline\n"
                            "    - missing values -> skipped")
    assert md.split("\ndownload  ")[1].split("\n\n")[0].endswith("  assumptions made while coding: none")


def test_the_summary_repeats_only_assumptions_already_shown(make_run, capsys):
    spec = weather().model_copy(update={"assumptions": [WINDOW], "presentation_assumptions": [LAYOUT]})
    run = make_run(spec=spec, stage="finalize", validation={"ok": True, "level": "faasr_py"},
                   mga={"process": {"status": "passed", "assumptions": ["leap days -> dropped"]}})
    md = ok(S, ["summary", "--run", str(run)], capsys)["markdown"]
    section = md.split("Assumptions (shown at Gates 2 and 3):")[1].split("\n\nDecisions:")[0].strip().splitlines()
    assert section == ["**Affects the results**", f"- {WINDOW}", "", "**Presentation and format**", f"- {LAYOUT}",
                       "", "**Made while coding**", "- `process`: leap days -> dropped"]
    assert "Assumptions" not in ok(S, ["summary", "--run", str(make_run(spec=weather(), stage="finalize"))],
                                   capsys)["markdown"]


def test_prompts_ask_for_assumptions():
    resolve = " ".join((ROOT / "references" / "prompts" / "wca-resolve.md").read_text().split())
    assert '"assumptions": { "affects_results": [' in resolve and "`<sketch phrase or topic> -> <reading used>`" in resolve
    results = resolve.split("- `affects_results`")[1].split("- `presentation`")[0]
    for topic in ("time windows", "date ranges", "missing or flagged data", "leap days", "units", "aggregation"):
        assert topic in results, topic
    presentation = resolve.split("- `presentation`")[1].split("Write the same reading")[0]
    for topic in ("layout", "file formats", "dpi", "data source URL"):
        assert topic in presentation, topic
    assert "the field itself is required" in resolve and "ordinary change request" in resolve
    mga = " ".join((ROOT / "agents" / "curate-mga.md").read_text().split())
    assert "Write `<your folder>/assumptions.txt`" in mga and "assumptions: <number of lines" in mga



def test_long_step_descriptions_wrap_inside_the_block(make_run, capsys):
    spec = json.loads(weather().model_dump_json())
    spec["nodes"][1]["description"] = "For each variable take the values from 2025-01-01 to 2025-03-01, " * 4
    md = show(make_run(skeleton_draft={"spec": spec}), 1, capsys)["markdown"]
    block = md.split("2. process  · 3 copies\n")[1].split("\n\n")[0].splitlines()
    assert len(block) > 1 and all(l.startswith("   ") and len(l) <= 99 for l in block)


def test_every_skill_and_stage_file_asks_for_the_plain_script_form():
    rule = ("Run scripts in the plain form `uv run --quiet <plugin>/scripts/<script>.py …`: no variable "
            "assignments, no `&&` chains, no subshells.")
    files = sorted(ROOT.glob("skills/*/SKILL.md")) + sorted(ROOT.glob("references/stages/*.md"))
    assert len(files) >= 20
    for f in files:
        text = " ".join(f.read_text().split())
        if f.parent.name == "curate-doctor":  # doctor runs on the system python3, not uv
            assert ("Run the script in the plain form `python3 <plugin>/scripts/doctor.py …`: no variable "
                    "assignments, no `&&` chains, no subshells.") in text
        else:
            assert rule in text, f.relative_to(ROOT)