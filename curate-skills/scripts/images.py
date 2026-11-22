"""Container image addresses for the Pegasus target: one check, used by Gate 4 (a per-run image),
doctor --pegasus (CURATE_PEGASUS_IMAGE) and the Pegasus preflight.

  check_image(addr, runtime, host=None, runner=hostcmd.run) -> {"ok", "image", "error"}

Runtime-aware:
- docker:       docker:///<repo>[:tag][@sha256:…] (docker://<repo> is normalized to docker:///<repo>,
                as Pegasus needs), or file:///<abs path to a `docker save` tar> (.tar, .tar.gz, .tgz)
- singularity:  the same docker addresses, plus library://…, shub://…, oras://… (Apptainer
                sources, e.g. for OSPool), or file:///<abs path to a .sif>
A file:// image must exist and be readable: here, or on the ssh host (a read-only `test`).
Anything else is refused with a message saying what is accepted. Standard library only.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import hostcmd

RUNTIMES = ("docker", "singularity")
# registry[:port]/path/name[:tag][@sha256:digest] (lower-case path, as Docker requires)
DOCKER_REF = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[0-9]+)?(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
                        r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[0-9a-f]{64})?$")
APPTAINER_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
FILE_SUFFIXES = {"docker": (".tar", ".tar.gz", ".tgz"), "singularity": (".sif",)}


def accepted(runtime: str) -> str:
    base = "docker:///<repo>:<tag> (docker://<repo>:<tag> is accepted too)"
    if runtime == "singularity":
        return base + ", library://…, shub://…, oras://…, or file:///<absolute path>.sif"
    return base + ", or file:///<absolute path to a `docker save` .tar>"


def _refuse(addr, runtime, why):
    return {"ok": False, "image": None,
            "error": f"image {addr!r} {why}. With runtime {runtime}, use {accepted(runtime)}."}


def check_image(addr, runtime: str, host: str | None = None, runner=hostcmd.run) -> dict:
    addr = (addr or "").strip()
    if runtime not in RUNTIMES:
        return {"ok": False, "image": None, "error": f"runtime {runtime!r} is not docker or singularity"}
    if not addr:
        return _refuse(addr, runtime, "is empty")
    if addr.startswith("docker://"):
        ref = addr[len("docker://"):].lstrip("/")
        if not DOCKER_REF.match(ref):
            return _refuse(addr, runtime, f"has no valid image reference ({ref!r})")
        return {"ok": True, "image": "docker:///" + ref, "error": None}
    for scheme in ("library://", "shub://", "oras://"):
        if addr.startswith(scheme):
            if runtime != "singularity":
                return _refuse(addr, runtime, f"is an Apptainer source ({scheme}), which needs runtime singularity")
            ref = addr[len(scheme):]
            if not ref or not APPTAINER_REF.match(ref):
                return _refuse(addr, runtime, f"has no valid {scheme} reference")
            return {"ok": True, "image": addr, "error": None}
    if addr.startswith("file://"):
        path = addr[len("file://"):]
        if not path.startswith("/"):
            return _refuse(addr, runtime, "is not an absolute file:/// path")
        if not path.endswith(FILE_SUFFIXES[runtime]):
            return _refuse(addr, runtime, f"is not a {' / '.join(FILE_SUFFIXES[runtime])} file")
        if host is None:
            ok = Path(path).is_file() and os.access(path, os.R_OK)
            where = "this machine"
        else:
            code, _, _ = hostcmd.on(host, f"test -f {shlex.quote(path)} && test -r {shlex.quote(path)}",
                                    runner=runner, timeout=60)
            ok, where = code == 0, host
        if not ok:
            return {"ok": False, "image": None, "error": f"image file {path} does not exist or is not readable "
                                                         f"on {where}"}
        return {"ok": True, "image": "file://" + path, "error": None}
    return _refuse(addr, runtime, "has no accepted scheme")
