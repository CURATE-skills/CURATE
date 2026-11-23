"""Every script the skills run with `uv run` gets ONLY the packages in its own `# /// script` header
(the test suite installs everything from tests/requirements.txt, so it would not notice a missing
one). For each runnable script: follow its imports through the plugin's own modules (also imports
inside functions, which run later), then, in an environment built from that header alone
(`uv run --no-project`), import every plugin module it can reach and every third-party package
they use. Imports guarded by `try: … except ImportError` are optional and skipped.
"""
import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1]
ROOTS = [PLUGIN / "scripts", PLUGIN / "curate_ir"]  # where the scripts' sys.path inserts point
# Scripts the skills run with `uv run` (files with a __main__ block). The curate_ir/ tools that run
# on the Pegasus host with plain python3 (validate.py, expand.py, backends/, runner/) are not uv
# scripts and are not checked here.
RUNNABLE = sorted([p for p in (PLUGIN / "scripts").glob("*.py") if '__name__ == "__main__"' in p.read_text()]
                  + [p for p in (PLUGIN / "curate_ir" / "cwl").glob("*.py")
                     if '__name__ == "__main__"' in p.read_text()])
STDLIB = set(sys.stdlib_module_names)


def header_deps(path: Path) -> list[str]:
    text = path.read_text()
    m = re.search(r"^# /// script\n(.*?)^# ///$", text, re.S | re.M)
    if not m:
        return []
    block = "\n".join(line[2:] if line.startswith("# ") else line[1:] for line in m.group(1).splitlines())
    dm = re.search(r"dependencies\s*=\s*\[(.*?)\]", block, re.S)
    return re.findall(r'"([^"]+)"', dm.group(1)) if dm else []


def _optional(node, parents) -> bool:
    """Inside a `try` whose handlers catch ImportError (or a broader exception)."""
    for p in parents.get(node, []):
        if isinstance(p, ast.Try):
            for h in p.handlers:
                names = [] if h.type is None else [h.type] if not isinstance(h.type, ast.Tuple) else h.type.elts
                if h.type is None or any(getattr(n, "id", None) in ("ImportError", "ModuleNotFoundError",
                                                                    "Exception", "BaseException") for n in names):
                    return True
    return False


def imports(path: Path) -> list[tuple[str, bool]]:
    """(dotted module name, optional) for every import in the file, at any depth."""
    tree = ast.parse(path.read_text())
    parents: dict = {}

    def walk(node, chain):
        inner = chain + [node] if isinstance(node, ast.Try) else chain
        for child in ast.iter_child_nodes(node):
            parents[child] = inner
            walk(child, inner)
    walk(tree, [])
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, _optional(node, parents)) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.append((node.module, _optional(node, parents)))
    return out


def local_file(name: str) -> Path | None:
    parts = name.split(".")
    for root in ROOTS:
        for cand in (root.joinpath(*parts).with_suffix(".py"), root.joinpath(*parts) / "__init__.py"):
            if cand.is_file():
                return cand
    return None


def closure(script: Path) -> tuple[list[str], list[str]]:
    """(plugin modules reachable from the script, third-party top-level packages they need)."""
    seen, local, third = set(), [], set()
    todo = [(script, None)]
    while todo:
        path, name = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        if name:
            local.append(name)
        for mod, optional in imports(path):
            top = mod.split(".")[0]
            f = local_file(mod) or (local_file(top) if top != mod else None)
            if f is not None and optional:
                continue  # guarded by try/except: run elsewhere with its own packages (doctor's S3 probe)
            if f is not None:
                todo.append((f, mod if local_file(mod) else top))
                if local_file(top) and top != mod:  # a package's __init__ runs first
                    todo.append((local_file(top), top))
            elif top not in STDLIB and not optional and top != "__future__":
                third.add(top)
    return sorted(set(local)), sorted(third)


def clean_env() -> dict:
    """No trace of the test suite's own environment (VIRTUAL_ENV, uv settings), so the script gets only
    what its header lists."""
    import os
    return {k: v for k, v in os.environ.items()
            if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "CONDA_PREFIX") and not k.startswith("UV_")}


def header_only(deps: list[str], probe: Path) -> list[str]:
    return ["uv", "run", "--quiet", "--isolated", "--no-project", "--python", "3.13",
            *[x for d in deps for x in ("--with", d)], "python", "-I", str(probe)]


def test_the_probe_sees_only_the_header(tmp_path):
    """Guard for this test itself: a package that is in tests/requirements.txt but not in the header
    must NOT be importable in the probe's environment."""
    probe = tmp_path / "p.py"
    probe.write_text("import yaml\n")
    r = subprocess.run(header_only(["pydantic==2.11.7"], probe), capture_output=True, text=True, timeout=600,
                       cwd=tmp_path, env=clean_env())
    assert r.returncode != 0 and "No module named 'yaml'" in r.stderr


def test_runnable_scripts_are_found():
    names = {p.name for p in RUNNABLE}
    assert {"emit.py", "spec.py", "catalog.py", "deploy.py", "pegasus_deploy.py", "monitor.py", "doctor.py",
            "cwl_faasr.py", "cwl_pegasus.py", "validate.py"} <= names


def test_the_scan_follows_lazy_imports():
    """The bug this guards against: emit.py imports spec_cwl inside a function; spec_cwl needs yaml."""
    local, third = closure(PLUGIN / "scripts" / "emit.py")
    assert "spec_cwl" in local and "yaml" in third and "cwl_utils" in third


@pytest.mark.parametrize("script", RUNNABLE, ids=lambda p: str(p.relative_to(PLUGIN)))
def test_header_covers_every_import(script, tmp_path):
    local, third = closure(script)
    deps = header_deps(script)
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import importlib, sys\n"
        + "".join(f"sys.path.insert(0, {str(r)!r})\n" for r in reversed(ROOTS))
        + f"mods = {local + third!r}\n"
        "bad = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m)\n"
        "    except ImportError as e:\n"
        "        bad.append(f'{m}: {e}')\n"
        "print('\\n'.join(bad))\n"
        "sys.exit(1 if bad else 0)\n")
    r = subprocess.run(header_only(deps, probe), capture_output=True, text=True, timeout=600, cwd=tmp_path,
                       env=clean_env())
    assert r.returncode == 0, (f"{script.name}'s header {deps} misses what its imports need:\n{r.stdout}"
                               f"{r.stderr[-1500:]}")


def test_doctors_s3_probe_environment_has_what_deploy_needs(tmp_path):
    """doctor.py runs on the system python3 (standard library only) and runs its S3 probe as
    `uv run --no-project --with FaaSr_py python doctor.py _s3probe`, which imports deploy.py there."""
    sys.path.insert(0, str(PLUGIN / "scripts"))
    import doctor
    local, third = closure(PLUGIN / "scripts" / "doctor.py")
    assert third == [], f"doctor.py runs on plain python3 but needs {third}"
    probe = tmp_path / "probe.py"
    probe.write_text(f"import sys\nsys.path.insert(0, {str(PLUGIN / 'scripts')!r})\n"
                     "import deploy, boto3\nassert callable(deploy.boto3_s3) and callable(deploy.s3_keys)\n")
    r = subprocess.run(header_only([doctor.FAASR_PY], probe), capture_output=True, text=True, timeout=600,
                       cwd=tmp_path, env=clean_env())
    assert r.returncode == 0, r.stderr[-1500:]
