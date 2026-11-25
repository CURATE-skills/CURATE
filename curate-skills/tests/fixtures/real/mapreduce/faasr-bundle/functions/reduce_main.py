"""MapReduce reduce step: reducer rank i reads its shuffled partition (word -> list of
partial counts) and writes Z_i, a JSON object word -> total occurrences.

Works for vocabularies of any size and frequency; an empty partition yields {}.
"""

import json


def main(inputs: dict, outputs: dict, params: dict) -> None:
    rank = params.get("rank")
    max_rank = params.get("max_rank")
    in_path = inputs["input1"]
    out_path = outputs["output1"]

    with open(in_path, "r", encoding="utf-8") as fh:
        partition = json.load(fh)
    if not isinstance(partition, dict):
        raise ValueError(
            f"reduce[{rank}/{max_rank}]: {in_path} is not a JSON object of word -> counts")

    totals = {}
    for word, counts in partition.items():
        if isinstance(counts, bool) or isinstance(counts, int):
            counts = [counts]
        if not isinstance(counts, list):
            raise ValueError(
                f"reduce[{rank}/{max_rank}]: counts for {word!r} is not a list: {counts!r}")
        total = 0
        for c in counts:
            if isinstance(c, bool) or not isinstance(c, int):
                raise ValueError(
                    f"reduce[{rank}/{max_rank}]: partial count for {word!r} "
                    f"is not an integer: {c!r}")
            total += c
        totals[word] = total

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(totals, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(f"reduce[{rank}/{max_rank}]: {len(totals)} words -> {totals} -> {out_path}")
