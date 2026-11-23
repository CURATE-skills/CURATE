"""plain_contract.py: what counts as a plain function, and what each slot of each step receives."""
import json
from pathlib import Path

import plain_contract as PC
from curate_models import WorkflowSpec

PLAIN = Path(__file__).resolve().parent / "fixtures" / "e2-stages"
FAASR_CATALOG = Path(__file__).resolve().parent / "fixtures" / "catalog-e2-mapreduce-faasr"


def load(folder) -> WorkflowSpec:
    return WorkflowSpec.model_validate(json.loads((folder / "spec-with-code.json").read_text()))


# ── is_plain ──

def test_plain_function_is_plain():
    assert PC.is_plain("def main(inputs, outputs, params):\n    pass\n")
    assert PC.is_plain("def main(inputs: dict, outputs: dict, params: dict) -> None:\n    return True\n")


def test_each_problem_has_its_own_message():
    assert PC.plain_problems("def main(:")[0].startswith("not valid Python")
    assert PC.plain_problems("def split(folder, input1):\n    pass\n") == [
        "no top-level `def main(inputs, outputs, params)`"]
    assert PC.plain_problems("def main(inputs, outputs):\n    pass\n") == [
        "main takes (inputs, outputs); the contract is main(inputs, outputs, params)"]
    assert PC.plain_problems("def main(inputs, outputs, params, *rest):\n    pass\n")[0].startswith("main takes")
    faasr = "def main(inputs, outputs, params):\n    faasr_log('x')\n    r = faasr_rank()\n"
    assert PC.plain_problems(faasr) == [
        "uses the FaaSr API (faasr_log, faasr_rank); a plain function reads and writes the local paths "
        "in inputs/outputs"]
    imp = "from FaaSr_py import x\nimport faasr_stubs\n\ndef main(inputs, outputs, params):\n    pass\n"
    assert PC.plain_problems(imp) == ["imports FaaSr_py, faasr_stubs; a plain function imports no workflow system"]


def test_fixtures():
    assert all(PC.is_plain(n.code) for n in load(PLAIN).nodes)
    old = json.loads(next((FAASR_CATALOG / "workflows").glob("*.json")).read_text())["workflow_spec"]["nodes"]
    assert old and not any(PC.is_plain(n["code"]) for n in old)  # the migration tests' input


def test_secret_names():
    code = ('import os\nk = os.environ["API_KEY"]\nt = os.environ.get("TOKEN_2", "")\n'
            'u = os.getenv(\'USER_PW\')\nh = os.environ["HOME"]\nm = os.getenv("MPLBACKEND")\n'
            'x = os.environ["lower"]\nk2 = os.environ["API_KEY"]\n')
    assert PC.secret_names(code) == ["API_KEY", "TOKEN_2", "USER_PW"]
    assert PC.secret_names(None) == []


# ── io_layout ──

def test_e2_layout():
    layout, problems = PC.io_layout(load(PLAIN))
    assert problems == []
    assert layout["generate_text"] == {"rank": 1, "inputs": [],
                                       "outputs": [{"slot": "output1", "file": "input_text.txt", "kind": "one"}]}
    assert layout["split"]["outputs"] == [{"slot": "output1", "file": "text_batch_{rank}.txt", "kind": "list",
                                           "copies": 3}]
    assert layout["map"]["rank"] == 3
    assert layout["map"]["inputs"] == [{"slot": "input1", "file": "text_batch_{rank}.txt", "kind": "one"}]
    assert layout["map"]["outputs"] == [{"slot": "output1", "file": "map_counts_{rank}.json", "kind": "one"}]
    assert layout["shuffle"]["inputs"] == [{"slot": "input1", "file": "map_counts_{rank}.json", "kind": "folder",
                                            "prefix": "map_counts_", "matches": ["map_counts_{rank}.json"]}]
    assert layout["shuffle"]["outputs"][0]["copies"] == 5
    assert layout["reduce"]["inputs"][0]["kind"] == "one"


def weather(plot_kind="folder", read=None) -> WorkflowSpec:
    """The shape of the catalog's Corvallis workflow: one download, three ranked variables, one plot."""
    return WorkflowSpec.model_validate({"name": "w", "nodes": [
        {"name": "download", "outputs": [{"name": "station.csv"}]},
        {"name": "process", "rank": 3, "inputs": [{"name": "station.csv"}],
         "outputs": [{"name": "winter_{rank}.csv"}]},
        {"name": "plot", "inputs": [{"name": "winter_{rank}.csv", "kind": plot_kind, "read": read}],
         "outputs": [{"name": "plot.png"}]}]})


def test_weather_layout():
    layout, problems = PC.io_layout(weather())
    assert problems == []
    assert layout["process"]["inputs"] == [{"slot": "input1", "file": "station.csv", "kind": "one"}]  # shared
    assert layout["plot"]["inputs"][0]["kind"] == "folder"
    assert layout["plot"]["inputs"][0]["matches"] == ["winter_{rank}.csv"]
    layout, _ = PC.io_layout(weather("file"))
    assert layout["plot"]["inputs"][0] == {"slot": "input1", "file": "winter_{rank}.csv", "kind": "list", "copies": 3}


def test_ranked_reader_reads_its_own_copy_unless_all():
    spec = WorkflowSpec.model_validate({"name": "r", "nodes": [
        {"name": "a", "rank": 2, "outputs": [{"name": "p_{rank}"}]},
        {"name": "b", "rank": 2, "inputs": [{"name": "p_{rank}"}], "outputs": [{"name": "q_{rank}"}]},
        {"name": "c", "rank": 2, "inputs": [{"name": "p_{rank}", "read": "all"}], "outputs": [{"name": "s_{rank}"}]}]})
    layout, problems = PC.io_layout(spec)
    assert problems == []
    assert layout["b"]["inputs"][0]["kind"] == "one"
    assert layout["c"]["inputs"][0] == {"slot": "input1", "file": "p_{rank}", "kind": "list", "copies": 2}


def test_unknown_count_is_a_problem():
    spec = WorkflowSpec.model_validate({"name": "u", "nodes": [
        {"name": "a", "outputs": [{"name": "p_{rank}"}]},
        {"name": "b", "inputs": [{"name": "p_{rank}", "kind": "folder"}], "outputs": [{"name": "o"}]}]})
    _, problems = PC.io_layout(spec)
    assert problems == ["file p_{rank} has {rank} in its name, but no ranked step writes it or reads it one copy "
                        "per rank, so its number of copies is unknown"]


def test_folder_matches_by_prefix_and_not_own_outputs():
    spec = WorkflowSpec.model_validate({"name": "f", "nodes": [
        {"name": "a", "outputs": [{"name": "res_a.csv"}, {"name": "other.csv"}]},
        {"name": "b", "inputs": [{"name": "res_in.csv"}], "outputs": [{"name": "res_b.csv"}]},
        {"name": "c", "inputs": [{"name": "res_", "kind": "folder"}], "outputs": [{"name": "res_c.csv"}]}]})
    assert PC.folder_matches(spec, "c", "res_") == ["res_a.csv", "res_b.csv", "res_in.csv"]


def test_resolve():
    assert PC.resolve("p_{rank}.txt", 2) == "p_2.txt"
    assert PC.resolve("p.txt", 2) == "p.txt" and PC.resolve("p_{rank}", None) == "p_{rank}"


# ── a folder input holds only its own files ──

def folder_spec(*extra_outputs, reader_out="total.json", other_input=None):
    nodes = [{"name": "count", "rank": 3, "outputs": [{"name": "count_{rank}.json"}, *[{"name": o} for o in extra_outputs]]},
             {"name": "sum", "inputs": [{"name": "count_{rank}.json", "kind": "folder"}],
              "outputs": [{"name": reader_out}]}]
    if other_input:
        nodes.append({"name": "side", "inputs": [{"name": other_input}], "outputs": [{"name": "side.txt"}]})
    return WorkflowSpec.model_validate({"name": "f", "nodes": nodes})


def test_folder_conflicts():
    assert PC.folder_conflicts(folder_spec()) == []  # only the folder's own family
    (msg,) = PC.folder_conflicts(folder_spec("count_summary.json"))
    assert msg == ("step sum's folder input input1 (count_{rank}.json) holds every file starting with 'count_', "
                   "so count_summary.json (written by count) would land in it too; rename count_summary.json so "
                   "it does not start with 'count_'")
    assert "count_total.json (written by sum)" in PC.folder_conflicts(folder_spec(reader_out="count_total.json"))[0]
    assert "count_extra.csv (read by side)" in PC.folder_conflicts(folder_spec(other_input="count_extra.csv"))[0]
    assert PC.folder_conflicts(folder_spec("count_summary.json"), only={"count"}) == []  # sum not checked
    bare = WorkflowSpec.model_validate({"name": "b", "nodes": [
        {"name": "w", "rank": 2, "outputs": [{"name": "{rank}.json"}]},
        {"name": "r", "inputs": [{"name": "{rank}.json", "kind": "folder"}], "outputs": [{"name": "o"}]}]})
    assert "has no name before {rank}" in PC.folder_conflicts(bare)[0]
