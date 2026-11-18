"""Ice-off: after a sustained freeze (run_days below 0), first run of run_days above 0."""
import csv


def main(inputs, outputs, params):
    by = {}
    for r in csv.DictReader(open(inputs["daily"])):
        by.setdefault(r["winter"], []).append((int(r["day"]), float(r["temp_c"])))
    k = params.get("run_days", 5)
    with open(outputs["iceoff"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["winter", "iceoff_day"])
        for winter, temps in sorted(by.items()):
            frozen, below, count, ans = False, 0, 0, None
            for day, t in temps:
                if t < 0:
                    below, count = below + 1, 0
                    frozen = frozen or below >= k
                elif frozen:
                    below = 0
                    count += 1
                    if count == k:
                        ans = day - k + 1
                        break
                else:
                    below = 0
            w.writerow([winter, ans])
