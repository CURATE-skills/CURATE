"""MapReduce shuffle step: flatten all mapper outputs Y_1..Y_N into (word, partial count)
pairs, group them by word, and partition the words across the M reducers.

Distinct words are sorted lexicographically; word k (0-based) goes to reducer rank
(k mod M) + 1. Each partition file is a JSON object word -> list of partial counts (in
mapper rank order). Works for vocabularies of any size and frequency.
"""

import json


def main(inputs: dict, outputs: dict, params: dict) -> None:
    in_paths = inputs["input1"]
    out_paths = outputs["output1"]

    if isinstance(in_paths, str):
        in_paths = [in_paths]
    if isinstance(out_paths, str):
        out_paths = [out_paths]

    if not in_paths:
        raise ValueError("shuffle: input1 (map outputs) holds no files")
    if not out_paths:
        raise ValueError("shuffle: output1 (reducer partitions) holds no paths")

    # Flatten Y_i into a list of (word, partial count) pairs.
    pairs = []
    for path in in_paths:
        with open(path, "r", encoding="utf-8") as fh:
            counts = json.load(fh)
        if not isinstance(counts, dict):
            raise ValueError(f"shuffle: {path} is not a JSON object of word -> count")
        for word, count in counts.items():
            if isinstance(count, bool) or not isinstance(count, int):
                raise ValueError(
                    f"shuffle: {path}: count for {word!r} is not an integer: {count!r}")
            pairs.append((word, count))
        print(f"shuffle: read {len(counts)} words from {path}")

    # Group by word.
    grouped = {}
    for word, count in pairs:
        grouped.setdefault(word, []).append(count)

    m = len(out_paths)
    partitions = [{} for _ in range(m)]
    for k, word in enumerate(sorted(grouped)):
        partitions[k % m][word] = grouped[word]

    for r, (path, part) in enumerate(zip(out_paths, partitions), start=1):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(part, fh, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"shuffle: partition {r}/{m}: {sorted(part)} -> {path}")

    print(f"shuffle: {len(pairs)} pairs, {len(grouped)} distinct words, "
          f"{m} partitions")
