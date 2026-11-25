"""Download NOAA GHCN-Daily data for one station and write one CSV per variable.

Source: NOAA NCEI GHCN-Daily per-station access CSV (public, no credentials).
Output i (rank order) holds variable VARIABLES[i]: PRCP (mm), TMIN (degC), TMAX (degC).
"""
import io
import time

import pandas as pd
import requests

DEFAULT_STATION = "USC00351862"  # CORVALLIS STATE UNIV, Oregon
BASE_URL = "https://www.ncei.noaa.gov/data/global-historical-climatology-network-daily/access/{station}.csv"
WINDOW_START = "2014-12-01"
WINDOW_END = "2025-02-28"

# rank order: rank 1 = PRCP, rank 2 = TMIN, rank 3 = TMAX
VARIABLES = ["PRCP", "TMIN", "TMAX"]
UNITS = {"PRCP": "mm", "TMIN": "degC", "TMAX": "degC"}
SCALE = {"PRCP": 0.1, "TMIN": 0.1, "TMAX": 0.1}  # GHCN tenths -> mm / degC

TIMEOUT = 120
RETRIES = 4


def _download(url: str) -> str:
    last_err = None
    for attempt in range(1, RETRIES + 1):
        try:
            print(f"Downloading {url} (attempt {attempt}/{RETRIES})")
            resp = requests.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
            if not resp.text.strip():
                raise RuntimeError(f"Empty response from {url}")
            return resp.text
        except (requests.RequestException, RuntimeError) as err:
            last_err = err
            print(f"Download attempt {attempt} failed: {err}")
            if attempt < RETRIES:
                time.sleep(5 * attempt)
    raise RuntimeError(f"Failed to download GHCN-Daily data from {url} after {RETRIES} attempts: {last_err}")


def _quality_flag(attr) -> str:
    """Return the quality flag (second comma-separated field) of a GHCN _ATTRIBUTES value."""
    if attr is None or (isinstance(attr, float) and pd.isna(attr)):
        return ""
    parts = str(attr).split(",")
    return parts[1].strip() if len(parts) > 1 else ""


def main(inputs: dict, outputs: dict, params: dict) -> None:
    station = (params or {}).get("station", DEFAULT_STATION)
    out_paths = outputs["output1"]
    if isinstance(out_paths, str):
        out_paths = [out_paths]
    n = len(out_paths)
    if n == 0:
        raise ValueError("outputs['output1'] holds no paths")
    if n > len(VARIABLES):
        raise ValueError(f"outputs['output1'] has {n} paths but only {len(VARIABLES)} variables are defined: {VARIABLES}")

    url = BASE_URL.format(station=station)
    text = _download(url)
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False, low_memory=False)
    if "DATE" not in df.columns:
        raise RuntimeError(f"Downloaded file for station {station} has no DATE column; columns: {list(df.columns)[:10]}")

    present = [v for v in VARIABLES if v in df.columns]
    if not present:
        raise RuntimeError(f"Station {station} has none of the variables {VARIABLES}")
    print(f"Station {station}: {len(df)} rows total; variables present: {present}")

    df["DATE"] = pd.to_datetime(df["DATE"], format="%Y-%m-%d")
    df = df[(df["DATE"] >= WINDOW_START) & (df["DATE"] <= WINDOW_END)]
    if df.empty:
        raise RuntimeError(f"Station {station} has no rows between {WINDOW_START} and {WINDOW_END}")
    if df["DATE"].duplicated().any():
        raise RuntimeError(f"Station {station} has duplicate dates in the window")
    df = df.set_index("DATE")
    full_index = pd.date_range(WINDOW_START, WINDOW_END, freq="D")

    for i, path in enumerate(out_paths):
        var = VARIABLES[i]
        if var in df.columns:
            raw = pd.to_numeric(df[var].str.strip().replace("", pd.NA), errors="coerce")
            attr_col = f"{var}_ATTRIBUTES"
            if attr_col in df.columns:
                qflag = df[attr_col].map(_quality_flag)
                raw = raw.where(qflag == "")
                n_flagged = int((qflag != "").sum())
            else:
                n_flagged = 0
            values = (raw * SCALE[var]).round(1)
        else:
            print(f"WARNING: station {station} has no {var} column; all values blank")
            values = pd.Series(dtype=float)
            n_flagged = 0
        values = values.reindex(full_index)

        out = pd.DataFrame({
            "date": full_index.strftime("%Y-%m-%d"),
            "variable": var,
            "value": values.values,
            "units": UNITS[var],
        })
        out.to_csv(path, index=False)
        print(f"Wrote {var} ({UNITS[var]}): {len(out)} rows, {int(out['value'].notna().sum())} valid, "
              f"{n_flagged} quality-flagged -> {path}")
