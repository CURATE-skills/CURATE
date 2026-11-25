"""Compare winter 2024-25 daily values of one GHCN variable with the 10-year average.

Ranked step: rank 1 = PRCP, rank 2 = TMIN, rank 3 = TMAX.
The 10-year average for each calendar day (Dec 1 .. Feb 28) is the mean over the
10 preceding winters (2014-15 .. 2023-24), skipping missing days; blank when fewer
than 5 years have a value. Feb 29 is dropped.
"""
import pandas as pd

VARIABLES = ["PRCP", "TMIN", "TMAX"]  # rank order
TARGET_WINTER = 2024  # winter starting Dec 2024
N_BASE_YEARS = 10
MIN_YEARS = 5


def _winter_start_year(dates: pd.Series) -> pd.Series:
    """Dec belongs to the winter starting that year; Jan/Feb to the winter starting the year before."""
    return dates.dt.year.where(dates.dt.month == 12, dates.dt.year - 1)


def main(inputs: dict, outputs: dict, params: dict) -> None:
    rank = int(params["rank"])
    if not 1 <= rank <= len(VARIABLES):
        raise ValueError(f"rank {rank} out of range 1..{len(VARIABLES)}")
    var = VARIABLES[rank - 1]
    in_path = inputs["input1"]
    out_path = outputs["output1"]
    print(f"Rank {rank}/{params.get('max_rank')}: variable {var}, input {in_path}")

    df = pd.read_csv(in_path)
    required = {"date", "variable", "value", "units"}
    missing_cols = required - set(df.columns)
    if missing_cols:
        raise ValueError(f"Input {in_path} lacks columns {sorted(missing_cols)}; has {list(df.columns)}")

    file_vars = set(df["variable"].dropna().astype(str).unique())
    if file_vars != {var}:
        raise ValueError(f"Input {in_path} holds variables {sorted(file_vars)}, expected only {var} for rank {rank}")
    units_vals = df["units"].dropna().astype(str).unique()
    if len(units_vals) != 1:
        raise ValueError(f"Input {in_path} has units {list(units_vals)}, expected exactly one")
    units = units_vals[0]

    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    if df["date"].duplicated().any():
        raise ValueError(f"Input {in_path} has duplicate dates")
    if df["value"].notna().sum() == 0:
        raise ValueError(f"Input {in_path} has no data for {var}")

    # Winter days only, Feb 29 dropped
    winter = df[df["date"].dt.month.isin([12, 1, 2])].copy()
    winter = winter[~((winter["date"].dt.month == 2) & (winter["date"].dt.day == 29))]
    winter["wyear"] = _winter_start_year(winter["date"])
    winter["md"] = winter["date"].dt.strftime("%m-%d")

    # Target winter 2024-25 calendar
    target_dates = pd.date_range(f"{TARGET_WINTER}-12-01", f"{TARGET_WINTER + 1}-02-28", freq="D")
    target_dates = target_dates[~((target_dates.month == 2) & (target_dates.day == 29))]
    md_order = target_dates.strftime("%m-%d")

    cur = winter[winter["wyear"] == TARGET_WINTER].set_index("md")["value"]
    if cur.notna().sum() == 0:
        raise ValueError(f"Input {in_path} has no {var} data for winter {TARGET_WINTER}-{(TARGET_WINTER + 1) % 100:02d}")

    base_years = range(TARGET_WINTER - N_BASE_YEARS, TARGET_WINTER)
    base = winter[winter["wyear"].isin(base_years)]
    if base["value"].notna().sum() == 0:
        raise ValueError(f"Input {in_path} has no {var} data for the baseline winters "
                         f"{base_years[0]}-{base_years[-1] + 1}")
    grp = base.groupby("md")["value"]
    mean = grp.mean()  # skips NaN
    n_years = grp.count()

    value = cur.reindex(md_order)
    n = n_years.reindex(md_order).fillna(0).astype(int)
    avg = mean.reindex(md_order).where(n >= MIN_YEARS).round(2)
    diff = (value - avg).round(2)

    out = pd.DataFrame({
        "date": target_dates.strftime("%Y-%m-%d"),
        "day_of_winter": range(1, len(target_dates) + 1),
        "variable": var,
        "units": units,
        "value_2024_25": value.values,
        "avg_10yr": avg.values,
        "n_years": n.values,
        "diff": diff.values,
    })
    if len(out) != 90:
        raise RuntimeError(f"Expected 90 winter days, built {len(out)}")
    out.to_csv(out_path, index=False)
    print(f"Wrote {var} ({units}): {len(out)} rows, {int(out['value_2024_25'].notna().sum())} with 2024-25 values, "
          f"{int(out['avg_10yr'].notna().sum())} with 10-yr avg, n_years range "
          f"{int(out['n_years'].min())}-{int(out['n_years'].max())} -> {out_path}")
