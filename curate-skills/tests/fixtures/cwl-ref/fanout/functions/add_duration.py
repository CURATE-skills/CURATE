"""One winter: add the ice duration in days."""
import json


def main(inputs, outputs, params):
    w = json.load(open(inputs["ice"]))
    a, b = w["freeze_day"], w["iceoff_day"]
    w["duration_days"] = (b - a) if a is not None and b is not None else None
    json.dump(w, open(outputs["row"], "w"))
