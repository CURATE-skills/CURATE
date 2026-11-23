"""Example step: one variable per rank."""

VARIABLES = ["PRCP", "TMIN", "TMAX"]


def main(inputs, outputs, params):
    var = VARIABLES[params["rank"] - 1]
    with open(inputs["input1"]) as f, open(outputs["output1"], "w") as out:
        header = f.readline().strip().split(",")
        col = header.index(var)
        out.write(f"date,{var}\n")
        for line in f:
            row = line.strip().split(",")
            out.write(f"{row[0]},{row[col]}\n")
