"""One winter: freeze-up and ice-off days (same rules as the diamond's two steps)."""
import csv
import json


def main(inputs, outputs, params):
    winter, k = str(params["winter"]), params.get("run_days", 5)
    temps = [(int(r["day"]), float(r["temp_c"]))
             for r in csv.DictReader(open(inputs["daily"])) if r["winter"] == winter]

    freeze, count = None, 0                      # first run of k days below 0
    for day, t in temps:
        count = count + 1 if t < 0 else 0
        if count == k:
            freeze = day - k + 1
            break

    frozen, below, count, iceoff = False, 0, 0, None   # after sustained freeze, k days above 0
    for day, t in temps:
        if t < 0:
            below, count = below + 1, 0
            frozen = frozen or below >= k
        elif frozen:
            below = 0
            count += 1
            if count == k:
                iceoff = day - k + 1
                break
        else:
            below = 0

    json.dump({"winter": int(winter), "freeze_day": freeze, "iceoff_day": iceoff},
              open(outputs["ice"], "w"))
