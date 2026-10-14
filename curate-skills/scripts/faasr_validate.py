# /// script
# requires-python = "==3.13.*"
# dependencies = ["FaaSr_py==0.1.13"]  # pins boto3==1.37.11 itself
# ///
# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Derived from CURATE-prototype src/faasr_agents/faasr/validate.py
# (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). See scripts/VENDORED.md.
"""Validate a FaaSr workflow JSON (stdin) with FaaSr_py's schema and DAG checks.

Prints one JSON line: {"ok": bool, "error": str|null} or {"unavailable": true}.
Run in its own uv env because FaaSr_py is heavy; emit.py falls back to
structural checks when this cannot run.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Optional


def validate_faasr_json(workflow_dict: dict) -> tuple[bool, Optional[str]]:
    """
    Validate a FaaSr workflow dict against the FaaSr schema and check for DAG issues.
    Uses FaaSr_py's validate_json (schema check) and check_dag (cycle/reachability check).
    Returns (is_valid, error_message_or_None).
    """
    from FaaSr_py.helpers.graph_functions import validate_json, check_dag

    faasr_logger = logging.getLogger("FaaSr_py.helpers.graph_functions")
    captured: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(self.format(record))

    handler = _Capture()
    faasr_logger.addHandler(handler)
    try:
        try:
            validate_json(workflow_dict)
            actions = workflow_dict.get("ActionList") or {}
            entry = workflow_dict.get("FunctionInvoke")
            only_action = next(iter(actions.values()), {}) if len(actions) == 1 else {}
            invoke_next = only_action.get("InvokeNext", [])

            # FaaSr_py's graph builder records nodes only while traversing edges,
            # so check_dag raises KeyError for the valid one-node/no-edge case.
            if len(actions) == 1 and not invoke_next:
                if entry not in actions:
                    return False, "FunctionInvoke does not refer to a valid function"
            else:
                check_dag(workflow_dict)
        except SystemExit as e:
            return False, "\n".join(captured) or f"Validation failed (exit {e.code})"
        except Exception as e:
            detail = "\n".join(captured)
            return False, detail or f"Validation failed ({type(e).__name__}: {e})"
        return True, None
    finally:
        faasr_logger.removeHandler(handler)


def main() -> int:
    try:
        import FaaSr_py.helpers.graph_functions  # noqa: F401
    except Exception as e:
        print(json.dumps({"unavailable": True, "error": f"{type(e).__name__}: {e}"}))
        return 3
    try:
        workflow = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"invalid JSON on stdin: {e}"}))
        return 2
    ok, err = validate_faasr_json(workflow)
    print(json.dumps({"ok": ok, "error": err}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
