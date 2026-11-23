import json


def main(inputs, outputs, params):
    """Sum partial counts for this reducer's word partition.

    Reads this rank's shuffle partition (a JSON object word -> list of partial integer
    counts), sums each list, and writes word -> total count as JSON. An empty partition
    produces an empty JSON object.
    """
    rank = params["rank"]
    with open(inputs["input1"], "r", encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        msg = f"reduce[{rank}]: partition {inputs['input1']} is empty (expected a JSON object)"
        print(msg, flush=True)
        raise ValueError(msg)

    partition = json.loads(text)
    if not isinstance(partition, dict):
        msg = f"reduce[{rank}]: partition {inputs['input1']} is not a JSON object"
        print(msg, flush=True)
        raise ValueError(msg)

    totals = {}
    for word, counts in partition.items():
        if isinstance(counts, bool) or isinstance(counts, int):
            counts = [counts]
        if not isinstance(counts, list):
            msg = f"reduce[{rank}]: counts for word {word!r} are not a list"
            print(msg, flush=True)
            raise ValueError(msg)
        total = 0
        for c in counts:
            if isinstance(c, bool) or not isinstance(c, int):
                msg = f"reduce[{rank}]: non-integer count {c!r} for word {word!r}"
                print(msg, flush=True)
                raise ValueError(msg)
            total += c
        totals[word] = total

    with open(outputs["output1"], "w", encoding="utf-8") as f:
        json.dump(totals, f, indent=2, sort_keys=True)
    print(f"reduce[{rank}]: wrote {outputs['output1']} with {len(totals)} words: {totals}", flush=True)
