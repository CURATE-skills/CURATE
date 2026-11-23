"""Example step (reduce): one copy per rank."""


def main(inputs, outputs, params):
    with open(inputs["input1"]) as f, open(outputs["output1"], "w") as out:
        out.write(f"{params['rank']}/{params['max_rank']}: {len(f.read())}\n")
