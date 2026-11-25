import os
import shutil
import tempfile

# Number of parallel map instances (ranked successor `map` x3).
N_BATCHES = 3


def _curate_copy(src, dst):
    """Copy a file unless src and dst are the same path (added by migrate_catalog.py)."""
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)


def main(inputs, outputs, params):
    """Partition the input text into N_BATCHES contiguous, near-equal word batches.

    Tokenizes on whitespace; batch sizes differ by at most one and every word
    appears in exactly one batch, preserving order. Works for any vocabulary.
    """
    input1 = inputs["input1"]
    output1 = outputs["output1"]
    workdir = tempfile.mkdtemp(prefix="split_")
    local_in = os.path.join(workdir, "input_text.txt")

    _curate_copy(input1, local_in)
    if not os.path.exists(local_in):
        msg = f"split: input file {input1} could not be retrieved from ."
        print(msg, flush=True)
        raise FileNotFoundError(msg)

    with open(local_in, "r", encoding="utf-8") as f:
        words = f.read().split()

    if not words:
        msg = f"split: input file {input1} contains no words"
        print(msg, flush=True)
        raise ValueError(msg)

    total = len(words)
    base, extra = divmod(total, N_BATCHES)
    print(f"split: {total} words into {N_BATCHES} batches", flush=True)

    start = 0
    for i in range(1, N_BATCHES + 1):
        size = base + (1 if i <= extra else 0)
        batch = words[start:start + size]
        start += size

        local_out = os.path.join(workdir, f"text_batch_{i}.txt")
        with open(local_out, "w", encoding="utf-8") as f:
            f.write(" ".join(batch))
            f.write("\n")

        remote_name = output1[i - 1]
        _curate_copy(local_out, remote_name)
        print(f"split: wrote {remote_name} with {len(batch)} words", flush=True)

    if start != total:
        msg = f"split: word accounting mismatch ({start} written vs {total} read)"
        print(msg, flush=True)
        raise RuntimeError(msg)
