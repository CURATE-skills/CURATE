"""Example step: reads every comparison in its folder input."""
import os


def main(inputs, outputs, params):
    names = sorted(os.listdir(inputs["input1"]))
    if not names:
        raise ValueError("no comparison files")
    with open(outputs["output1"], "wb") as f:
        f.write(("plot of " + ", ".join(names)).encode())
