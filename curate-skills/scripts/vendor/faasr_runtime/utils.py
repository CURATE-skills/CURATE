# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Vendored from CURATE-prototype src/faasr_agents/faasr/runtime/utils.py
# (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). Edits listed in scripts/VENDORED.md.
from .enums import FunctionStatus


def extract_function_name(function_name: str) -> str:
    return function_name.split("(")[0]


def get_s3_path(key: str) -> str:
    return key.replace("\\", "/")


def pending(status: FunctionStatus) -> bool:
    return status == FunctionStatus.PENDING


def invoked(status: FunctionStatus) -> bool:
    return status == FunctionStatus.INVOKED


def not_invoked(status: FunctionStatus) -> bool:
    return status == FunctionStatus.NOT_INVOKED


def running(status: FunctionStatus) -> bool:
    return status == FunctionStatus.RUNNING


def completed(status: FunctionStatus) -> bool:
    return status == FunctionStatus.COMPLETED


def failed(status: FunctionStatus) -> bool:
    return status == FunctionStatus.FAILED


def skipped(status: FunctionStatus) -> bool:
    return status == FunctionStatus.SKIPPED


def timed_out(status: FunctionStatus) -> bool:
    return status == FunctionStatus.TIMEOUT


def has_completed(status: FunctionStatus) -> bool:
    return completed(status) or not_invoked(status)


def has_final_state(status: FunctionStatus) -> bool:
    return (
        completed(status)
        or not_invoked(status)
        or failed(status)
        or timed_out(status)
        or skipped(status)
    )
