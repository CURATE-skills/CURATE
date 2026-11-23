"""migrate_catalog.py on a copy of the mapreduce catalog fixture: the rewrite, the output check
(old code vs new code on the same inputs), hand fixes, backups and folder-input conflicts."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

import migrate_catalog as M
import plain_contract as PC

pytestmark = pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")

FIX = Path(__file__).resolve().parent / "fixtures"
SHUFFLE_PLAIN = (FIX / "e2-stages-plain" / "functions" / "shuffle.py").read_text()


@pytest.fixture
def cat(tmp_path):
    dst = tmp_path / "catalog"
    shutil.copytree(FIX / "catalog-e2-mapreduce", dst)
    return dst


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def blocks(res, kind=None) -> dict:
    return {(e["kind"], b["step"]): b for e in res["entries"] for b in e["blocks"] if kind in (None, e["kind"])}


def shuffle_fix(folder: Path) -> Path:
    old = json.loads(next((FIX / "catalog-e2-mapreduce" / "workflows").glob("*.json")).read_text())
    code = next(n["code"] for n in old["workflow_spec"]["nodes"] if n["name"] == "shuffle")
    folder.mkdir()
    (folder / "shuffle.json").write_text(json.dumps({
        "name": "shuffle", "old_sha256": hashlib.sha256(code.encode()).hexdigest(), "code": SHUFFLE_PLAIN,
        "inputs": {"input1": {"kind": "folder"}}}))
    return folder


def test_dry_run_converts_checks_and_writes_nothing(cat):
    before = snapshot(cat)
    res = M.migrate(cat)
    assert snapshot(cat) == before  # a dry run writes nothing
    b = blocks(res)
    for step in ("generate_text", "split", "map", "reduce"):
        for kind in ("function", "workflow"):
            assert b[(kind, step)]["status"] == "converted", (kind, step, b[(kind, step)])
            assert b[(kind, step)]["verified"]["ok"]
    assert b[("workflow", "reduce")]["verified"]["detail"] == "outputs equal on 5 rank(s)"
    assert b[("function", "map")]["verified"]["detail"].endswith("(checked in a workflow entry)")
    assert b[("workflow", "shuffle")]["status"] == "manual"
    assert "faasr_get_folder_list" in b[("workflow", "shuffle")]["reason"]
    assert res["ok"] is False and res["summary"] == {"converted": 8, "manual": 2}
    assert "+def main(inputs, outputs, params):" in b[("workflow", "map")]["diff"]


def test_apply_with_a_hand_fix_backs_up_converts_and_is_idempotent(cat, tmp_path):
    before = snapshot(cat)
    res = M.migrate(cat, apply=True, fixes_dir=shuffle_fix(tmp_path / "fixes"))
    assert res["ok"] and res["summary"] == {"converted": 8, "fixed": 2}, res["summary"]
    assert blocks(res)[("workflow", "shuffle")]["verified"]["ok"]
    backup = Path(res["backup_dir"])
    assert backup.parent == cat and backup.name.startswith(".backup-")
    assert {p: v for p, v in snapshot(backup).items()} == before  # every changed file, as it was
    wf = json.loads(next((cat / "workflows").glob("*.json")).read_text())["workflow_spec"]
    assert all(PC.is_plain(n["code"]) for n in wf["nodes"])
    assert next(n for n in wf["nodes"] if n["name"] == "shuffle")["inputs"][0]["kind"] == "folder"
    assert next(n for n in wf["nodes"] if n["name"] == "shuffle")["code"] == SHUFFLE_PLAIN
    again = M.migrate(cat, apply=True)
    assert again["summary"] == {"already_plain": 10} and again["written"] == [] and again["ok"]


def test_a_mismatch_is_manual_not_written(cat, tmp_path):
    fixes = shuffle_fix(tmp_path / "fixes")
    fix = json.loads((fixes / "shuffle.json").read_text())
    fix["code"] = SHUFFLE_PLAIN.replace("json.dump(part, f", "json.dump({'oops': 1}, f")
    (fixes / "shuffle.json").write_text(json.dumps(fix))
    res = M.migrate(cat, apply=True, fixes_dir=fixes)
    b = blocks(res)[("workflow", "shuffle")]
    assert b["status"] == "manual" and b["proposed"] == "fixed"
    assert "differs between the old and new code" in b["reason"]
    wf = json.loads(next((cat / "workflows").glob("*.json")).read_text())["workflow_spec"]
    assert not PC.is_plain(next(n for n in wf["nodes"] if n["name"] == "shuffle")["code"])  # left as it was


def test_folder_conflicts_are_reported_with_renames_and_not_written(cat, tmp_path):
    wf_file = next((cat / "workflows").glob("*.json"))
    data = json.loads(wf_file.read_text())
    for n in data["workflow_spec"]["nodes"]:
        if n["name"] == "reduce":  # an extra output that would land in shuffle's folder input
            n["outputs"].append({"name": "map_counts_summary.json", "type": "json", "description": ""})
    wf_file.write_text(json.dumps(data, indent=2))
    before = wf_file.read_bytes()
    res = M.migrate(cat, fixes_dir=shuffle_fix(tmp_path / "fixes"), verify=False)
    (c,) = res["folder_conflicts"]
    assert "map_counts_summary.json (written by reduce) would land in it too" in c["problems"][0]
    assert c["proposed_renames"] == {"map_counts_summary.json": "summary_map_counts.json"}
    assert res["ok"] is False
    entry = next(e for e in res["entries"] if e["kind"] == "workflow")
    assert entry["not_written"] == "folder-input conflict"
    assert wf_file.read_bytes() == before


def test_missing_catalog_and_cli(tmp_path, capsys):
    assert "does not exist" in M.migrate(tmp_path / "nope")["error"]
    assert M.main(["--catalog-dir", str(tmp_path / "nope")]) == 2
    assert M.main(["--catalog-dir", str(tmp_path), "--apply", "--no-verify"]) == 2
    assert "needs the output check" in capsys.readouterr().out


# ── the rewrite rules on their own ──

def test_rewrite_rules():
    old = '''import os


def step(folder, input1, output1):
    """Doc."""
    r = faasr_rank()
    key = faasr_secret("API_KEY")
    faasr_get_file(local_file="in.txt", remote_folder=folder, remote_file=input1.replace("{rank}", str(r["rank"])))
    faasr_log(f"read {input1} from {folder}")
    faasr_put_file(local_file="in.txt", remote_folder=folder, remote_file=output1)
'''
    with pytest.raises(M.Manual, match="a file argument that itself needs rewriting"):
        M.convert(old, "step", {"input1": "one", "output1": "one"})
    old = old.replace('remote_file=input1.replace("{rank}", str(r["rank"]))', "remote_file=name")
    old = old.replace('    faasr_get_file', '    name = input1.replace("{rank}", str(r["rank"]))\n    faasr_get_file')
    new = M.convert(old, "step", {"input1": "one", "output1": "one"})
    assert PC.is_plain(new)
    assert '    """Doc."""\n    input1 = inputs["input1"]\n    output1 = outputs["output1"]\n' in new
    assert 'r = {"rank": params["rank"], "max_rank": params["max_rank"]}' in new
    assert 'key = os.environ["API_KEY"]' in new and "name = input1\n" in new
    assert '_curate_copy(name, "in.txt")' in new and '_curate_copy("in.txt", output1)' in new
    assert 'print(f"read {input1} from .", flush=True)' in new
    assert "import os\nimport shutil\n" in new


@pytest.mark.parametrize("code,needle", [
    ("def s(folder, output1):\n    x = faasr_get_folder_list(prefix=folder)\n", "faasr_get_folder_list"),
    ("def s(folder, output1):\n    p = folder + '/x'\n", "uses `folder`"),
    ("def s(folder, output1):\n    faasr_put_file('a', folder, output1)\n", "arguments other than"),
    ("def s(x):\n    pass\n", "does not take (folder"),
    ("def other(folder):\n    pass\n", "no top-level `def s"),
])
def test_patterns_left_for_a_hand_conversion(code, needle):
    with pytest.raises(M.Manual, match=needle.replace("(", r"\(")):
        M.convert(code, "s", {"output1": "one"})


def test_list_slot_by_index():
    old = ("def s(folder, output1):\n    for i in range(1, 4):\n"
           "        faasr_put_file(local_file='x', remote_folder=folder, remote_file=output1.replace('{rank}', str(i)))\n")
    with pytest.raises(M.Manual):
        M.convert(old, "s", {"output1": "list"})  # inline: needs rewriting inside the call
    old = ("def s(folder, output1):\n    for i in range(1, 4):\n        n = output1.replace('{rank}', str(i))\n"
           "        faasr_put_file(local_file='x', remote_folder=folder, remote_file=n)\n")
    assert "n = output1[i - 1]" in M.convert(old, "s", {"output1": "list"})
    old = old.replace("str(i)", "k")
    assert "n = output1[int(k) - 1]" in M.convert(old, "s", {"output1": "list"})
