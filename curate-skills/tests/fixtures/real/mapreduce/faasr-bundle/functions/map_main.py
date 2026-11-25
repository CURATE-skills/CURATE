"""MapReduce map step: count word occurrences in this rank's text batch.

Tokenizes on whitespace (case-sensitive, no normalization) and writes Y_i as a JSON
object mapping word -> count. Works for vocabularies of any size and frequency.
"""

import json
from collections import Counter


def main(inputs: dict, outputs: dict, params: dict) -> None:
    rank = params.get("rank")
    max_rank = params.get("max_rank")

    in_path = inputs["input1"]
    out_path = outputs["output1"]

    with open(in_path, "r", encoding="utf-8") as fh:
        words = fh.read().split()

    counts = Counter(words)
    # Sorted keys for deterministic output.
    result = {word: counts[word] for word in sorted(counts)}

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(f"map: rank {rank}/{max_rank}: {len(words)} words, "
          f"{len(result)} distinct -> {out_path}")
    for word, count in result.items():
        print(f"map: rank {rank}: {word} = {count}")
