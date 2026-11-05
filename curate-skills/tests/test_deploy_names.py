"""Per-run deployment names, --list and --teardown (fakes only: nothing reaches GitHub or S3)."""
import json
from pathlib import Path

import pytest

import deploy as D
from test_deploy import FAKE, FakeGitHub, env, workflow  # noqa: F401  (env is a fixture)

A = "20261105-093012-weather"   # approved Gate 4 at 09:30 on 5 Nov
A2 = "20261105-093055-weather"  # same minute
B = "20261106-140000-weather"   # deploy-only from A
C = "20261107-080000-weather"   # deploy-only from B
OLD = "20261101-172823-weather"  # deployed before per-run names: "-cs"


@pytest.fixture
def plain(env, monkeypatch):
    monkeypatch.delenv("CURATE_DEPLOY_NAME_SUFFIX")  # the per-run default, not the override
    return monkeypatch


@pytest.fixture
def project(tmp_path):
    bundle = tmp_path / "faasr_output" / "weather"
    (bundle / "functions").mkdir(parents=True)
    (bundle / "workflow.json").write_text(json.dumps(workflow(), indent=2))
    (bundle / "functions" / "plot_weather.py").write_text("def plot_weather(): pass\n")
    (bundle / "functions" / "download_station.py").write_text("def download_station(): pass\n")
    return tmp_path


def make_run(project, run_id, copied_from=None, deploy=None, stage="deploy", log=None):
    bundle = project / "faasr_output" / "weather"
    h = D.tree_hash(bundle)
    r = project / ".curate" / "runs" / run_id
    (r / "deploy").mkdir(parents=True)
    hist = [{"gate": 4, "decision": "copied", "from_run": copied_from, "bundle_hash": h,
             "snapshot": "005-gate4-approve.json"}] if copied_from \
        else [{"gate": 4, "decision": "approve"}]
    st = {"schema_version": 2, "run_id": run_id, "stage": stage, "bundle_dir": str(bundle), "bundle_hash": h,
          "deploy_choice": "yes", "history": hist}
    if deploy:
        st["deploy"] = {"repo": "alice/faasr-actions", "branch": "main", "bundle_hash": h, **deploy}
    (r / "state.json").write_text(json.dumps(st))
    if log is not None:
        (r / "deploy" / "deploy-log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in log))
    return r


def deployed(name, suffix, steps=D.STEPS, **extra):
    return {"deploy_name": name, "suffix": suffix, "invocation_id": "inv",
            "steps": {x: {"ok": True, "ts": f"2026-11-05T10:0{i}:00+00:00"} for i, x in enumerate(steps)}, **extra}


class Bucket:
    """A fake S3 bucket: head_bucket, list_objects_v2 (one-key probes and full listings), delete_object."""

    def __init__(self, keys=(), bucket="b"):
        self.keys, self.bucket, self.deleted, self.content = set(keys), bucket, [], {}

    def __call__(self, cfg, access_key, secret_key):
        return self

    def head_bucket(self, Bucket):
        assert Bucket == self.bucket

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=None, ContinuationToken=None):
        hit = sorted(k for k in self.keys if k.startswith(Prefix))[:MaxKeys or None]
        return {"KeyCount": len(hit), "Contents": [{"Key": k} for k in hit], "IsTruncated": False}

    def get_object(self, Bucket, Key):
        import io
        return {"Body": io.BytesIO(self.content.get(Key, f"data of {Key}").encode())}

    def delete_object(self, Bucket, Key):
        assert Key in self.keys
        self.keys.discard(Key)
        self.deleted.append(Key)


def cli(capsys, *argv, fake=None, s3=None):
    code = D.main(list(argv), opener=fake or FakeGitHub(), s3_factory=s3 or Bucket())
    return code, json.loads(capsys.readouterr().out)


# ── naming ──

def test_default_name_is_the_workflow_and_the_approving_runs_date_time(project, plain, capsys):
    r = make_run(project, A)
    code, out = cli(capsys, "preflight", "--run", str(r))
    assert code == 0 and out["ok"], out["problems"]
    assert out["deploy_name"] == "weather-1105-0930" and out["suffix"] == "-1105-0930"
    assert out["name_source"] == f"Gate 4 approval in run {A}"
    assert [t["prefix"] for t in out["s3_targets"]] == ["weather_data-1105-0930/", "FaaSrLog/weather-1105-0930/"]
    assert "-cs" not in json.dumps(out) and f"Deploys as: `weather-1105-0930` (name from: Gate 4 approval in run {A})" in out["summary"]


def test_a_second_run_of_the_same_workflow_gets_its_own_name(project, plain, capsys):
    make_run(project, A, deploy=deployed("weather-1105-0930", "-1105-0930"), stage="done")
    later = make_run(project, "20261105-161500-weather")
    code, out = cli(capsys, "preflight", "--run", str(later))
    assert out["ok"] and out["deploy_name"] == "weather-1105-1615"


@pytest.mark.parametrize("taken_by", ["project", "repo"])
def test_a_taken_minute_adds_seconds(project, plain, capsys, taken_by):
    fake = FakeGitHub()
    if taken_by == "project":
        make_run(project, A2, deploy=deployed("weather-1105-0930", "-1105-0930"), stage="done")
    else:  # the name exists in the repo (someone else's deployment)
        fake.files["workflows/weather-1105-0930.json"] = json.dumps({"WorkflowName": "weather-1105-0930"})
    r = make_run(project, A)
    code, out = cli(capsys, "preflight", "--run", str(r), fake=fake)
    assert out["ok"], out["problems"]
    assert out["deploy_name"] == "weather-1105-093012" and "weather-1105-0930 is taken" in out["name_source"]


def test_both_names_taken_is_refused_and_curate_s_plain_deployment_is_left_alone(project, plain, capsys):
    curate = {"workflows/weather.json": json.dumps({"WorkflowName": "weather"}),
              ".github/workflows/weather-plot.yml": "name: weather-plot\n"}
    theirs = {f"workflows/weather-1105-0930{x}.json": json.dumps({"WorkflowName": "x"}) for x in ("", "12")}
    r = make_run(project, A)
    code, out = cli(capsys, "preflight", "--run", str(r), fake=FakeGitHub(files={**curate, **theirs}))
    assert code == 1 and not out["ok"]
    assert any("workflows/weather-1105-093012.json already exists (does not belong" in p for p in out["problems"])
    assert out["curate_files"] == ["workflows/weather.json", ".github/workflows/weather-plot.yml"]
    # with only CURATE's plain deployment there, the run deploys under its own name
    code, out = cli(capsys, "preflight", "--run", str(r), fake=FakeGitHub(files=curate))
    assert out["ok"] and out["deploy_name"] == "weather-1105-0930" and "workflows/weather.json" in out["curate_files"]


def test_an_override_naming_another_runs_deployment_is_refused(project, plain, capsys):
    make_run(project, A2, deploy=deployed("weather-team", "-team"), stage="done")
    r = make_run(project, A)
    code, out = cli(capsys, "preflight", "--run", str(r), "--name-suffix=-team")
    assert not out["ok"] and any(f"is run {A2}'s deployment in this project" in p for p in out["problems"])
    plain.setenv("CURATE_DEPLOY_NAME_SUFFIX", "-alice")  # the override still works
    code, out = cli(capsys, "preflight", "--run", str(r))
    assert out["ok"] and out["deploy_name"] == "weather-alice"
    assert out["name_source"] == "CURATE_DEPLOY_NAME_SUFFIX / --name-suffix"


def test_a_run_id_without_a_date_needs_the_override(project, plain, capsys):
    r = make_run(project, "my-run")
    code, out = cli(capsys, "preflight", "--run", str(r))
    assert code == 2 and "set CURATE_DEPLOY_NAME_SUFFIX" in out["error"]


def test_the_plain_workflow_name_is_never_a_deploy_name(project, plain):
    r = make_run(project, A)
    with pytest.raises(D.InputError):
        D.preflight(str(r), "")
    real = D.deploy_copy

    def same_name(wf, sfx, *a):  # a deploy copy that kept the plain name (defence in depth)
        copy_wf, targets, s3_targets = real(wf, sfx, *a)
        return {**copy_wf, "WorkflowName": wf["WorkflowName"]}, targets, s3_targets
    import unittest.mock as m
    with m.patch.object(D, "deploy_copy", same_name):
        pf = D.preflight(str(r), "-x", s3_factory=Bucket())
    assert any("is the workflow's own name, which CURATE's deployment uses" in p for p in pf["problems"])


# ── lineage: deploy-only runs keep the approving run's name ──

def test_a_deploy_only_run_uses_its_source_runs_name(project, plain, capsys):
    make_run(project, A, deploy=deployed("weather-1105-0930", "-1105-0930"), stage="done")
    b = make_run(project, B, copied_from=A)
    c = make_run(project, C, copied_from=B)
    for r in (b, c):
        code, out = cli(capsys, "preflight", "--run", str(r))
        assert out["deploy_name"] == "weather-1105-0930"
        assert out["name_source"] == f"run {A}'s deployment (this run was started from it)"
    assert D.lineage(c, json.loads((c / "state.json").read_text()))[-1][0] == A


def test_a_deploy_only_run_from_an_undeployed_run_uses_that_runs_date_time(project, plain, capsys):
    make_run(project, A, stage="done")  # approved Gate 4 without deploying
    b = make_run(project, B, copied_from=A)
    code, out = cli(capsys, "preflight", "--run", str(b))
    assert out["ok"] and out["deploy_name"] == "weather-1105-0930" and out["name_source"] == f"Gate 4 approval in run {A}"


def test_old_style_names_keep_working(project, plain, capsys):
    # the run that deployed with "-cs", and a deploy-only run from it (invoke only)
    old = make_run(project, OLD, deploy=deployed("weather-cs", "-cs"), stage="done")
    new = make_run(project, B, copied_from=OLD)
    assert D.resolve_suffix(str(new))["suffix"] == "-cs"
    assert D.resolve_suffix(str(old)) == {"suffix": "-cs", "source": "pinned: this run's deploy started with it"}
    with pytest.raises(D.InputError, match="keep it"):
        D.resolve_suffix(str(old), "-other")
    code, out = cli(capsys, "status", "--run", str(new))
    assert out["reuse_possible"] == OLD


# ── list ──

def test_list_shows_each_deployment_once_with_its_runs(project, plain, capsys, monkeypatch):
    make_run(project, OLD, deploy=deployed("weather-cs", "-cs"), stage="done")
    make_run(project, A, deploy=deployed("weather-1105-0930", "-1105-0930"), stage="done")
    reused = deployed("weather-1105-0930", "-1105-0930", reused_from=A)
    reused["steps"]["invoke"]["ts"] = "2026-11-06T14:05:00+00:00"
    make_run(project, B, copied_from=A, deploy=reused, stage="done")
    make_run(project, "20261107-090000-weather", deploy=deployed(
        "weather-1107-0900", "-1107-0900", teardown={"ts": "2026-11-08T00:00:00+00:00"}), stage="done")
    make_run(project, C)  # never deployed: not listed
    monkeypatch.chdir(project)
    code, out = cli(capsys, "list")
    assert code == 0
    rows = {r["name"]: r for r in out["deployments"]}
    assert set(rows) == {"weather-cs", "weather-1105-0930", "weather-1107-0900"}
    a = rows["weather-1105-0930"]
    assert a["run"] == A and a["reused_by"] == [B] and a["invocations"] == 2
    assert a["deployed"] == "2026-11-05T10:00:00+00:00" and a["last_invoked"] == "2026-11-06T14:05:00+00:00"
    assert rows["weather-1107-0900"]["torn_down"] == "2026-11-08T00:00:00+00:00"
    assert f"| `weather-1105-0930` | alice/faasr-actions | {A}; reused by {B} |" in out["markdown"]


def test_list_with_no_runs(tmp_path, capsys):
    code, out = cli(capsys, "list", "--project", str(tmp_path))
    assert code == 0 and out["deployments"] == [] and "No deployments" in out["markdown"]


# ── teardown ──

NAME, SFX = "weather-1105-0930", "-1105-0930"
FILES = {f"functions/{NAME}/download_station.py": "def download_station(): pass\n",
         f"functions/{NAME}/plot_weather.py": "def plot_weather(): pass\n",
         f"workflows/{NAME}.json": '{"WorkflowName": "weather-1105-0930"}',
         f".github/workflows/{NAME}-download.yml": f"name: {NAME}-download\n",
         f".github/workflows/{NAME}-plot.yml": f"name: {NAME}-plot\n"}
PREFIXES = [f"weather_data{SFX}/", f"FaaSrLog/{NAME}/"]


def torn(project, run_id=A, files=FILES, shas=True, **kw):
    log = [{"run_id": run_id, "deploy_name": NAME, "step": "publish", "repo": "alice/faasr-actions", "branch": "main",
            "path": p, **({"sha": D.blob_sha(c)} if shas else {})} for p, c in files.items()]
    log += [{"run_id": run_id, "deploy_name": NAME, "step": "invoke", "bucket": "b", "prefix": x} for x in PREFIXES]
    return make_run(project, run_id, deploy=deployed(NAME, SFX), stage="done", log=log, **kw)


def world():
    others = {"workflows/weather.json": "CURATE's", "functions/weather-1106-1000/plot_weather.py": "another run's"}
    fake = FakeGitHub(files={**FILES, **others}, writable=True)
    s3 = Bucket({f"weather_data{SFX}/station.csv", f"weather_data{SFX}/plot.png",
                 f"FaaSrLog/{NAME}/2026-11-05T10-00-00/inv/download.txt",
                 "weather_data/station.csv",                 # CURATE's own outputs
                 "weather_data-1106-1000/station.csv",       # another deployment's
                 f"FaaSrLog/{NAME}-old/x.txt"})              # a longer name with the same start
    return fake, s3


def test_teardown_shows_exactly_what_it_would_delete_and_deletes_nothing_without_yes(project, plain, capsys):
    r = torn(project)
    fake, s3 = world()
    code, out = cli(capsys, "teardown", "--run", str(r), fake=fake, s3=s3)
    assert code == 1 and out["needs_confirmation"] and "--confirmed --plan " + out["plan_id"] in out["error"]
    assert sorted(f["path"] for f in out["repo_files"]) == sorted(FILES)
    assert sorted(o["key"] for o in out["s3_objects"]) == sorted(
        [f"FaaSrLog/{NAME}/2026-11-05T10-00-00/inv/download.txt", f"weather_data{SFX}/plot.png",
         f"weather_data{SFX}/station.csv"])
    assert out["counts"] == {"repo_files": 5, "s3_objects": 3, "log_objects": 1, "output_objects": 2}
    summary = out["summary"]
    deploy_part, logs_part, outputs_part = (summary.split("**Deploy files** (repo):")[1].split("**FaaSr logs**")[0],
                                            summary.split("**FaaSr logs** (S3):")[1].split("**Outputs**")[0],
                                            summary.split("**Outputs** (S3, what the workflow wrote):")[1])
    assert deploy_part.count(f"functions/{NAME}/") == 2 and "weather_data" not in deploy_part
    assert f"s3://b/FaaSrLog/{NAME}/`: 1 object(s)" in logs_part and "weather_data" not in logs_part
    assert f"s3://b/weather_data{SFX}/`: 2 object(s)" in outputs_part and "FaaSrLog" not in outputs_part.split("- Not")[0]
    assert {o["key"]: o["kind"] for o in out["s3_objects"]}[f"weather_data{SFX}/plot.png"] == "outputs"
    assert set(out["outputs_choices"]) == {"keep", "delete", "download"}
    assert "Not deleted: the GitHub Actions run history" in out["summary"]
    assert fake.deletes == [] and s3.deleted == []


def test_teardown_deletes_only_the_listed_files_and_objects(project, plain, capsys):
    r = torn(project)
    fake, s3 = world()
    _, plan = cli(capsys, "teardown", "--run", str(r), fake=fake, s3=s3)
    code, out = cli(capsys, "teardown", "--run", str(r), "--confirmed", "--plan", plan["plan_id"],
                    "--outputs", "delete", fake=fake, s3=s3)
    assert code == 0 and out["ok"], out
    deleted = [p for p, _ in fake.deletes]
    assert sorted(deleted) == sorted(FILES) and deleted[0].startswith(".github/")  # Actions files first
    assert all("(curate-skills teardown)" in b["message"] for _, b in fake.deletes)
    assert set(fake.files) == {"workflows/weather.json", "functions/weather-1106-1000/plot_weather.py"}
    assert s3.keys == {"weather_data/station.csv", "weather_data-1106-1000/station.csv", f"FaaSrLog/{NAME}-old/x.txt"}
    st = json.loads((r / "state.json").read_text())
    assert st["deploy"]["teardown"]["repo_files"] == 5 and st["deploy"]["teardown"]["s3_objects"] == 3
    assert [e["step"] for e in map(json.loads, (r / "deploy" / "deploy-log.jsonl").read_text().splitlines())][-7:] \
        == ["teardown"] * 7
    code, again = cli(capsys, "teardown", "--run", str(r), fake=fake, s3=s3)
    assert code == 1 and "already torn down" in again["error"]
    # a later deploy-only run can no longer reuse it
    b = make_run(project, B, copied_from=A)
    code, status = cli(capsys, "status", "--run", str(b))
    assert status["reuse_possible"] is None


def test_teardown_refuses_without_a_deploy_log(project, plain, capsys):
    r = make_run(project, A, deploy=deployed(NAME, SFX), stage="done")
    code, out = cli(capsys, "teardown", "--run", str(r))
    assert code == 1 and "no deploy log for this run" in out["error"] and "nothing is deleted" in out["error"]


def test_teardown_refuses_when_a_file_changed_since_deploy(project, plain, capsys):
    r = torn(project)
    fake, s3 = world()
    fake.files[f"functions/{NAME}/plot_weather.py"] = "def plot_weather(): edited by hand\n"
    _, plan = cli(capsys, "teardown", "--run", str(r), fake=fake, s3=s3)
    code, out = cli(capsys, "teardown", "--run", str(r), "--confirmed", "--plan", plan["plan_id"], fake=fake, s3=s3)
    assert code == 1 and not out["ok"]
    assert f"functions/{NAME}/plot_weather.py changed since this run deployed it; refusing" in out["problems"]
    assert fake.deletes == [] and s3.deleted == []


def test_teardown_refuses_when_the_list_changed_since_it_was_shown(project, plain, capsys):
    r = torn(project)
    fake, s3 = world()
    _, plan = cli(capsys, "teardown", "--run", str(r), fake=fake, s3=s3)
    s3.keys.add(f"weather_data{SFX}/late.csv")  # written by an invocation after the list was shown
    code, out = cli(capsys, "teardown", "--run", str(r), "--confirmed", "--plan", plan["plan_id"], fake=fake, s3=s3)
    assert code == 1 and "changed since it was shown" in out["error"] and fake.deletes == [] and s3.deleted == []
    code, out = cli(capsys, "teardown", "--run", str(r), "--confirmed", fake=fake, s3=s3)  # no --plan at all
    assert code == 1 and fake.deletes == []


def test_teardown_refuses_a_reuse_run_and_an_unverifiable_old_log(project, plain, capsys):
    torn(project)
    b = make_run(project, B, copied_from=A, deploy=deployed(NAME, SFX, reused_from=A),
                 log=[{"run_id": B, "deploy_name": NAME, "step": "invoke", "bucket": "b", "prefix": PREFIXES[0]}])
    code, out = cli(capsys, "teardown", "--run", str(b))
    assert code == 1 and f"reused run {A}'s deployment; tear that run down instead" in out["error"]
    # a log written before shas were recorded, with no deploy copy to rebuild the files from
    old = torn(project, run_id=A2, shas=False)
    fake, s3 = world()
    code, out = cli(capsys, "teardown", "--run", str(old), fake=fake, s3=s3)
    assert code == 1 and any("content at deploy time is not known" in p for p in out["problems"])


def test_teardown_refuses_a_prefix_another_runs_deployment_records(project, plain, capsys):
    torn(project, run_id=A2)   # an old-style clash: two runs recorded the same S3 folder
    r = torn(project)
    fake, s3 = world()
    code, out = cli(capsys, "teardown", "--run", str(r), fake=fake, s3=s3)
    assert any(f"also recorded by run {A2}'s own deployment" in p for p in out["problems"]) and s3.deleted == []


def test_new_deploy_logs_record_each_files_sha(project, plain, capsys, monkeypatch):
    r = make_run(project, A)
    fake = FakeGitHub(writable=True)
    for step in ("publish", "upload"):
        code, out = cli(capsys, step, "--run", str(r), "--confirmed", fake=fake, s3=Bucket())
        assert out["ok"], out
    log = [json.loads(x) for x in (r / "deploy" / "deploy-log.jsonl").read_text().splitlines()]
    assert [e["path"] for e in log] == [f"functions/{NAME}/download_station.py", f"functions/{NAME}/plot_weather.py",
                                        f"workflows/{NAME}.json"]
    assert all(e["sha"] == D.blob_sha(fake.files[e["path"]]) for e in log)


# ── docs ──

def test_docs_describe_per_run_names_list_and_teardown_and_no_cs_default():
    plugin = Path(__file__).resolve().parents[1]
    readme = " ".join((plugin / "README.md").read_text().split())
    assert "Each deployment is named `<Workflow>-<MMDD-HHMM>` after the run that approved Gate 4" in readme
    assert "`/curate-skills:curate-deploy --list` shows this project's deployments; `--teardown --run <run_id>` " \
           "deletes exactly what that run deployed" in readme
    for f in ("README.md", ".env.template", "skills/curate-deploy/SKILL.md", "references/stages/deploy.md",
              "references/stages/monitor.md", "scripts/doctor.py"):
        assert "-cs" not in (plugin / f).read_text(), f
    skill = " ".join((plugin / "skills" / "curate-deploy" / "SKILL.md").read_text().split())
    assert "`deploy.py teardown --run <run>` (deletes nothing)" in skill
    assert "Ask once with AskUserQuestion" in skill and "--confirmed --plan <plan_id> --outputs keep|delete|download" in skill
    for option in ("**Keep outputs (default)**", "**Delete all**", "**Download outputs, then delete**", "**Cancel**"):
        assert option in skill
    assert "the choice is **Keep outputs**" in skill
    assert "`deploy.py list`; print its `markdown` exactly and stop" in skill


# ── teardown: what happens to the outputs ──

OUT_KEYS = {f"weather_data{SFX}/station.csv", f"weather_data{SFX}/plot.png"}
LOG_KEY = f"FaaSrLog/{NAME}/2026-11-05T10-00-00/inv/download.txt"


def confirmed(capsys, r, fake, s3, *extra):
    _, plan = cli(capsys, "teardown", "--run", str(r), fake=fake, s3=s3)
    return cli(capsys, "teardown", "--run", str(r), "--confirmed", "--plan", plan["plan_id"], *extra, fake=fake, s3=s3)


def test_keep_outputs_is_the_default(project, plain, capsys):
    r = torn(project)
    fake, s3 = world()
    code, out = confirmed(capsys, r, fake, s3)  # no --outputs: e.g. asked non-interactively
    assert code == 0 and out["outputs"] == "keep"
    assert sorted(p for p, _ in fake.deletes) == sorted(FILES)
    assert s3.deleted == [LOG_KEY] and OUT_KEYS <= s3.keys
    assert "Outputs kept (2 object(s)): s3://b/weather_data-1105-0930/" in out["message"]
    td = json.loads((r / "state.json").read_text())["deploy"]["teardown"]
    assert td["outputs"] == "keep" and td["outputs_kept"] == [f"s3://b/weather_data{SFX}/"] and td["s3_objects"] == 1


def test_download_outputs_first_then_delete_everything(project, plain, capsys, tmp_path):
    r = torn(project)
    fake, s3 = world()
    s3.content[f"weather_data{SFX}/station.csv"] = "date,prcp\n"
    code, out = confirmed(capsys, r, fake, s3, "--outputs", "download")
    assert code == 0 and out["ok"], out
    dest = project / "teardown-outputs" / NAME  # default: in the project, outside the run
    assert out["downloaded"]["to"] == str(dest.resolve()) and out["downloaded"]["files"] == 2
    assert (dest / f"weather_data{SFX}" / "station.csv").read_text() == "date,prcp\n"
    assert len(json.loads((dest / "manifest.json").read_text())["files"]) == 2
    assert set(s3.deleted) == OUT_KEYS | {LOG_KEY} and "Outputs saved first to" in out["message"]
    assert json.loads((r / "state.json").read_text())["deploy"]["teardown"]["outputs_saved_to"] == str(dest.resolve())


def test_download_that_cannot_save_every_output_deletes_nothing(project, plain, capsys, tmp_path):
    r = torn(project)
    fake, s3 = world()
    s3.content[f"weather_data{SFX}/station.csv"] = f"key,{FAKE['S3_SecretKey']}\n"  # holds a secret value
    code, out = confirmed(capsys, r, fake, s3, "--outputs", "download", "--to", str(tmp_path / "saved"))
    assert code == 1 and "nothing was deleted" in out["error"] and "S3_SecretKey" in out["error"]
    assert fake.deletes == [] and s3.deleted == [] and FAKE["S3_SecretKey"] not in json.dumps(out)
    full = tmp_path / "full"
    full.mkdir()
    (full / "mine.txt").write_text("x")
    code, out = confirmed(capsys, r, fake, s3, "--outputs", "download", "--to", str(full))
    assert code == 1 and "is not empty" in out["error"] and fake.deletes == [] and s3.deleted == []
    code, out = confirmed(capsys, r, fake, s3, "--outputs", "download", "--to", str(r / "x"))
    assert code == 1 and "inside the run folder" in out["error"] and s3.deleted == []
