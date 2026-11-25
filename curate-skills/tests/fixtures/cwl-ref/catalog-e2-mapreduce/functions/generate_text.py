import os
import random
import shutil
import tempfile

# Benchmark parameters from the user request: W = 5000 total words over
# M = 5 distinct words, each distributed evenly and shuffled randomly.
TOTAL_WORDS = 5000
VOCABULARY = ["cat", "dog", "bird", "horse", "pig"]


def _curate_copy(src, dst):
    """Copy a file unless src and dst are the same path (added by migrate_catalog.py)."""
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)


def main(inputs, outputs, params):
    output1 = outputs["output1"]
    total_words = TOTAL_WORDS
    vocab = list(VOCABULARY)

    if not vocab:
        print("generate_text: vocabulary is empty", flush=True)
        raise ValueError("vocabulary must not be empty")
    if total_words <= 0 or total_words % len(vocab) != 0:
        msg = (f"generate_text: total words W={total_words} must be a positive "
               f"multiple of vocabulary size {len(vocab)} for an even distribution")
        print(msg, flush=True)
        raise ValueError(msg)

    per_word = total_words // len(vocab)
    words = []
    for w in vocab:
        words.extend([w] * per_word)
    random.shuffle(words)

    tmpdir = tempfile.mkdtemp()
    local_path = os.path.join(tmpdir, "input_text.txt")
    with open(local_path, "w") as f:
        f.write(" ".join(words))
        f.write("\n")

    _curate_copy(local_path, output1)
    print(f"generate_text: wrote {len(words)} words ({per_word} each of "
              f"{', '.join(vocab)}) to ./{output1}", flush=True)
