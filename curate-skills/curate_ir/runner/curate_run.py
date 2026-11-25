#!/usr/bin/env python3
"""Node shim: run one CURATE function with local file paths.

  curate_run.py --code make_data.py [--entrypoint main] \
      --in params=params.json --out numbers=numbers.csv --params-json '{"n":100}'
  curate_run.py --code assemble.py --in-list ice=ice_2019.json --in-list ice=ice_2020.json ...

The function is called as main(inputs, outputs, params); inputs/outputs map logical names to
local paths:

  --in name=file        a path (str)
  --in-list name=file   (repeatable) a list of paths: a gather of a fan-out's outputs
  --in-dir name=file    (repeatable) a folder input: the files are linked into a directory
                        (_curate_in/<name>/) and the function gets that directory's path
  --out name=file       a path the function must write
  --out-list name=file  (repeatable) a list of paths the function must all write

Outputs whose logical name starts with `curate_done` are ordering markers: they are not passed
to the function; the shim writes them (empty) after it returns. Inputs whose name starts with
`curate_done` (the markers a step waits for) are not passed either.

Pegasus (condorio) stages inputs into the job's working directory and picks up outputs from
it, so plain relative names work. Standard library only, so it runs in any image that has
python3.
"""
import argparse
import importlib.util
import json
import os
import shutil
import sys

MARKER = "curate_done"
DIR_ROOT = "_curate_in"


def pairs(items, what):
    out = []
    for item in items or []:
        name, _, path = item.partition("=")
        if not name or not path:
            raise SystemExit(f"bad {what} mapping {item!r}; expected name=file")
        out.append((name, path))
    return out


def collect(single, many, what):
    """{name: path} from --x, plus {name: [paths]} from --x-list; a name may not be both."""
    out = dict(pairs(single, what))
    for name, path in pairs(many, what + " list"):
        if name in out and not isinstance(out[name], list):
            raise SystemExit(f"bad {what} list mapping {name}={path}: {name} is also a single {what}")
        out.setdefault(name, []).append(path)
    return out


def flat(mapping):
    return [p for v in mapping.values() for p in (v if isinstance(v, list) else [v])]


def link_into(src, dst_dir):
    dst = os.path.join(dst_dir, os.path.basename(src))
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--entrypoint", default="main")
    ap.add_argument("--in", dest="inputs", action="append")
    ap.add_argument("--in-list", dest="input_lists", action="append")
    ap.add_argument("--in-dir", dest="input_dirs", action="append")
    ap.add_argument("--out", dest="outputs", action="append")
    ap.add_argument("--out-list", dest="output_lists", action="append")
    ap.add_argument("--params-json", default="{}")
    a = ap.parse_args()

    inputs = collect(a.inputs, a.input_lists, "input")
    dirs = {}
    for name, path in pairs(a.input_dirs, "input dir"):
        if name in inputs:
            raise SystemExit(f"bad input dir mapping {name}={path}: {name} is also a file input")
        dirs.setdefault(name, []).append(path)
    outputs = collect(a.outputs, a.output_lists, "output")

    staged = flat(inputs) + [p for ps in dirs.values() for p in ps]
    missing = [p for p in staged if not os.path.exists(p)]
    if missing:
        raise SystemExit(f"declared inputs not present: {missing}")
    for name, paths in dirs.items():
        d = os.path.join(DIR_ROOT, name)
        os.makedirs(d, exist_ok=True)
        for p in paths:
            link_into(p, d)
        inputs[name] = d

    fn_inputs = {k: v for k, v in inputs.items() if not k.startswith(MARKER)}
    fn_outputs = {k: v for k, v in outputs.items() if not k.startswith(MARKER)}

    spec = importlib.util.spec_from_file_location("curate_fn", a.code)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, a.entrypoint)
    fn(fn_inputs, fn_outputs, json.loads(a.params_json))

    for key, path in outputs.items():
        if key.startswith(MARKER):
            for p in path if isinstance(path, list) else [path]:
                open(p, "w").close()
    not_written = [p for p in flat(outputs) if not os.path.exists(p)]
    if not_written:
        raise SystemExit(f"declared outputs not written: {not_written}")
    print(json.dumps({"curate_node": os.path.basename(a.code), "ok": True}), file=sys.stderr)


if __name__ == "__main__":
    main()
