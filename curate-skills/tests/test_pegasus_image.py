"""A Pegasus run's own image (Gate 4 / "Start this run?"), the shared image check, pinning, and the
visible pegasus_output/ folder. No Pegasus, HTCondor, Docker or ssh."""
import json
import os
from pathlib import Path

import pytest

import envfile
import images
import pegasus_deploy as PD
import spec as S
from conftest import call, ok
from test_pegasus_deploy import Fake, peg_run, run_steps  # noqa: F401  (fixtures)
from test_pegasus_monitor import ALL, JOBS, Host, launched, poll, report, st  # noqa: F401
from test_start import source_run, state  # noqa: F401  (fixture)

yaml = pytest.importorskip("yaml")
DEFAULT_IMG = "docker:///python:3.12-slim"


# ── images.check_image: each case ──

@pytest.mark.parametrize("addr,runtime,image", [
    ("docker://python:3.12-slim", "docker", "docker:///python:3.12-slim"),          # normalized
    ("docker:///python:3.12-slim", "docker", "docker:///python:3.12-slim"),
    ("  docker://ghcr.io/org/img:1.0  ", "docker", "docker:///ghcr.io/org/img:1.0"),
    ("docker://localhost:5000/team/img", "docker", "docker:///localhost:5000/team/img"),
    ("docker://python@sha256:" + "a" * 64, "docker", "docker:///python@sha256:" + "a" * 64),
    ("docker://python:3.12", "singularity", "docker:///python:3.12"),               # Apptainer pulls docker too
    ("library://sylabs/default/alpine:3.18", "singularity", "library://sylabs/default/alpine:3.18"),
    ("shub://vsoch/hello-world", "singularity", "shub://vsoch/hello-world"),
    ("oras://ghcr.io/org/img:1", "singularity", "oras://ghcr.io/org/img:1"),
])
def test_accepted(addr, runtime, image):
    assert images.check_image(addr, runtime) == {"ok": True, "image": image, "error": None}


@pytest.mark.parametrize("addr,runtime,needle", [
    ("", "docker", "is empty"),
    ("python:3.12", "docker", "has no accepted scheme"),
    ("https://example.org/img.tar", "docker", "has no accepted scheme"),
    ("docker://Python:3.12", "docker", "has no valid image reference"),
    ("docker://", "docker", "has no valid image reference"),
    ("library://sylabs/default/alpine", "docker", "needs runtime singularity"),
    ("oras://ghcr.io/x", "docker", "needs runtime singularity"),
    ("shub://", "singularity", "has no valid shub:// reference"),
    ("file://relative/img.tar", "docker", "is not an absolute file:/// path"),
    ("file:///abs/img.sif", "docker", "is not a .tar / .tar.gz / .tgz file"),
    ("file:///abs/img.tar", "singularity", "is not a .sif file"),
])
def test_refused_with_what_is_accepted(addr, runtime, needle):
    out = images.check_image(addr, runtime)
    assert not out["ok"] and out["image"] is None and needle in out["error"]
    assert f"With runtime {runtime}, use docker:///<repo>:<tag>" in out["error"]
    if runtime == "singularity":
        assert "library://…, shub://…, oras://…" in out["error"]


def test_unknown_runtime():
    assert images.check_image(DEFAULT_IMG, "podman")["error"] == "runtime 'podman' is not docker or singularity"


def test_file_images_must_exist_and_be_readable(tmp_path):
    tar = tmp_path / "img.tar"
    assert "does not exist or is not readable on this machine" in images.check_image(f"file://{tar}", "docker")["error"]
    tar.write_bytes(b"x")
    assert images.check_image(f"file://{tar}", "docker") == {"ok": True, "image": f"file://{tar}", "error": None}
    sif = tmp_path / "img.sif"
    sif.write_bytes(b"x")
    assert images.check_image(f"file://{sif}", "singularity")["ok"]
    if os.geteuid() != 0:  # root reads anything
        tar.chmod(0)
        assert not images.check_image(f"file://{tar}", "docker")["ok"]


def test_file_image_on_an_ssh_host_is_checked_there():
    calls = []

    def runner(cmd, timeout=600):
        calls.append(cmd)
        return (0 if "/data/ok.sif" in cmd[-1] else 1), "", ""
    assert images.check_image("file:///data/ok.sif", "singularity", "maxwell", runner)["ok"]
    bad = images.check_image("file:///data/no.sif", "singularity", "maxwell", runner)
    assert bad["error"] == "image file /data/no.sif does not exist or is not readable on maxwell"
    assert calls[0][:6] == ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "maxwell"]
    assert calls[0][-1] == "test -f /data/ok.sif && test -r /data/ok.sif"


# ── Gate 4: check, approve with the run's own image ──

def at_gate4(run):
    s = state(run)
    s["stage"] = "gate4"
    (run / "state.json").write_text(json.dumps(s))


def test_pegasus_image_command_is_read_only_and_shows_the_summary(source_run, capsys):  # noqa: F811
    at_gate4(source_run)
    before = (source_run / "state.json").read_text()
    out = ok(S, ["pegasus-image", "--run", str(source_run), "--image", "docker://me/tools:2"], capsys)
    assert out["image"] == "docker:///me/tools:2" and out["question"] == "Run on Pegasus with docker:///me/tools:2?"
    assert out["options"] == ["Yes, use this image", "Choose another image", "Back"]
    assert out["summary"].startswith("**Pegasus** on this machine: `mapreduce-word-count-")
    assert "image `docker:///me/tools:2` (this run only)" in out["summary"]
    code, bad = call(S, ["pegasus-image", "--run", str(source_run), "--image", "python:3.12"], capsys)
    assert code == 1 and "has no accepted scheme" in bad["error"] and bad["accepted"].startswith("docker:///")
    assert (source_run / "state.json").read_text() == before


def test_pegasus_image_summary_shows_package_warnings(source_run, capsys):  # noqa: F811
    at_gate4(source_run)
    s = state(source_run)
    s["spec"]["nodes"][1]["dependencies"] = ["numpy"]
    s["validation"]["spec_hash"] = S.spec_hash(s["spec"])
    (source_run / "state.json").write_text(json.dumps(s))
    out = ok(S, ["pegasus-image", "--run", str(source_run), "--image", "docker:///me/tools:2"], capsys)
    name = s["spec"]["nodes"][1]["name"]
    assert f"- note: step {name} imports numpy: these must already be in the image docker:///me/tools:2" in out["summary"]


def test_approve_with_image_records_it_for_this_run_only(source_run, capsys, monkeypatch, tmp_path):  # noqa: F811
    f = tmp_path / ".env"
    f.write_text("CURATE_PEGASUS_IMAGE=docker:///from/settings:1\n")
    monkeypatch.setattr(envfile, "default_path", lambda: f)
    at_gate4(source_run)
    out = ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus",
                 "--image", "docker://me/tools:2"], capsys)
    assert out["deploy"] == "pegasus" and out["stage"] == "deploy"
    s = state(source_run)
    assert s["pegasus"] == {"image_choice": "docker:///me/tools:2", "image_source": "chosen at Gate 4, this run only"}
    assert s["history"][-1]["image"] == "docker:///me/tools:2"
    assert f.read_text() == "CURATE_PEGASUS_IMAGE=docker:///from/settings:1\n"  # the settings file never changes
    assert "CURATE_PEGASUS_IMAGE" not in os.environ


def test_approve_with_a_bad_image_changes_nothing(source_run, capsys):  # noqa: F811
    at_gate4(source_run)
    before = (source_run / "state.json").read_text()
    code, out = call(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus",
                         "--image", "file:///nowhere/img.tar"], capsys)
    assert code == 2 and "does not exist or is not readable" in out["error"]
    assert (source_run / "state.json").read_text() == before
    code, out = call(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "yes",
                         "--image", DEFAULT_IMG], capsys)
    assert code == 2 and out["error"] == "--image only applies to --deploy pegasus"


def test_normal_pegasus_option_unchanged(source_run, capsys):  # noqa: F811
    at_gate4(source_run)
    ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus"], capsys)
    s = state(source_run)
    assert s["pegasus"] is None and "image" not in s["history"][-1] and s["deploy_choice"] == "pegasus"


# ── pinned at prepare: state.pegasus and ir.yaml ──

@pytest.fixture
def chosen(source_run, capsys, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(PD, "RUN", Fake(tmp_path))
    at_gate4(source_run)
    ok(S, ["approve", "--run", str(source_run), "--gate", "4", "--on-duplicate", "keep", "--deploy", "pegasus",
           "--image", "docker://me/tools:2"], capsys)
    return source_run


def test_prepare_pins_the_chosen_image(chosen, monkeypatch):
    pf = PD.preflight(str(chosen))
    assert pf["ok"] and "image `docker:///me/tools:2` (chosen at Gate 4, this run only)" in pf["summary"]
    run_steps(chosen, "prepare")
    rec = state(chosen)["pegasus"]
    assert rec["image"] == "docker:///me/tools:2" and rec["image_source"] == "chosen at Gate 4, this run only"
    ir = yaml.safe_load((chosen.resolve() / "pegasus" / "ir" / "ir.yaml").read_text())
    assert ir["defaults"]["image"] == "docker:///me/tools:2"
    # CURATE_PEGASUS_IMAGE does not matter for a run with its own image; the runtime still does
    monkeypatch.setenv("CURATE_PEGASUS_IMAGE", "docker:///something/else:9")
    assert PD.preflight(str(chosen))["ok"]
    monkeypatch.setenv("CURATE_PEGASUS_RUNTIME", "singularity")
    assert "runtime: pinned 'docker', now 'singularity'" in PD.preflight(str(chosen))["problems"][0]


def test_without_a_choice_the_setting_image_is_pinned_and_watched(peg_run, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(PD, "RUN", Fake(tmp_path))
    run_steps(peg_run, "prepare")
    assert state(peg_run)["pegasus"]["image_source"] == "the built-in default"
    monkeypatch.setenv("CURATE_PEGASUS_IMAGE", "docker:///something/else:9")
    assert "image: pinned 'docker:///python:3.12-slim', now 'docker:///something/else:9'" in \
        PD.preflight(str(peg_run))["problems"][0]


def test_preflight_checks_the_setting_image(peg_run, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(PD, "RUN", Fake(tmp_path))
    monkeypatch.setenv("CURATE_PEGASUS_IMAGE", "file:///nowhere/img.tar")
    pf = PD.preflight(str(peg_run))
    assert not pf["ok"] and "image file /nowhere/img.tar does not exist or is not readable" in pf["problems"][0]


def test_monitor_and_gate5_show_the_image(chosen, monkeypatch, capsys):
    run_steps(chosen, "prepare", "launch")
    out = poll(chosen, Host([ALL], collect=report()), monkeypatch)
    assert out["result"] == "success"
    md = ok(S, ["show", "--run", str(chosen), "--gate", "5"], capsys)["markdown"]
    assert "- Image: `docker:///me/tools:2` (chosen at Gate 4, this run only)" in md


# ── "Start this run?" for --start-at deploy ──

def test_start_at_deploy_with_a_different_image(source_run, capsys):  # noqa: F811
    from test_start import start
    code, out = start(capsys, source_run, "deploy", "--from-run", state(source_run)["run_id"])
    assert out["start_options"][2] == "Yes, run on Pegasus with a different image…"
    new = Path(out["run"])
    code, bad = call(S, ["confirm-start", "--run", str(new), "--deploy", "pegasus", "--image", "python"], capsys)
    assert code == 2 and "has no accepted scheme" in bad["error"] and state(new)["deploy_choice"] is None
    assert ok(S, ["pegasus-image", "--run", str(new), "--image", "docker://me/tools:2"], capsys)["ok"]
    res = ok(S, ["confirm-start", "--run", str(new), "--deploy", "pegasus", "--image", "docker://me/tools:2"], capsys)
    assert res["deploy_choice"] == "pegasus"
    s = state(new)
    assert s["pegasus"] == {"image_choice": "docker:///me/tools:2",
                            "image_source": 'chosen at "Start this run?", this run only'}
    assert s["history"][-1]["image"] == "docker:///me/tools:2"


# ── pegasus_output/<workflow>-<run_id>/ ──

def outdir_of(run):
    s = state(run)
    return Path(s["project_dir"]) / "pegasus_output" / f"mapreduce_word_count-{s['run_id']}"


def test_success_output_folder(launched, monkeypatch):  # noqa: F811
    work = Path(state(launched)["pegasus"]["work_dir"])
    (work / "deliver" / "results").mkdir(parents=True)
    for i in range(1, 6):
        (work / "deliver" / "results" / f"word_count_{i}.json").write_text(f'{{"w{i}": 1000}}')
    (work / "run-report.json").write_text(json.dumps(report()))
    out = poll(launched, Host([ALL], collect=report()), monkeypatch)
    d = outdir_of(launched)
    assert out["lines"][-1] == f"✓ workflow succeeded (11 jobs; results in {d})"
    assert sorted(p.name for p in (d / "results").iterdir()) == [f"word_count_{i}.json" for i in range(1, 6)]
    assert (d / "run-report.json").is_file() and not (d / "FAILED.txt").exists()
    assert state(launched)["monitor"]["output_dir"] == str(d)


def test_failure_copies_partial_outputs_with_a_marker_once_per_launch(launched, monkeypatch, capsys):  # noqa: F811
    work = Path(state(launched)["pegasus"]["work_dir"])
    (work / "deliver" / "results").mkdir(parents=True)
    (work / "deliver" / "results" / "word_count_1.json").write_text("{}")
    rep = report("failure (DAGMan status 1)", delivered=["results/word_count_1.json"],
                 missing=[f"word_count_{i}.json" for i in range(2, 6)])
    (work / "run-report.json").write_text(json.dumps(rep))
    failed = st("failure", done=JOBS[:7], failed=["reduce__2"], exit_=1)  # up to reduce__1
    out = poll(launched, Host([failed], collect=rep), monkeypatch)
    d = outdir_of(launched)
    assert out["result"] == "failure" and out["lines"][-1].endswith(f"; partial outputs in {d}")
    assert (d / "results" / "word_count_1.json").is_file() and (d / "run-report.json").is_file()
    marker = (d / "FAILED.txt").read_text()
    assert marker.startswith("This Pegasus run FAILED: step reduce failed; DAGMan exit 1; missing outputs:")
    assert "(partial; marked FAILED)" in state(launched)["monitor"]["summary"]
    # a re-check of the same launch reuses the folder: no -2
    ok(S, ["begin", "--run", str(launched), "--stage", "monitor"], capsys)
    out = poll(launched, Host([failed], collect=rep), monkeypatch)
    assert out["lines"][-1].endswith(f"; partial outputs in {d}")
    assert not Path(f"{d}-2").exists()


def test_a_new_launch_gets_a_new_folder(launched, monkeypatch):  # noqa: F811
    d = outdir_of(launched)
    d.mkdir(parents=True)
    (d / "results.txt").write_text("from an earlier launch")  # never overwritten
    out = poll(launched, Host([ALL], collect=report()), monkeypatch)
    assert out["lines"][-1].endswith(f"results in {d}-2)")
    assert (d / "results.txt").read_text() == "from an earlier launch"


def test_report_only_writes_no_output_folder(launched, monkeypatch):  # noqa: F811
    poll(launched, Host([ALL], collect=report()), monkeypatch, report_only=True)
    assert not (Path(state(launched)["project_dir"]) / "pegasus_output").exists()
