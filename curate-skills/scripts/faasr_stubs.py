# Copyright (c) 2026 Nolan Cutler. MIT License; see NOTICE.
# Derived from CURATE-prototype src/faasr_agents/faasr/context_dir.py (_STUBS_CODE)
# (commit 3fd5c16092ddf97a28bc3b83088e23e7f1b0f140). See scripts/VENDORED.md.
"""Local stand-ins for the FaaSr runtime API, for stub-testing one function.

Copied into each run as <run>/stubs/faasr_stubs.py. A stub run executes with
the node's own directory (<run>/functions/<node>/) as the working directory:

  faasr_get_file   reads ./test_output, then ./test_data, then each directory in
                   $CURATE_UPSTREAM_DIRS (os.pathsep-separated; upstream nodes'
                   test_output, nearest first). A missing file is created empty,
                   as in the original, with a warning on stderr.
  faasr_put_file   writes ./test_output/<remote_file>.
"""
from __future__ import annotations

import os
import shutil
import sys


def _search_dirs() -> list[str]:
    upstream = [d for d in os.environ.get("CURATE_UPSTREAM_DIRS", "").split(os.pathsep) if d]
    return ["test_output", "test_data"] + upstream


def faasr_get_file(local_file: str, remote_folder: str, remote_file: str) -> None:
    # The node's own outputs win, then its seeded inputs, then upstream nodes'
    # REAL outputs from their stub runs.
    for d in _search_dirs():
        src = os.path.join(d, remote_file)
        if os.path.exists(src):
            shutil.copy(src, local_file)
            return
    print(f"[FaaSr stub] WARNING: {remote_file!r} not found in test_output/, test_data/ "
          f"or upstream outputs; created an empty {local_file!r}", file=sys.stderr, flush=True)
    open(local_file, "w").close()


def faasr_put_file(local_file: str, remote_folder: str, remote_file: str) -> None:
    os.makedirs("test_output", exist_ok=True)
    if os.path.exists(local_file):
        shutil.copy(local_file, os.path.join("test_output", remote_file))


def faasr_log(msg: str) -> None:
    print(f"[FaaSr] {msg}", flush=True)


def faasr_secret(name: str) -> str:
    return os.environ[name]


def faasr_rank() -> dict:
    return {
        "rank": int(os.environ.get("FAASR_RANK", "1")),
        "max_rank": int(os.environ.get("FAASR_MAX_RANK", "1")),
    }


def faasr_get_folder_list(server_name="", prefix=""):
    # Real FaaSr returns FULL S3 object keys (folder prefix included).
    names = set()
    for d in _search_dirs():
        if os.path.isdir(d):
            names.update(os.listdir(d))
    if not prefix:
        return sorted(names)
    if "/" in prefix:
        dirpart, frag = prefix.rsplit("/", 1)
        return sorted(f"{dirpart}/{n}" for n in names if n.startswith(frag))
    return sorted(f"{prefix}/{n}" for n in names)
