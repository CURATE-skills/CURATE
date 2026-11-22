import random

# Benchmark parameters from the user request: W = 5000 total words over
# M = 5 distinct words, each distributed evenly and shuffled randomly.
TOTAL_WORDS = 5000
VOCABULARY = ["cat", "dog", "bird", "horse", "pig"]


def main(inputs, outputs, params):
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

    with open(outputs["output1"], "w") as f:
        f.write(" ".join(words))
        f.write("\n")
    print(f"generate_text: wrote {len(words)} words ({per_word} each of "
          f"{', '.join(vocab)}) to {outputs['output1']}", flush=True)
