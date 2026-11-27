"""A step written to function-reference's rule for dated data (tests/test_date_ranges.py): filter a
GHCN-Daily style CSV (DATE,PRCP,TMIN,TMAX; empty = missing) to the date range spec.md gives, count
the observed values before any reindexing, raise if there are none, then report N of M days."""
import csv
import json
from datetime import date, timedelta

START, END = date(2025, 12, 1), date(2026, 2, 28)  # spec.md's Date Ranges: `winter`, both inclusive
VARIABLES = ["PRCP", "TMIN", "TMAX"]


def main(inputs, outputs, params):
    with open(inputs["input1"]) as f:
        rows = list(csv.DictReader(f))
    dates = [date.fromisoformat(r["DATE"]) for r in rows]
    first, last = (min(dates), max(dates)) if dates else (None, None)
    in_range = [r for r, d in zip(rows, dates) if START <= d <= END]
    days = (END - START).days + 1
    result = {}
    for var in VARIABLES:
        observed = [float(r[var]) for r in in_range if r.get(var, "").strip()]
        if not observed:  # counted before any reindexing to the full calendar
            raise ValueError(f"no observations of {var} in {START}..{END}; the data covers "
                             + (f"{first}..{last}" if first else "no dates at all"))
        result[var] = {"observed_days": len(observed), "days": days, "mean": sum(observed) / len(observed)}
    with open(outputs["output1"], "w") as f:
        json.dump(result, f)


def daily_rows(start: date, end: date) -> list[dict]:
    """Test data: one observed row per day."""
    out, d = [], start
    while d <= end:
        out.append({"DATE": d.isoformat(), "PRCP": "1.0", "TMIN": "-2.0", "TMAX": "8.0"})
        d += timedelta(days=1)
    return out
