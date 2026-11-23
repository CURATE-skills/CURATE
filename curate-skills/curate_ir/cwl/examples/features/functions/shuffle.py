"""Example step: reads a folder, writes one file per rank5 value."""
import os


def main(inputs, outputs, params):
    names = sorted(os.listdir(inputs["input1"]))
    if not names:
        raise ValueError("empty folder")
    for i, path in enumerate(outputs["output1"]):
        with open(path, "w") as f:
            f.write(f"part {i + 1} of {len(names)} files\n")
