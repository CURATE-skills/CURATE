import json
import os
import re


def main(inputs, outputs, params):
    """Flatten the per-mapper partial counts and partition words across reducers.

    inputs["input1"] is a folder holding every mapper output (map_counts_<rank>.json);
    outputs["output1"] is the list of partition paths, one per reducer rank. Groups
    partial counts by word (ordered by mapper rank), sorts distinct words
    deterministically, and assigns them round-robin to reducers 1..N. A reducer with no
    words receives an empty object.
    """
    folder = inputs["input1"]
    partition_paths = outputs["output1"]

    matches = []
    for base in os.listdir(folder):
        m = re.search(r"(\d+)\D*$", base)
        if m:
            matches.append((int(m.group(1)), base))
    matches.sort()
    if not matches:
        msg = f"shuffle: no mapper outputs found in {folder}"
        print(msg, flush=True)
        raise FileNotFoundError(msg)
    print(f"shuffle: found {len(matches)} mapper outputs: {[b for _, b in matches]}", flush=True)

    grouped = {}
    for _, base in matches:
        with open(os.path.join(folder, base), "r", encoding="utf-8") as f:
            text = f.read()
        if not text.strip():
            msg = f"shuffle: mapper output {base} is empty"
            print(msg, flush=True)
            raise ValueError(msg)
        counts = json.loads(text)
        if not isinstance(counts, dict):
            msg = f"shuffle: mapper output {base} is not a JSON object"
            print(msg, flush=True)
            raise ValueError(msg)
        for word, count in counts.items():
            if isinstance(count, bool) or not isinstance(count, int):
                msg = f"shuffle: non-integer count {count!r} for word {word!r} in {base}"
                print(msg, flush=True)
                raise ValueError(msg)
            grouped.setdefault(word, []).append(count)

    words = sorted(grouped)
    partitions = [{} for _ in partition_paths]
    for idx, word in enumerate(words):
        partitions[idx % len(partitions)][word] = grouped[word]

    for path, part in zip(partition_paths, partitions):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(part, f, indent=2, sort_keys=True)
        print(f"shuffle: wrote {path} with {len(part)} words", flush=True)
    print(f"shuffle: flattened {len(words)} distinct words from {len(matches)} mappers "
          f"into {len(partitions)} partitions", flush=True)
