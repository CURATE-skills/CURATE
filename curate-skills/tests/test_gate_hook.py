"""The guard hook on AskUserQuestion: a curate gate question is refused until that gate's marker
line was printed since the user's last message (fake transcripts, real hook process)."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "hooks" / "mga_guard.py"
SYSTEM_PY = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else shutil.which("python3")
MSG = "Print the full gate content from spec.py show as a message (verbatim), then ask."
MARK = "— end of Gate {} · run 20261105-101500-weather —"


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant_text(text):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool_call(name="Bash"):
    return {"type": "assistant", "message": {"role": "assistant",
                                             "content": [{"type": "tool_use", "name": name, "input": {}}]}}


def tool_result(text="{}"):
    return {"type": "user", "message": {"role": "user",
                                        "content": [{"type": "tool_result", "tool_use_id": "t", "content": text}]}}


META = {"type": "attachment", "attachment": {"type": "x"}}


def question(header="Gate 1", q="Approve this skeleton?"):
    return {"questions": [{"question": q, "header": header, "multiSelect": False,
                           "options": [{"label": "Approve"}, {"label": "Stop here"}]}]}


@pytest.fixture
def curate_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / ".curate").mkdir(parents=True)
    return proj


def ask(proj, entries, tool_input, write_transcript=True):
    transcript = proj.parent / "t.jsonl"
    if write_transcript:
        transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
    payload = {"session_id": "s", "hook_event_name": "PreToolUse", "cwd": str(proj),
               "tool_name": "AskUserQuestion", "tool_input": tool_input, "transcript_path": str(transcript)}
    proc = subprocess.run([SYSTEM_PY, str(GUARD)], input=json.dumps(payload), capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(proj)}, timeout=10)
    assert proc.returncode == 0, proc.stderr
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecisionReason"]


GATE1 = "## Gate 1 — Skeleton\n\n```text\n1. download  · 1 copy\n```\n\n" + MARK.format(1)


def test_marker_printed_in_an_earlier_message_of_this_turn_is_allowed(curate_project):
    # the gate procedure: show (tool), then content + `ask` (one message), then the question
    entries = [user("/curate-skills:curate build me a workflow"), META, tool_call(), tool_result(),
               assistant_text(GATE1 + "\nFull review: .curate/runs/r/review/gate1.md"), tool_call(), tool_result()]
    assert ask(curate_project, entries, question("Gate 1")) is None


def test_missing_marker_is_denied_with_the_instruction(curate_project):
    entries = [user("/curate-skills:curate build me a workflow"), tool_call(), tool_result(GATE1),  # only a tool result
               assistant_text("I'm moving on to the gate review file next.")]
    assert ask(curate_project, entries, question("Gate 1")) == MSG
    log = [json.loads(l) for l in (curate_project / ".curate" / "mga_guard.log").read_text().splitlines()]
    assert log[-1]["tool"] == "AskUserQuestion" and log[-1]["reason"] == MSG


def test_same_message_print_and_ask_is_denied_once_then_the_retry_passes(curate_project):
    # Printing and asking in one message: that message is not in the transcript yet, so the first
    # attempt is denied; after the denial the message (with the marker) is written and the retry passes.
    before = [user("go"), tool_call(), tool_result()]
    assert ask(curate_project, before, question("Gate 1")) == MSG
    after = before + [assistant_text(GATE1), tool_call("AskUserQuestion"), tool_result("denied: " + MSG)]
    assert ask(curate_project, after, question("Gate 1")) is None


def test_a_marker_from_before_the_users_last_message_does_not_count(curate_project):
    entries = [user("go"), assistant_text(GATE1), user("change step 2 to use numpy"), tool_call(), tool_result()]
    assert ask(curate_project, entries, question("Gate 1")) == MSG


def test_a_marker_of_another_gate_does_not_count(curate_project):
    entries = [user("approve"), assistant_text("## Gate 1 …\n\n" + MARK.format(1))]
    assert ask(curate_project, entries, question("Gate 2", "Approve the reuse decisions?")) == MSG
    assert ask(curate_project, entries + [assistant_text(MARK.format(2))], question("Gate 2")) is None


def test_tool_results_and_sidechain_messages_do_not_reset_the_turn(curate_project):
    side = {**user("subagent prompt"), "isSidechain": True}
    skill = {"type": "user", "isMeta": True,  # a skill's text injected mid-turn (as in real transcripts)
             "message": {"role": "user", "content": [{"type": "text", "text": "Base directory for this skill: …"}]}}
    entries = [user("approve"), assistant_text(MARK.format(3)), tool_call("Agent"), side, tool_result("{}"), skill]
    assert ask(curate_project, entries, question("Gate 3")) is None


@pytest.mark.parametrize("tool_input", [
    question("Deploy 1/4", "Deploy step 1/4: publish?"),
    question("Start", "Start this run?"),
    question("Teardown", "Tear down weather-1105-0930?"),
    {"questions": [{"question": "Which?", "header": "Gate", "options": []}]},  # not "Gate N"
    {},
])
def test_other_questions_pass_through(curate_project, tool_input):
    assert ask(curate_project, [user("hi")], tool_input) is None


def test_outside_a_curate_project_nothing_is_checked(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert ask(plain, [user("hi")], question("Gate 1")) is None


def test_an_unreadable_transcript_never_blocks_a_question(curate_project):
    assert ask(curate_project, [], question("Gate 1"), write_transcript=False) is None


# ── the Stop hook: asking in chat (headless, or no AskUserQuestion) ──

def show_call(gate, run=".curate/runs/r"):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": f"uv run --quiet /p/scripts/spec.py show --run {run} --gate {gate}"}}]}}


def stop(proj, entries, last_message, active=False):
    transcript = proj.parent / "t.jsonl"
    transcript.write_text("".join(json.dumps(e) + "\n" for e in entries))
    payload = {"session_id": "s", "hook_event_name": "Stop", "cwd": str(proj), "transcript_path": str(transcript),
               "last_assistant_message": last_message, "stop_hook_active": active}
    proc = subprocess.run([SYSTEM_PY, str(GUARD)], input=json.dumps(payload), capture_output=True, text=True,
                          env={"PATH": os.environ["PATH"], "CLAUDE_PROJECT_DIR": str(proj)}, timeout=10)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def test_stop_after_showing_a_gate_without_printing_it_is_blocked_once(curate_project):
    # the headless failure: show, ask, then "Gate 1, above" and the question — no content
    entries = [user("/curate-skills:curate build it"), show_call(1), tool_result(GATE1), tool_call(), tool_result()]
    out = stop(curate_project, entries, "I've drafted the workflow outline (Gate 1, above). Approve?")
    assert out == {"decision": "block", "reason": MSG}
    assert stop(curate_project, entries, "Approve?", active=True) is None  # never twice in a row


def test_stop_with_the_content_printed_is_allowed(curate_project):
    entries = [user("go"), show_call(1), tool_result(GATE1)]
    assert stop(curate_project, entries, GATE1 + "\n\nApprove this skeleton?") is None  # in the final message
    assert stop(curate_project, entries + [assistant_text(GATE1), tool_call(), tool_result()], "Approve?") is None


def test_stop_checks_the_last_gate_shown_this_turn(curate_project):
    entries = [user("approve"), show_call(1), assistant_text(MARK.format(1)), show_call(2), tool_result()]
    assert stop(curate_project, entries, "Approve the reuse decisions?")["reason"] == MSG
    old = [user("go"), show_call(1), user("approve")]  # shown before the user's last message
    assert stop(curate_project, old, "Working on reuse.") is None


def test_stop_without_a_gate_or_outside_curate_is_allowed(curate_project, tmp_path):
    assert stop(curate_project, [user("hi"), tool_call(), tool_result()], "Done.") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert stop(plain, [user("go"), show_call(1)], "Approve?") is None
