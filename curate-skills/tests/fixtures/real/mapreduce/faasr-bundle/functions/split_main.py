"""Split the MapReduce input text into N contiguous, near-equal word batches.

N is the number of output batch paths (one per map rank). Words are kept in order;
the first (W mod N) batches get one extra word. Works for any vocabulary.
"""


def main(inputs: dict, outputs: dict, params: dict) -> None:
    batch_paths = outputs["output1"]
    if isinstance(batch_paths, str):
        batch_paths = [batch_paths]
    batch_paths = list(batch_paths)
    n_batches = len(batch_paths)
    if n_batches == 0:
        raise ValueError("outputs['output1'] holds no batch paths; cannot split into 0 batches")

    with open(inputs["input1"], "r", encoding="utf-8") as fh:
        words = fh.read().split()

    if not words:
        msg = f"input text {inputs['input1']} contains no words"
        print(f"split: ERROR {msg}")
        raise ValueError(msg)

    total = len(words)
    base, remainder = divmod(total, n_batches)

    start = 0
    for i, path in enumerate(batch_paths):
        size = base + (1 if i < remainder else 0)
        batch = words[start:start + size]
        start += size
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(" ".join(batch))
            fh.write("\n")
        print(f"split: batch {i + 1}/{n_batches}: {len(batch)} words -> {path}")

    if start != total:
        raise RuntimeError(f"split assigned {start} words, expected {total}")

    print(f"split: partitioned {total} words into {n_batches} batches "
          f"({base} each, +1 for the first {remainder})")
