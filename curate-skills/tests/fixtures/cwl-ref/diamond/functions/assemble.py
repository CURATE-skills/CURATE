"""Join freeze-up and ice-off into the deliverable JSON."""
import csv, json


def main(inputs, outputs, params):
    fz = {r["winter"]: r["freeze_day"] for r in csv.DictReader(open(inputs["freeze"]))}
    io = {r["winter"]: r["iceoff_day"] for r in csv.DictReader(open(inputs["iceoff"]))}
    out = []
    for w in sorted(fz):
        a, b = fz[w], io.get(w)
        a = int(a) if a not in ("", "None") else None
        b = int(b) if b not in ("", "None", None) else None
        out.append({"winter": int(w), "freeze_day": a, "iceoff_day": b,
                    "duration_days": (b - a) if a is not None and b is not None else None})
    json.dump({"winters": out}, open(outputs["result"], "w"), indent=2)
