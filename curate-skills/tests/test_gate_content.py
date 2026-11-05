"""Every gate shows its full content (and saves it to review/gate<N>.md) before it asks."""
import json
from pathlib import Path

import pytest

import spec as S
from conftest import ok
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


def test_gate1_lists_every_step_in_order_with_parallel_copies(make_run, capsys):
    run = make_run(skeleton_draft={"spec": json.loads(weather().model_dump_json())})
    md = show(run, 1, capsys)["markdown"]
    assert "| # | Function | Parallel copies | What it does |" in md
    rows = [l for l in md.splitlines() if l.startswith("| ") and "`" in l]
    assert [r.split("|")[2].strip() for r in rows] == ["`download`", "`process`", "`plot`"]
    assert "| 2 | `process` | 3 | One variable per copy. |" in md


def test_gate2_shows_io_packages_reuse_and_data_flow(make_run, capsys):
    run = make_run(spec=weather())
    md = show(run, 2, capsys)["markdown"]
    assert "| Function | Reuse decision | Parallel copies | Inputs | Outputs | Packages | Secrets |" in md
    assert "| `download` | [new] | 1 |" in md and "| requests |" in md
    assert "| `plot` | [catalog] ← [catalog] plot0 |" in md


def test_gate3_shows_every_functions_full_code_and_marks_kept_ones(make_run, capsys):
    run = make_run(spec=weather(), mga={"download": {"status": "passed", "attempts": 1},
                                        "process": {"status": "failed", "attempts": 2,
                                                    "last_output_tail": "ValueError: rank out of range\n"},
                                        "plot": {"status": "reused"}})
    md = show(run, 3, capsys)["markdown"]
    for name, body in (("download", "def download():\n    return 'csv'"), ("process", "def process():\n    pass"),
                       ("plot", "def plot():\n    pass")):
        assert f"```python\n{body}\n```" in md, name
    assert "### `download` — stub test: passed" in md
    assert "### `plot` — kept (not regenerated); stub test: passed (not regenerated)" in md
    assert "### `process` — stub test: failed\nLast line of its stub test: ValueError: rank out of range" in md
    assert md.index("### `download`") < md.index("### `process`") < md.index("### `plot`")  # workflow order


def test_gate4_shows_the_workflow_and_every_file_in_the_bundle(make_run, capsys, tmp_path):
    bundle = tmp_path / "bundle"
    (bundle / "functions").mkdir(parents=True)
    for f, text in {"workflow.json": "{}", "functions/download.py": "x", "README.md": "r",
                    "functions/.DS_Store": "junk"}.items():
        (bundle / f).write_text(text)
    run = make_run(spec=weather(), bundle_dir=str(bundle), validation={"ok": True, "level": "faasr_py"})
    md = show(run, 4, capsys)["markdown"]
    assert "**weather** — compare winter weather" in md
    assert "2. `process` ×3 (parallel copies) — One variable per copy." in md
    files = md.split("Files in the bundle:")[1].strip().splitlines()
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
    assert out["header"] == "Gate 5" and out["question"] == "Save to the catalog?"
    assert not list((Path(json.loads((run / "state.json").read_text())["catalog_dir"])).rglob("*.json"))  # dry run


@pytest.mark.parametrize("gate", [1, 2, 3, 4, 5])
def test_show_saves_the_same_content_to_the_runs_review_folder(make_run, capsys, gate):
    run = make_run(skeleton_draft={"spec": json.loads(weather().model_dump_json())}, spec=weather(),
                   stage="finalize", validation={"ok": True, "level": "faasr_py"}, bundle_dir="/b")
    out = show(run, gate, capsys)
    review = run / "review" / f"gate{gate}.md"
    assert review.read_text() == out["markdown"] + "\n" and out["review_file"] == str(review)
    assert out["review_line"] == f"Full review: {review}"


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
