#!/usr/bin/env python3
"""Node shim: run one CURATE function with local file paths.

  curate_run.py --code make_data.py [--entrypoint main] \
      --in params=params.json --out numbers=numbers.csv --params-json '{"n":100}'
  curate_run.py --code assemble.py --in-list ice=ice_2019.json --in-list ice=ice_2020.json ...

--in gives the function a path (str); --in-list (repeatable) gives a list of paths,
which is how IR v0.2 gathers the outputs of a fan-out.

Pegasus (condorio) stages inputs into the job's working directory and picks up
outputs from it, so plain relative names work. Standard library only, so it
runs in any image that has python3.
"""
import argparse
import importlib.util
import json
import os
import sys


def kv(items):
    out = {}
    for item in items or []:
        name, _, path = item.partition("=")
        if not name or not path:
            raise SystemExit(f"bad mapping {item!r}; expected name=file")
        out[name] = path
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--entrypoint", default="main")
    ap.add_argument("--in", dest="inputs", action="append")
    ap.add_argument("--in-list", dest="input_lists", action="append")
    ap.add_argument("--out", dest="outputs", action="append")
    ap.add_argument("--params-json", default="{}")
    a = ap.parse_args()

    inputs, outputs = kv(a.inputs), kv(a.outputs)
    for item in a.input_lists or []:
        name, _, path = item.partition("=")
        if not name or not path or name in inputs and not isinstance(inputs[name], list):
            raise SystemExit(f"bad list mapping {item!r}")
        inputs.setdefault(name, []).append(path)
    all_inputs = [p for v in inputs.values() for p in (v if isinstance(v, list) else [v])]
    missing = [p for p in all_inputs if not os.path.exists(p)]
    if missing:
        raise SystemExit(f"declared inputs not present: {missing}")

    spec = importlib.util.spec_from_file_location("curate_fn", a.code)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fn = getattr(mod, a.entrypoint)
    fn(inputs, outputs, json.loads(a.params_json))

    not_written = [p for p in outputs.values() if not os.path.exists(p)]
    if not_written:
        raise SystemExit(f"declared outputs not written: {not_written}")
    print(json.dumps({"curate_node": os.path.basename(a.code), "ok": True}), file=sys.stderr)


if __name__ == "__main__":
    main()
