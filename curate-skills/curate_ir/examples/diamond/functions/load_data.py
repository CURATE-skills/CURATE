"""Synthetic daily lake temperature per winter (stand-in for a data-loading node)."""
import csv, json, math, random


def main(inputs, outputs, params):
    p = json.load(open(inputs["params"]))
    rng = random.Random(p["seed"])
    with open(outputs["daily"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["winter", "day", "temp_c"])
        for winter in p["winters"]:
            shift = rng.uniform(-10, 10)
            for day in range(365):  # day 0 = Oct 1
                t = 8 * math.cos(2 * math.pi * (day - shift) / 360) - 3 + rng.gauss(0, 0.5)
                w.writerow([winter, day, round(t, 2)])
