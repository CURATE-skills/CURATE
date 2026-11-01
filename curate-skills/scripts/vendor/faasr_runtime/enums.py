# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Vendored from CURATE-prototype src/faasr_agents/faasr/runtime/enums.py
# (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). Edits listed in scripts/VENDORED.md.
from enum import Enum


class InvocationStatus(Enum):
    PENDING = "pending"
    INVOKED = "invoked"
    NOT_INVOKED = "not_invoked"


class FunctionStatus(Enum):
    """Function execution status"""

    PENDING = "pending"
    INVOKED = "invoked"
    NOT_INVOKED = "not_invoked"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
