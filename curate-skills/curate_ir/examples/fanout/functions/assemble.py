"""Gather: inputs["rows"] is a LIST of per-winter files."""
import json


def main(inputs, outputs, params):
    rows = sorted((json.load(open(p)) for p in inputs["rows"]), key=lambda w: w["winter"])
    json.dump({"winters": rows}, open(outputs["result"], "w"), indent=2)
