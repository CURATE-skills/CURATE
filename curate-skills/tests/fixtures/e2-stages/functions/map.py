import json
from collections import Counter


def main(inputs, outputs, params):
    """Count word occurrences in this rank's text batch (ranked map step).

    Tokenizes on whitespace (same as split), counts every distinct word with no
    hard-coded vocabulary, and writes the partial count dictionary (word -> count)
    as JSON.
    """
    rank = params["rank"]
    print(f"map: rank {rank}/{params['max_rank']} reading {inputs['input1']}", flush=True)

    with open(inputs["input1"], "r", encoding="utf-8") as f:
        words = f.read().split()

    # An empty batch is legitimate (fewer words than mappers): emit {}.
    counts = Counter(words)
    result = {word: counts[word] for word in sorted(counts)}

    with open(outputs["output1"], "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True)
    print(f"map: rank {rank} counted {len(words)} words, {len(result)} distinct", flush=True)
