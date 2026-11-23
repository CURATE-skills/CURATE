"""Example step: runs only if check returned True."""
import json
import os


def main(inputs, outputs, params):
    token_set = bool(os.environ.get("API_TOKEN"))
    with open(outputs["output1"], "w") as f:
        json.dump({"parts": len(inputs["input1"]), "token_set": token_set}, f)
