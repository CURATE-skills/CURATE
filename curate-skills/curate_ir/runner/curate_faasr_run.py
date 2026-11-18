#!/usr/bin/env python3
"""Node shim for IR contract `faasr`: run one FaaSr-style function with local files.

  curate_faasr_run.py --code map.py --entrypoint map \
      --in input1=text_batch_2.txt --out output1=map_counts_2.json \
      --params-json '{"faasr": {"args": {...}, "rank_axis": "rank3", "max_rank": 3}, "rank3": "2"}'

Same command line as curate_run.py (the backend builds both the same way). The function is
called as FaaSr calls it, fn(**params["faasr"]["args"]), with the FaaSr API in its globals:

  faasr_get_file        copies a staged input (by its file name) to the local path asked for;
                        only this step's declared inputs can be read
  faasr_put_file        copies a local file to the job folder under the remote name; only
                        this step's declared outputs can be written
  faasr_get_folder_list the staged inputs as "<folder>/<name>" keys that start with the prefix,
                        like the S3 keys real FaaSr returns
  faasr_rank            {"rank": this copy's value of params["faasr"]["rank_axis"], "max_rank"}
  faasr_log             a line on stderr
  faasr_secret          an environment variable (the Pegasus target passes none yet)

Pegasus (condorio) stages inputs into the job folder and picks up outputs from it by file
name, so the remote folder argument is ignored. Outputs whose logical name starts with
`curate_done` are ordering markers: the shim writes them after the function returns. Every
declared output must exist at the end. Standard library only, so it runs in any image
with python3.
"""
import argparse
import importlib.util
import json
import os
import shutil
import sys

MARKER = "curate_done"


def kv(items):
    out = {}
    for item in items or []:
        name, _, path = item.partition("=")
        if not name or not path:
            raise SystemExit(f"bad mapping {item!r}; expected name=file")
        out[name] = path
    return out


def faasr_api(staged, declared_out, folder, rank, max_rank, log=sys.stderr):
    """The FaaSr runtime functions, bound to this job's declared files."""

    def _local(local_file, local_folder):
        if local_folder not in ("", ".", None):
            return os.path.join(local_folder, local_file)
        return local_file

    def faasr_get_file(local_file="", remote_folder="", remote_file="", local_folder=".", server_name=""):
        name = os.path.basename(remote_file)
        if name not in staged:
            raise FileNotFoundError(f"faasr_get_file: {name!r} is not a declared input of this step "
                                    f"(declared: {', '.join(sorted(staged)) or 'none'})")
        dst = _local(local_file, local_folder)
        if os.path.dirname(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.abspath(dst) != os.path.abspath(name):
            shutil.copyfile(name, dst)

    def faasr_put_file(local_file="", remote_folder="", remote_file="", local_folder=".", server_name=""):
        name = os.path.basename(remote_file)
        if name not in declared_out:
            raise PermissionError(f"faasr_put_file: {name!r} is not a declared output of this step "
                                  f"(declared: {', '.join(sorted(declared_out)) or 'none'})")
        src = _local(local_file, local_folder)
        if os.path.abspath(src) != os.path.abspath(name):
            shutil.copyfile(src, name)

    def faasr_get_folder_list(server_name="", prefix=""):
        keys = [f"{folder}/{n}" if folder else n for n in sorted(staged)]
        return [k for k in keys if k.startswith(prefix or "")]

    def faasr_rank():
        return {"rank": rank, "max_rank": max_rank}

    def faasr_log(msg):
        print(f"[FaaSr] {msg}", file=log, flush=True)

    def faasr_secret(name):
        if name not in os.environ:
            raise KeyError(f"faasr_secret: {name} is not set for this job")
        return os.environ[name]

    def faasr_delete_file(*a, **k):
        raise NotImplementedError("faasr_delete_file is not available in a CURATE workflow")

    return {f.__name__: f for f in (faasr_get_file, faasr_put_file, faasr_get_folder_list, faasr_rank,
                                    faasr_log, faasr_secret, faasr_delete_file)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", required=True)
    ap.add_argument("--entrypoint", required=True)
    ap.add_argument("--in", dest="inputs", action="append")
    ap.add_argument("--in-list", dest="input_lists", action="append")
    ap.add_argument("--out", dest="outputs", action="append")
    ap.add_argument("--params-json", default="{}")
    a = ap.parse_args()

    staged = set(kv(a.inputs).values())
    for item in a.input_lists or []:
        name, _, path = item.partition("=")
        if not name or not path:
            raise SystemExit(f"bad list mapping {item!r}")
        staged.add(path)
    missing = sorted(p for p in staged if not os.path.exists(p))
    if missing:
        raise SystemExit(f"declared inputs not present: {missing}")
    outputs = kv(a.outputs)
    params = json.loads(a.params_json)
    fp = params.get("faasr") or {}
    if "args" not in fp:
        raise SystemExit("params has no faasr.args: not a contract faasr node")
    axis = fp.get("rank_axis")
    rank = int(params[axis]) if axis else 1
    max_rank = int(fp.get("max_rank") or 1)

    api = faasr_api(staged, set(outputs.values()), fp["args"].get("folder", ""), rank, max_rank)
    spec = importlib.util.spec_from_file_location("curate_fn", a.code)
    mod = importlib.util.module_from_spec(spec)
    mod.__dict__.update(api)  # FaaSr injects these as globals; the code never imports them
    spec.loader.exec_module(mod)
    getattr(mod, a.entrypoint)(**fp["args"])

    for key, path in outputs.items():
        if key.startswith(MARKER):
            open(path, "w").close()
    not_written = sorted(p for p in outputs.values() if not os.path.exists(p))
    if not_written:
        raise SystemExit(f"declared outputs not uploaded with faasr_put_file: {not_written}")
    print(json.dumps({"curate_node": os.path.basename(a.code), "rank": rank, "ok": True}), file=sys.stderr)


if __name__ == "__main__":
    main()
