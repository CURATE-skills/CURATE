"""Generate the MapReduce word-count input text.

W total words drawn evenly from a vocabulary (default [cat, dog, bird, horse, pig]),
randomly shuffled, written as one line of single-space-separated words.
"""

import random

DEFAULT_W = 5000
DEFAULT_VOCABULARY = ["cat", "dog", "bird", "horse", "pig"]


def main(inputs: dict, outputs: dict, params: dict) -> None:
    total_words = int(params.get("W", DEFAULT_W))
    vocabulary = list(params.get("vocabulary", DEFAULT_VOCABULARY))
    seed = params.get("seed")

    if total_words <= 0:
        raise ValueError(f"W must be a positive integer, got {total_words}")
    if not vocabulary:
        raise ValueError("vocabulary must contain at least one word")
    if len(set(vocabulary)) != len(vocabulary):
        raise ValueError(f"vocabulary contains duplicate words: {vocabulary}")
    for word in vocabulary:
        if not isinstance(word, str) or not word or any(c.isspace() for c in word):
            raise ValueError(f"vocabulary word must be a non-empty string without whitespace: {word!r}")

    base, remainder = divmod(total_words, len(vocabulary))
    words = []
    for i, word in enumerate(vocabulary):
        count = base + (1 if i < remainder else 0)
        words.extend([word] * count)

    rng = random.Random(int(seed)) if seed is not None else random.Random()
    rng.shuffle(words)

    if len(words) != total_words:
        raise RuntimeError(f"generated {len(words)} words, expected {total_words}")

    with open(outputs["output1"], "w", encoding="utf-8") as fh:
        fh.write(" ".join(words))
        fh.write("\n")

    print(f"generate_text: wrote {len(words)} words over {len(vocabulary)} vocabulary words "
          f"({base} each, +1 for the first {remainder}) to {outputs['output1']}")
