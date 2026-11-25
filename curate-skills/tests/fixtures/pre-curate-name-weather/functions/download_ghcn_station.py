import csv
import io
import os
import shutil
import tempfile

import requests

GHCN_URL = (
    "https://www.ncei.noaa.gov/data/global-historical-climatology-network-daily/"
    "access/USC00351862.csv"
)


def _curate_copy(src, dst):
    """Copy a file unless src and dst are the same path (added by migrate_catalog.py)."""
    if os.path.abspath(src) != os.path.abspath(dst):
        shutil.copyfile(src, dst)


def main(inputs, outputs, params):
    """Download the NOAA GHCN-Daily CSV for station USC00351862 and upload it unchanged."""
    output1 = outputs["output1"]
    print(f"Downloading GHCN-Daily station CSV from {GHCN_URL}", flush=True)
    try:
        resp = requests.get(GHCN_URL, timeout=300)
        resp.raise_for_status()
    except Exception as e:
        print(f"ERROR: failed to download {GHCN_URL}: {e}", flush=True)
        raise

    content = resp.content
    if not content or not content.strip():
        msg = f"ERROR: empty response from {GHCN_URL}"
        print(msg, flush=True)
        raise RuntimeError(msg)

    # Validate header contains a DATE column
    first_line = content.split(b"\n", 1)[0].decode("utf-8", errors="replace")
    header = next(csv.reader(io.StringIO(first_line)), [])
    header = [h.strip().strip('"') for h in header]
    if "DATE" not in header:
        msg = f"ERROR: downloaded CSV has no DATE column; header: {header[:20]}"
        print(msg, flush=True)
        raise RuntimeError(msg)

    n_lines = content.count(b"\n")
    print(f"Downloaded {len(content)} bytes, ~{n_lines} lines; columns: {len(header)}", flush=True)

    tmpdir = tempfile.mkdtemp()
    local_path = os.path.join(tmpdir, "USC00351862_daily.csv")
    with open(local_path, "wb") as f:
        f.write(content)

    _curate_copy(local_path, output1)
    print(f"Uploaded station CSV to ./{output1}", flush=True)
