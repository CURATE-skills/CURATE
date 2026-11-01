"""Secret-leak checks shared by deploy.py and monitor.py.

Two kinds of hit, reported by *label* only (never the value):
  - by value: the value of a known secret env var (or a token the script fetched itself,
    e.g. from `gh auth token`) appears in the text — the check emit.py already does;
  - by pattern: something shaped like a GitHub token or an AWS key appears, whoever it
    belongs to.
`redact` replaces both with `[REDACTED:<label>]` (for logs that are saved or shown).
Standard library only.
"""
from __future__ import annotations

import os
import re

# Secret env vars CURATE's deploy and runtime read (deploy/*.py, faasr/runtime/*.py).
SECRET_ENV_NAMES = ("GH_PAT", "S3_AccessKey", "S3_SecretKey", "AWS_AccessKey", "AWS_SecretKey",
                    "OW_APIkey", "GCP_SecretKey", "SLURM_Token", "GITHUB_TOKEN", "GH_TOKEN")
MIN_VALUE_LEN = 6  # as emit.find_leaked_secrets

PATTERNS = {
    "github_token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b"),
    "github_pat": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "aws_secret_access_key": re.compile(
        r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{30,}"),
}


def _values(names, extra_values: dict | None) -> dict[str, str]:
    vals = {n: os.environ.get(n, "") for n in names}
    vals.update(extra_values or {})
    return {k: v for k, v in vals.items() if v and len(v) >= MIN_VALUE_LEN}


def find_leaks(text: str, names=SECRET_ENV_NAMES, extra_values: dict | None = None) -> list[str]:
    """Labels of secrets found in text: env names (by value), extra labels, pattern names."""
    hits = [label for label, v in _values(names, extra_values).items() if v in text]
    hits += [label for label, rx in PATTERNS.items() if rx.search(text)]
    return sorted(set(hits))


def redact(text: str, names=SECRET_ENV_NAMES, extra_values: dict | None = None) -> str:
    # Longest values first, so a value containing another is replaced whole.
    for label, v in sorted(_values(names, extra_values).items(), key=lambda kv: -len(kv[1])):
        text = text.replace(v, f"[REDACTED:{label}]")
    for label, rx in PATTERNS.items():
        text = rx.sub(f"[REDACTED:{label}]", text)
    return text
