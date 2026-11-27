"""The stub-test mode is not a user setting: strict for everyone; parity (the original CURATE's
behaviour) only for a developer who sets CURATE_STUBTEST_MODE=parity."""
import json
from pathlib import Path

import spec as S
from conftest import ok
from test_gate_content import weather

PLUGIN = Path(__file__).resolve().parents[1]
STAGE_SKILLS = ("curate", "curate-skeleton", "curate-reuse", "curate-resolve", "curate-generate", "curate-emit")


def test_no_user_facing_place_offers_parity():
    for sk in STAGE_SKILLS:
        text = (PLUGIN / "skills" / sk / "SKILL.md").read_text()
        assert "stubtest-mode" not in text and "stubtest_mode" not in text and "parity" not in text, sk
    assert "stubtest_mode" not in json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())["userConfig"]
    readme = (PLUGIN / "README.md").read_text()
    settings = readme.split("## Settings")[1].split("\n## ")[0]
    assert "stubtest" not in settings.lower() and "parity" not in settings
    ref = (PLUGIN / "skills" / "function-reference" / "SKILL.md").read_text()
    assert "`parity`" not in ref


def test_gates_and_summary_name_the_mode_only_when_it_is_parity(make_run, capsys):
    code = "def main(inputs, outputs, params):\n    pass\n"
    for mode, shown in (("strict", False), ("parity", True)):
        run = make_run(spec=weather(), stage="gate3", stubtest_mode=mode,
                       mga={n: {"status": "passed"} for n in ("download", "process", "plot")})
        gate3 = ok(S, ["show", "--session", str(run), "--gate", "3"], capsys)["markdown"].splitlines()[0]
        assert gate3 == "## Gate 3 — Code  (iteration 0/5" + (", stub tests: parity" if shown else "") + ")"
        summary = ok(S, ["summary", "--session", str(run)], capsys)["markdown"]
        assert ("stub tests `parity`" in summary) is shown and ("stub tests `strict`" not in summary)
