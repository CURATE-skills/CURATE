#!/usr/bin/env python3
"""Sum token usage from a Claude Code session transcript and its subagents.

Usage: transcript_usage.py <session-id> [--project-dir ~/.claude/projects/<encoded>]
Each assistant message can span several transcript lines; the last line per
message id is counted. Transcripts do not always store a message's final usage,
so output-token totals are a LOWER BOUND; use /cost for dollar figures.
"""
import argparse
import glob
import json
import os

KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def summarize(path):
    per_id, prompts, first, last = {}, 0, None, None
    for line in open(path):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ts = e.get("timestamp")
        first, last = first or ts, ts or last
        m = e.get("message") or {}
        if e.get("type") == "user" and isinstance(m.get("content"), str):
            prompts += 1
        if e.get("type") == "assistant" and m.get("usage") and m.get("id"):
            per_id[m["id"]] = m["usage"]  # keep the last record per message
    totals = {k: sum((u.get(k) or 0) for u in per_id.values()) for k in KEYS}
    return {"assistant_messages": len(per_id), "user_prompts": prompts, "first": first,
            "last": last, **totals}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("session")
    p.add_argument("--project-dir", default=os.path.expanduser("~/.claude/projects/-Users-laurakuo-curate"))
    a = p.parse_args()
    base = os.path.join(a.project_dir, a.session)
    out = {"main": summarize(base + ".jsonl"), "subagents": {}}
    for f in sorted(glob.glob(os.path.join(base, "subagents", "*.jsonl"))):
        out["subagents"][os.path.basename(f)[:-6]] = summarize(f)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
