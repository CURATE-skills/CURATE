import json
import os
import shutil
import tempfile
from collections import Counter


def _curate_copy(src, dst):
    """Copy a file unless src and dst are the same path (added by migrate_catalog.py)."""
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)


def main(inputs, outputs, params):
    """Count word occurrences in this instance's text batch (ranked map step).

    Reads text_batch_{rank}.txt, tokenizes on whitespace (same as split),
    counts every distinct word with no hard-coded vocabulary, and writes the
    partial count dictionary Y_rank (word -> count) as JSON.
    """
    input1 = inputs["input1"]
    output1 = outputs["output1"]
    r = {"rank": params["rank"], "max_rank": params["max_rank"]}
    rank = int(r["rank"])
    in_name = input1
    out_name = output1
    print(f"map: rank {rank}/{r.get('max_rank')} reading {in_name}", flush=True)

    workdir = tempfile.mkdtemp(prefix=f"map_{rank}_")
    local_in = os.path.join(workdir, "text_batch.txt")
    local_out = os.path.join(workdir, "map_counts.json")

    _curate_copy(in_name, local_in)
    if not os.path.exists(local_in):
        msg = f"map: input file {in_name} could not be retrieved from ."
        print(msg, flush=True)
        raise FileNotFoundError(msg)

    with open(local_in, "r", encoding="utf-8") as f:
        words = f.read().split()

    # An empty batch is legitimate (fewer words than mappers): emit {}.
    counts = Counter(words)
    result = {word: counts[word] for word in sorted(counts)}

    with open(local_out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True)

    _curate_copy(local_out, out_name)
    print(f"map: rank {rank} counted {len(words)} words, "
        f"{len(result)} distinct, wrote {out_name}", flush=True)
