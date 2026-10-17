#!/usr/bin/env python3
"""Line counts for curate-skills vs the CURATE prototype.

Usage: python3 experiments/tools/line_counts.py [--json] [--plugin DIR] [--compare DIR]

--plugin counts another checkout (default: this repo's curate-skills); --compare prints
bucket totals for both, e.g. --plugin ../curate-v1/curate-skills --compare curate-skills.

For every plugin file: total, blank, comment and code lines. For files derived from
the prototype, code (or prose) lines are split into "copied" and "added": the file's
non-blank, non-comment lines are matched in order against the concatenated cited
source files with difflib.SequenceMatcher (whitespace-trimmed lines); lines inside
matching blocks count as copied, the rest as added. Prompt text in the original lives
in Python string literals, so for Markdown the original's '{{'/'}}' are unescaped
first; re-wrapped prose does not match line for line, so prose "copied" is a lower bound.
"""
import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "curate-skills"
ORIG = ROOT / "ref" / "curate" / "src" / "faasr_agents"

VENDORED = {
    "scripts/curate_models.py": ["models.py"],
    "scripts/emit.py": ["faasr/emit.py", "agents/wda.py"],
    "scripts/faasr_validate.py": ["faasr/validate.py"],
    "scripts/catalog.py": ["catalog/store.py", "catalog/workflow_store.py", "orchestrator.py", "agents/fca.py"],
    "scripts/faasr_stubs.py": ["faasr/context_dir.py"],
    "scripts/stubtest.py": ["agents/fga.py", "faasr/context_dir.py"],
}
PORTED = {
    "scripts/spec.py": ["agents/wca.py", "agents/fca.py", "faasr/context_dir.py", "orchestrator.py"],
    "scripts/scratch.py": ["faasr/context_dir.py", "agents/fga.py"],
}
NEW = ["scripts/runstate.py", "hooks/mga_guard.py", "hooks/hooks.json"]  # missing files are skipped
PROMPT_SOURCES = ["prompts/wca_prompts.py", "prompts/fca_prompts.py", "prompts/wda_prompts.py",
                  "agents/fga.py", "faasr/context_dir.py"]
# The prototype code that the plugin's pieces replace, for comparison.
ORIGINAL_GROUPS = {
    "vendored parts (emit, validate, catalog, models, stubs)": [
        "faasr/emit.py", "faasr/validate.py", "catalog/store.py", "catalog/workflow_store.py", "models.py"],
    "orchestration + agents (graph, state, gates, WCA/FCA/FGA/WDA, LLM, prompts)": [
        "orchestrator.py", "state.py", "llm.py", "agents/wca.py", "agents/fca.py", "agents/fga.py",
        "agents/wda.py", "faasr/context_dir.py", "prompts/wca_prompts.py", "prompts/fca_prompts.py",
        "prompts/wda_prompts.py"],
    "CLI + TUI": ["cli.py", "tui"],
    "deploy + runtime monitor (not in v1)": ["deploy", "faasr/runtime", "faasr/artifacts.py"],
    "other (pricing, export, temp paths)": ["pricing.py", "export.py", "temp_paths.py"],
}


def classify(path: Path):
    lines = path.read_text().splitlines()
    md = path.suffix == ".md"
    blank = sum(1 for l in lines if not l.strip())
    if md:
        comment = 0
    elif path.suffix == ".json":
        comment = 0
    else:
        comment = sum(1 for l in lines if l.strip().startswith("#"))
    body = [l.strip() for l in lines if l.strip() and (md or not l.strip().startswith("#"))]
    return {"total": len(lines), "blank": blank, "comment": comment, "body": body}


def source_lines(rel_paths, markdown=False):
    out = []
    for rel in rel_paths:
        p = ORIG / rel
        files = sorted(p.rglob("*.py")) if p.is_dir() else [p]
        for f in files:
            for l in f.read_text().splitlines():
                s = l.strip()
                if not s or (not markdown and s.startswith("#")):
                    continue
                if markdown:
                    s = s.replace("{{", "{").replace("}}", "}")
                out.append(s)
    return out


def copied(body, src):
    m = difflib.SequenceMatcher(None, body, src, autojunk=False)
    return sum(b.size for b in m.get_matching_blocks())


def row(rel, sources=None, markdown=False):
    if not (PLUGIN / rel).exists():
        return None
    c = classify(PLUGIN / rel)
    r = {"file": rel, "total": c["total"], "blank": c["blank"], "comment": c["comment"],
         "code": len(c["body"])}
    if sources:
        r["copied"] = copied(c["body"], source_lines(sources, markdown))
        r["added"] = r["code"] - r["copied"]
    return r


def count_original(rel_paths):
    total = code = 0
    for rel in rel_paths:
        p = ORIG / rel
        for f in (sorted(p.rglob("*.py")) if p.is_dir() else [p]):
            ls = f.read_text().splitlines()
            total += len(ls)
            code += sum(1 for l in ls if l.strip() and not l.strip().startswith("#"))
    return {"total": total, "code": code}


def build_report():
    text_files = sorted(list((PLUGIN / "skills").rglob("*.md")) + list((PLUGIN / "references").rglob("*.md"))
                        + [PLUGIN / "agents" / "curate-mga.md"])
    rows = lambda xs: [r for r in xs if r]
    return {
        "vendored": rows(row(k, v) for k, v in VENDORED.items()),
        "ported_new": rows([row(k, v) for k, v in PORTED.items()] + [row(k) for k in NEW]),
        "tests": rows(row(str(p.relative_to(PLUGIN))) for p in sorted((PLUGIN / "tests").glob("*.py"))),
        "skills_prompts": rows(row(str(p.relative_to(PLUGIN)), PROMPT_SOURCES, markdown=True)
                               for p in text_files),
        "original": {k: count_original(v) for k, v in ORIGINAL_GROUPS.items()},
        "original_all": count_original(["."]),
    }


def totals(report):
    out = {}
    for section in ("vendored", "ported_new", "tests", "skills_prompts"):
        out[section] = {k: sum(r.get(k, 0) for r in report[section])
                        for k in ("total", "code", "copied", "added")}
    out["python_total"] = out["vendored"]["total"] + out["ported_new"]["total"]
    out["all_total"] = out["python_total"] + out["tests"]["total"] + out["skills_prompts"]["total"]
    return out


def main():
    global PLUGIN
    args = sys.argv[1:]
    if "--plugin" in args:
        PLUGIN = Path(args[args.index("--plugin") + 1]).resolve()
    if "--compare" in args:
        other = Path(args[args.index("--compare") + 1]).resolve()
        first, PLUGIN_A = totals(build_report()), PLUGIN
        PLUGIN = other
        second = totals(build_report())
        print(f"bucket | {PLUGIN_A} | {other}")
        for k in ("vendored", "ported_new", "tests", "skills_prompts"):
            print(f"{k} (total lines) | {first[k]['total']} | {second[k]['total']}")
        for k in ("python_total", "all_total"):
            print(f"{k} | {first[k]} | {second[k]}")
        return
    report = build_report()
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
        return
    for section in ("vendored", "ported_new", "tests", "skills_prompts"):
        rows = report[section]
        print(f"\n## {section}")
        has_split = any("copied" in r for r in rows)
        print("file | total | blank | comment | code" + (" | copied | added" if has_split else ""))
        for r in rows:
            extra = f" | {r.get('copied', '—')} | {r.get('added', '—')}" if has_split else ""
            print(f"{r['file']} | {r['total']} | {r['blank']} | {r['comment']} | {r['code']}{extra}")
        tot = {k: sum(r.get(k, 0) for r in rows) for k in ("total", "blank", "comment", "code", "copied", "added")}
        print("TOTAL | " + " | ".join(str(tot[k]) for k in ("total", "blank", "comment", "code"))
              + (f" | {tot['copied']} | {tot['added']}" if has_split else ""))
    print("\n## original prototype (src/faasr_agents)")
    for k, v in report["original"].items():
        print(f"{k} | {v['total']} | {v['code']}")
    print(f"ALL | {report['original_all']['total']} | {report['original_all']['code']}")


if __name__ == "__main__":
    main()
