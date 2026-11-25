"""Plot winter 2024-25 vs the 10-year average for PRCP, TMIN and TMAX in one 3-panel figure.

Fan-in step: reads the per-variable comparison tables written by compare_winter_variable
(columns date, day_of_winter, variable, units, value_2024_25, avg_10yr, n_years, diff).
"""
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

PANEL_ORDER = ["PRCP", "TMIN", "TMAX"]
PANEL_NAMES = {
    "PRCP": "Precipitation (PRCP)",
    "TMIN": "Minimum temperature (TMIN)",
    "TMAX": "Maximum temperature (TMAX)",
}
EXPECTED_UNITS = {"PRCP": "mm", "TMIN": "degC", "TMAX": "degC"}
SUPTITLE = ("Corvallis, OR (GHCN USC00351862): winter 2024-25 vs 10-year average "
            "(2014-15 to 2023-24)")
REQUIRED_COLS = {"date", "variable", "units", "value_2024_25", "avg_10yr"}


def _load(path: str) -> pd.DataFrame:
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise FileNotFoundError(f"Comparison table missing or empty: {path}")
    df = pd.read_csv(path)
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"{path} lacks columns {sorted(missing)}; has {list(df.columns)}")
    vars_ = df["variable"].dropna().astype(str).unique()
    if len(vars_) != 1:
        raise ValueError(f"{path} holds variables {list(vars_)}, expected exactly one")
    units = df["units"].dropna().astype(str).unique()
    if len(units) != 1:
        raise ValueError(f"{path} has units {list(units)}, expected exactly one")
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d")
    df["value_2024_25"] = pd.to_numeric(df["value_2024_25"], errors="coerce")
    df["avg_10yr"] = pd.to_numeric(df["avg_10yr"], errors="coerce")
    return df.sort_values("date").reset_index(drop=True)


def main(inputs: dict, outputs: dict, params: dict) -> None:
    paths = inputs["input1"]
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        raise ValueError("input1 (winter_comparison tables) holds no files")
    out_path = outputs["output1"]

    tables = {}
    for p in paths:
        df = _load(p)
        var = str(df["variable"].iloc[0])
        if var in tables:
            raise ValueError(f"Variable {var} appears in more than one input ({p})")
        tables[var] = df
        print(f"Read {var}: {len(df)} rows, {int(df['value_2024_25'].notna().sum())} values, "
              f"{int(df['avg_10yr'].notna().sum())} averages <- {p}")

    missing = [v for v in PANEL_ORDER if v not in tables]
    if missing:
        raise ValueError(f"input1 lacks comparison tables for {missing}; found {sorted(tables)}")
    extra = [v for v in tables if v not in PANEL_ORDER]
    if extra:
        raise ValueError(f"input1 holds unexpected variables {extra}")

    fig, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=True)
    for ax, var in zip(axes, PANEL_ORDER):
        df = tables[var]
        units = str(df["units"].iloc[0])
        if units != EXPECTED_UNITS[var]:
            raise ValueError(f"{var} units are {units!r}, expected {EXPECTED_UNITS[var]!r}")
        if var == "PRCP":
            ax.bar(df["date"], df["value_2024_25"], width=0.8, color="tab:blue",
                   alpha=0.7, label="Winter 2024-25")
            ax.plot(df["date"], df["avg_10yr"], color="black", linewidth=1.5,
                    label="10-year average")
        else:
            color = "tab:blue" if var == "TMIN" else "tab:red"
            ax.plot(df["date"], df["value_2024_25"], color=color, linewidth=1.5,
                    label="Winter 2024-25")
            ax.plot(df["date"], df["avg_10yr"], color="black", linewidth=1.5,
                    linestyle="--", label="10-year average")
        ax.set_ylabel(f"{var} ({units})")
        ax.set_title(PANEL_NAMES[var])
        ax.legend(loc="upper right")
        ax.grid(True, alpha=0.3)

    axes[-1].set_xlabel("Date")
    axes[-1].xaxis.set_major_locator(mdates.MonthLocator())
    axes[-1].xaxis.set_minor_locator(mdates.DayLocator(bymonthday=[8, 15, 22]))
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    first = min(t["date"].min() for t in tables.values())
    last = max(t["date"].max() for t in tables.values())
    axes[-1].set_xlim(first - pd.Timedelta(days=1), last + pd.Timedelta(days=1))

    fig.suptitle(SUPTITLE, fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"Wrote figure -> {out_path}")
