"""Example step: a list of paths, rank order."""
import json


def main(inputs, outputs, params):
    rows = {}
    for path in inputs["input1"]:
        with open(path) as f:
            rows[f.readline().strip().split(",")[1]] = sum(1 for _ in f)
    with open(outputs["output1"], "w") as f:
        json.dump(rows, f)
