"""First day of the first run of `run_days` days below threshold."""
import csv


def first_run(temps, below, run_days):
    count = 0
    for day, t in temps:
        count = count + 1 if (t < 0) == below else 0
        if count == run_days:
            return day - run_days + 1
    return None


def main(inputs, outputs, params):
    by = {}
    for r in csv.DictReader(open(inputs["daily"])):
        by.setdefault(r["winter"], []).append((int(r["day"]), float(r["temp_c"])))
    with open(outputs["freeze"], "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["winter", "freeze_day"])
        for winter, temps in sorted(by.items()):
            w.writerow([winter, first_run(temps, True, params.get("run_days", 5))])
