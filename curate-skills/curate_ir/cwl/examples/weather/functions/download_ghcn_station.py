"""Example step: stands in for the GHCN-Daily download (no network)."""


def main(inputs, outputs, params):
    with open(outputs["output1"], "w") as f:
        f.write("date,PRCP,TMIN,TMAX\n2025-01-01,1.0,-2.0,8.0\n")
