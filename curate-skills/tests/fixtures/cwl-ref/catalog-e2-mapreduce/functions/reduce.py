import json
import os
import shutil
import tempfile


def _curate_copy(src, dst):
    """Copy a file unless src and dst are the same path (added by migrate_catalog.py)."""
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)


def main(inputs, outputs, params):
    """Sum partial counts for this reducer's word partition, yielding Z_i.

    Reads the shuffle partition for this instance's rank (a JSON object
    word -> list of partial integer counts), sums each list, and writes
    word -> total count as JSON numbered by rank. An empty partition
    produces an empty JSON object.
    """
    input1 = inputs["input1"]
    output1 = outputs["output1"]
    r = {"rank": params["rank"], "max_rank": params["max_rank"]}
    rank = r["rank"]
    in_name = input1
    out_name = output1

    workdir = tempfile.mkdtemp(prefix=f"reduce_{rank}_")
    local_in = os.path.join(workdir, "partition.json")
    _curate_copy(in_name, local_in)
    if not os.path.exists(local_in):
        msg = f"reduce[{rank}]: could not retrieve {in_name} from ."
        print(msg, flush=True)
        raise FileNotFoundError(msg)

    with open(local_in, "r", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        msg = f"reduce[{rank}]: partition {in_name} is empty (expected a JSON object)"
        print(msg, flush=True)
        raise ValueError(msg)

    partition = json.loads(text)
    if not isinstance(partition, dict):
        msg = f"reduce[{rank}]: partition {in_name} is not a JSON object"
        print(msg, flush=True)
        raise ValueError(msg)

    totals = {}
    for word, counts in partition.items():
        if isinstance(counts, bool) or isinstance(counts, int):
            counts = [counts]
        if not isinstance(counts, list):
            msg = f"reduce[{rank}]: counts for word {word!r} are not a list"
            print(msg, flush=True)
            raise ValueError(msg)
        total = 0
        for c in counts:
            if isinstance(c, bool) or not isinstance(c, int):
                msg = f"reduce[{rank}]: non-integer count {c!r} for word {word!r}"
                print(msg, flush=True)
                raise ValueError(msg)
            total += c
        totals[word] = total

    local_out = os.path.join(workdir, "word_count.json")
    with open(local_out, "w", encoding="utf-8") as f:
        json.dump(totals, f, indent=2, sort_keys=True)
    _curate_copy(local_out, out_name)
    print(f"reduce[{rank}]: wrote {out_name} with {len(totals)} words: {totals}", flush=True)
