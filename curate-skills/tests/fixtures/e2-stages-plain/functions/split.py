def main(inputs, outputs, params):
    """Partition the input text into one contiguous, near-equal word batch per map rank.

    outputs["output1"] is the list of batch paths (one per rank of the ranked `map` step).
    Tokenizes on whitespace; batch sizes differ by at most one and every word appears in
    exactly one batch, preserving order. Works for any vocabulary.
    """
    batches = outputs["output1"]
    with open(inputs["input1"], "r", encoding="utf-8") as f:
        words = f.read().split()

    if not words:
        msg = f"split: input file {inputs['input1']} contains no words"
        print(msg, flush=True)
        raise ValueError(msg)

    total = len(words)
    base, extra = divmod(total, len(batches))
    print(f"split: {total} words into {len(batches)} batches", flush=True)

    start = 0
    for i, path in enumerate(batches, 1):
        size = base + (1 if i <= extra else 0)
        batch = words[start:start + size]
        start += size
        with open(path, "w", encoding="utf-8") as f:
            f.write(" ".join(batch))
            f.write("\n")
        print(f"split: wrote {path} with {len(batch)} words", flush=True)

    if start != total:
        msg = f"split: word accounting mismatch ({start} written vs {total} read)"
        print(msg, flush=True)
        raise RuntimeError(msg)
