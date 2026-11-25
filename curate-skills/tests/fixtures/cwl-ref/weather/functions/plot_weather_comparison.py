import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

PLOT_START = pd.Timestamp("2025-01-01")
PLOT_END = pd.Timestamp("2025-03-01")

# Panel order: precipitation, max temperature, min temperature
PANELS = [
    ("PRCP", "Precipitation", "Precipitation (as reported, tenths of mm)"),
    ("TMAX", "Maximum temperature", "Max temperature (°C)"),
    ("TMIN", "Minimum temperature", "Min temperature (°C)"),
]


def main(inputs, outputs, params):
    # inputs["input1"] is a folder holding every per-variable comparison file
    # (winter_comparison_<rank>.csv), and only those.
    folder = inputs["input1"]
    names = sorted(os.listdir(folder))
    if not names:
        msg = f"ERROR: the input folder {folder} holds no comparison files"
        print(msg, flush=True)
        raise FileNotFoundError(msg)
    print(f"Found {len(names)} comparison files: {names}", flush=True)

    frames = []
    for name in names:
        df = pd.read_csv(os.path.join(folder, name))
        required = {"date", "variable", "this_year", "ten_year_avg"}
        if not required.issubset(df.columns):
            msg = f"ERROR: {name} missing columns {sorted(required - set(df.columns))}"
            print(msg, flush=True)
            raise ValueError(msg)
        frames.append(df)

    data = pd.concat(frames, ignore_index=True)
    data["date"] = pd.to_datetime(data["date"], errors="coerce")
    data["this_year"] = pd.to_numeric(data["this_year"], errors="coerce")
    data["ten_year_avg"] = pd.to_numeric(data["ten_year_avg"], errors="coerce")
    data = data.dropna(subset=["date"])
    data = data[(data["date"] >= PLOT_START) & (data["date"] <= PLOT_END)]

    missing = [v for v, _, _ in PANELS if v not in set(data["variable"])]
    if missing:
        msg = f"ERROR: no data for variable(s) {missing} in inputs"
        print(msg, flush=True)
        raise ValueError(msg)

    fig, axes = plt.subplots(len(PANELS), 1, figsize=(11, 10), sharex=True)
    for ax, (var, title, ylabel) in zip(axes, PANELS):
        sub = data[data["variable"] == var].sort_values("date")
        ax.plot(sub["date"], sub["this_year"], linestyle="-", color="tab:blue",
                label="Winter 2025")
        ax.plot(sub["date"], sub["ten_year_avg"], linestyle="--", color="tab:orange",
                label="2015-2024 average")
        ax.set_title(title)
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right")
    axes[-1].set_xlabel("Date")
    axes[-1].set_xlim(PLOT_START, PLOT_END)
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    fig.suptitle("Corvallis, OR (USC00351862): winter 2025 vs 2015-2024 average")
    fig.tight_layout(rect=(0, 0, 1, 0.97))

    fig.savefig(outputs["output1"], dpi=150)
    plt.close(fig)
    print(f"Wrote figure to {outputs['output1']}", flush=True)
