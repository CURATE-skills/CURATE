"""Example step: its return value decides whether `report` runs."""
import os


def main(inputs, outputs, params):
    return all(os.path.getsize(p) > 0 for p in inputs["input1"])
