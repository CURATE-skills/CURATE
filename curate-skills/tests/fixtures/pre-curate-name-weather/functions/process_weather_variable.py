import os
import shutil
import tempfile

import pandas as pd

RANK_VARIABLES = {1: "PRCP", 2: "TMIN", 3: "TMAX"}
THIS_YEAR = 2025
THIS_START = pd.Timestamp("2025-01-01")
THIS_END = pd.Timestamp("2025-03-01")
BASELINE_YEARS = list(range(2015, 2025))  # previous 10 years: 2015..2024
# Baseline window extended by 30 days after Mar 1 -> Jan 1 .. Mar 31 of each year
BASELINE_END_MONTH_DAY = (3, 31)


def _curate_copy(src, dst):
    """Copy a file unless src and dst are the same path (added by migrate_catalog.py)."""
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)


def main(inputs, outputs, params):
    input1 = inputs["input1"]
    output1 = outputs["output1"]
    r = {"rank": params["rank"], "max_rank": params["max_rank"]}
    rank = int(r["rank"])
    if rank not in RANK_VARIABLES:
        msg = f"ERROR: unexpected rank {rank}; expected 1..3"
        print(msg, flush=True)
        raise ValueError(msg)
    var = RANK_VARIABLES[rank]
    in_name = input1 if "{rank}" in input1 else input1
    out_name = output1
    print(f"Rank {rank}: processing {var} from ./{in_name}", flush=True)

    tmpdir = tempfile.mkdtemp()
    local_in = os.path.join(tmpdir, "station.csv")
    _curate_copy(in_name, local_in)

    df = pd.read_csv(local_in, usecols=lambda c: c in ("DATE", var), dtype=str)
    if "DATE" not in df.columns or var not in df.columns:
        msg = f"ERROR: input CSV missing DATE or {var} column"
        print(msg, flush=True)
        raise ValueError(msg)

    df["DATE"] = pd.to_datetime(df["DATE"].str.strip(), errors="coerce")
    df[var] = pd.to_numeric(df[var].str.strip(), errors="coerce")
    df = df.dropna(subset=["DATE"])
    if var in ("TMIN", "TMAX"):
        # tenths of degrees Celsius -> degrees Celsius
        df[var] = df[var] / 10.0
    # PRCP is kept exactly as reported (no unit conversion)

    df["month"] = df["DATE"].dt.month
    df["day"] = df["DATE"].dt.day
    in_window = (df["month"] <= 2) | (
        (df["month"] == 3) & (df["day"] <= BASELINE_END_MONTH_DAY[1])
    )

    base = df[df["DATE"].dt.year.isin(BASELINE_YEARS) & in_window]
    base = base.dropna(subset=[var])
    if base.empty:
        msg = f"ERROR: no {var} data for baseline years {BASELINE_YEARS[0]}-{BASELINE_YEARS[-1]}"
        print(msg, flush=True)
        raise ValueError(msg)
    avg = base.groupby(["month", "day"])[var].mean()

    this = df[(df["DATE"] >= THIS_START) & (df["DATE"] <= THIS_END)].set_index("DATE")[var]
    if this.dropna().empty:
        msg = f"ERROR: no {var} data between {THIS_START.date()} and {THIS_END.date()}"
        print(msg, flush=True)
        raise ValueError(msg)

    dates = pd.date_range(f"{THIS_YEAR}-01-01", f"{THIS_YEAR}-03-31", freq="D")
    rows = []
    for d in dates:
        ty = this.get(d) if d <= THIS_END else None
        ty = None if ty is None or pd.isna(ty) else float(ty)
        ta = avg.get((d.month, d.day))
        ta = None if ta is None or pd.isna(ta) else float(ta)
        rows.append(
            {
                "date": d.strftime("%Y-%m-%d"),
                "variable": var,
                "this_year": ty,
                "ten_year_avg": ta,
            }
        )
    out = pd.DataFrame(rows, columns=["date", "variable", "this_year", "ten_year_avg"])

    local_out = os.path.join(tmpdir, out_name)
    out.to_csv(local_out, index=False)
    _curate_copy(local_out, out_name)
    print(f"Rank {rank}: wrote {len(out)} rows for {var} "
        f"({out['this_year'].notna().sum()} this-year values) to ./{out_name}", flush=True)
