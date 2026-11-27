"""Gate 2 states the date ranges a workflow's dated data uses, read against a recorded day
(resolved_on); cheap checks flag ranges no source can cover yet; and a step whose range has no
observed values raises, naming the range and the dates the data actually covers (the Nov 18
weather run read "this winter" as 2025-26 against a station file ending July 2025, and plotted
"0 of 90 days observed" in every panel instead of failing)."""
import csv
import datetime as dt
import importlib.util
import json
from pathlib import Path

import pytest

import scratch
import spec as S
from conftest import call, ok
from curate_models import DateRange, FunctionSpec, IOSpec, WorkflowSpec
from runstate import spec_hash
from test_spec import RESOLVED, SKELETON, state, write

FIXTURE = Path(__file__).parent / "fixtures" / "dated" / "summarize_range.py"
WINTER = {"name": "winter", "start": "2025-12-01", "end": "2026-02-28", "nodes": ["map_words", "reduce_counts"],
          "source": "GHCN-Daily station USC00351862", "phrase": "this winter"}


def weather_spec(resolved_on="2026-11-18", ranges=(WINTER,), describe=""):
    nodes = [FunctionSpec(name=n, description=describe if n == "plot" else n,
                          outputs=[IOSpec(name=f"{n}.out")]) for n in ("map_words", "reduce_counts", "plot")]
    return WorkflowSpec(name="w", entry="map_words", nodes=nodes,
                        edges=[{"from_node": "map_words", "to_node": "reduce_counts"},
                               {"from_node": "reduce_counts", "to_node": "plot"}],
                        resolved_on=resolved_on, date_ranges=[DateRange(**r) for r in ranges])


def messages(spec, severity=None):
    return [p["message"] for p in S.check_date_ranges(spec) if severity in (None, p["severity"])]


# ── recorded in the spec, shown at Gate 2 ──

def _resolved(make_run, capsys, **extra):
    run = make_run()
    sk = json.loads(json.dumps(SKELETON))
    sk["nodes"].append({"name": "plot", "description": "plot"})
    sk["edges"].append({"from_node": "reduce_counts", "to_node": "plot"})
    ok(S, ["skeleton", "--session", str(run), "--json", write(run, "skeleton.json", sk)], capsys)
    ok(S, ["approve", "--session", str(run), "--gate", "1"], capsys)
    cands = [{"node": n, "decision": "new"} for n in ("split_words", "map_words", "reduce_counts", "plot")]
    ok(S, ["apply-candidates", "--session", str(run), "--json", write(run, "candidates.json", cands)], capsys)
    ctx = ok(S, ["prompt-context", "--session", str(run), "--stage", "resolve"], capsys)
    assert ctx["today"] == dt.date.today().isoformat()
    resolved = {**json.loads(json.dumps(RESOLVED)), **extra}
    resolved["nodes"][0]["name"] = "split_words"
    resolved["entry"] = "split_words"
    for e in resolved["edges"] + resolved["data_flow"]:
        e["from_node"] = "split_words" if e["from_node"] == "split_text" else e["from_node"]
    ok(S, ["merge-resolved", "--session", str(run), "--json", write(run, "resolved.json", resolved)], capsys)
    return run


def test_gate2_shows_the_ranges_and_the_day_they_were_read(make_run, capsys):
    run = _resolved(make_run, capsys, date_ranges=[WINTER], resolved_on="2026-11-18")
    spec = WorkflowSpec.model_validate(state(run)["spec"])
    assert spec.resolved_on == "2026-11-18" and spec.date_ranges[0].name == "winter"
    md = ok(S, ["show", "--session", str(run), "--gate", "2"], capsys)["markdown"]
    block = md.split("### Assumptions")[1]
    assert "**Date ranges** (read on 2026-11-18, taken as today)" in block
    assert ('- `winter`: 2025-12-01 – 2026-02-28 ("this winter"); used by map_words, reduce_counts; '
            "source: GHCN-Daily station USC00351862") in block
    assert block.index("**Date ranges**") < block.index("**Affects the results**")  # first among them


def test_without_a_reference_date_resolve_records_today(make_run, capsys):
    run = _resolved(make_run, capsys)
    spec = WorkflowSpec.model_validate(state(run)["spec"])
    assert spec.resolved_on == dt.date.today().isoformat() and spec.date_ranges == []
    md = ok(S, ["show", "--session", str(run), "--gate", "2"], capsys)["markdown"]
    assert f"**Date ranges** (read on {spec.resolved_on}, taken as today)" in md
    assert "- none: no step downloads or filters dated data" in md


def test_a_malformed_range_is_refused():
    with pytest.raises(S.InputError, match="date_ranges"):
        S.parse_workflow_spec({**RESOLVED, "date_ranges": "winter"})


def test_a_spec_without_ranges_hashes_as_before():
    spec = WorkflowSpec(name="w", nodes=[FunctionSpec(name="a")])
    dumped = json.loads(spec.model_dump_json())
    assert "date_ranges" not in dumped and "resolved_on" not in dumped
    assert spec_hash(dumped) == spec_hash({"name": "w", "nodes": [{"name": "a"}]})
    with_ranges = json.loads(weather_spec().model_dump_json())
    assert with_ranges["resolved_on"] == "2026-11-18" and with_ranges["date_ranges"][0]["name"] == "winter"
    assert WorkflowSpec.model_validate(with_ranges) == weather_spec()


# ── cheap checks, no network ──

def test_the_nov_18_range_was_in_the_past_so_only_the_run_can_catch_it():
    assert messages(weather_spec()) == []  # read on 2026-11-18: 2025-12-01 – 2026-02-28 had happened


@pytest.mark.parametrize("start,end,expected", [
    ("2026-12-01", "2027-02-28", "entirely after 2026-11-18"),
    ("2026-09-01", "2026-12-31", "ends 2026-12-31, after 2026-11-18"),
])
def test_ranges_a_source_cannot_cover_yet_are_warnings(start, end, expected):
    got = messages(weather_spec(ranges=[{**WINTER, "start": start, "end": end}]), "warning")
    assert len(got) == 1 and expected in got[0]


def test_malformed_ranges_are_errors():
    errs = messages(weather_spec(ranges=[{**WINTER, "start": "2026-03-01"}, {**WINTER, "end": "Feb 2026"},
                                         {**WINTER, "name": "this winter", "nodes": ["nope"]}]), "error")
    assert any("starts 2026-03-01, after its end 2026-02-28" in e for e in errs)
    assert any("must be ISO dates" in e and "'Feb 2026'" in e for e in errs)
    assert any("defined twice" in e for e in errs)
    assert any("must be an identifier" in e for e in errs) and any("unknown step 'nope'" in e for e in errs)


def test_a_dated_step_without_a_range_is_a_warning_never_an_error():
    spec = weather_spec(describe="plot the winter 2025 panels")
    assert [p["severity"] for p in S.check_date_ranges(spec)] == ["warning"]
    assert "plot mentions 'winter' but no date range names it" in messages(spec)[0]
    assert all(p["severity"] != "error" for p in S.check_spec(spec) if "date range" in p["message"])


def test_specs_resolved_before_ranges_existed_are_not_checked():
    assert messages(weather_spec(resolved_on=None, ranges=(), describe="winter 2025")) == []


# ── the step: an empty range fails, naming the range and what the data covers ──

def test_spec_md_hands_the_range_and_the_rule_to_the_steps_that_use_it(tmp_path):
    wf = weather_spec()
    md = scratch.date_ranges_md(wf.nodes[0], wf)
    assert "## Date Ranges (approved at Gate 2; read on 2026-11-18)" in md
    assert '- `winter`: 2025-12-01 to 2026-02-28, both inclusive ("this winter")' in md
    assert "before any reindexing to a full calendar" in md and "raise ValueError naming the range" in md
    assert scratch.date_ranges_md(wf.nodes[2], wf) == ""  # plot does not use it
    ref = (Path(__file__).resolve().parents[1] / "skills" / "function-reference" / "SKILL.md").read_text()
    assert "**Dated data: an empty range is an error.**" in ref
    prompt = (Path(__file__).resolve().parents[1] / "references" / "prompts" / "wca-resolve.md").read_text()
    assert "Today is {today}." in prompt and '"resolved_on": "{today}"' in prompt


def _step():
    spec = importlib.util.spec_from_file_location("summarize_range", FIXTURE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _station(tmp_path, rows):
    p = tmp_path / "station.csv"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, ["DATE", "PRCP", "TMIN", "TMAX"])
        w.writeheader()
        w.writerows(rows)
    return str(p)


def test_a_step_following_the_rule_fails_the_nov_18_run(tmp_path):
    step = _step()
    station = _station(tmp_path, step.daily_rows(dt.date(1893, 1, 1), dt.date(2025, 7, 31)))  # ends July 2025
    with pytest.raises(ValueError) as e:
        step.main({"input1": station}, {"output1": str(tmp_path / "out.json")}, {})
    assert str(e.value) == "no observations of PRCP in 2025-12-01..2026-02-28; the data covers 1893-01-01..2025-07-31"
    assert not (tmp_path / "out.json").exists()  # no "0 of 90 days observed" result


def test_the_same_step_reports_a_covered_range(tmp_path):
    step = _step()
    station = _station(tmp_path, step.daily_rows(dt.date(2025, 11, 1), dt.date(2026, 3, 31)))
    step.main({"input1": station}, {"output1": str(tmp_path / "out.json")}, {})
    out = json.loads((tmp_path / "out.json").read_text())
    assert out["TMAX"] == {"observed_days": 90, "days": 90, "mean": 8.0}


def test_a_catalog_step_reused_as_is_on_a_range_is_warned():
    spec = weather_spec()
    spec = spec.model_copy(update={"nodes": [n.model_copy(update={"source": s}) for n, s in
                                             zip(spec.nodes, ("catalog", "adapt", "catalog"))]})
    got = S.check_date_ranges(spec)
    assert got == [{"severity": "warning", "message": "map_words uses a date range but is reused as is from the "
                    "catalog: it may not raise on an empty range; choose adapt to apply the rule"}]
    # plot is from the catalog too, but uses no range; reduce_counts is adapted: regenerated with the rule
