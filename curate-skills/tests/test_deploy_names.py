"""Deployment names (one per version), --list and --teardown (fakes only: nothing reaches GitHub or S3).

Runs deployed before one deployment per version (date-suffixed per-run names) stay readable and can
still be torn down: the legacy teardown tests below use such runs."""
import json
from pathlib import Path

import pytest

import deploy as D
import ids
from versioned import RID, versioned_bundle
from test_deploy import FAKE, FakeGitHub, env, workflow  # noqa: F401  (env is a fixture)

A = "20261105-093012-weather"   # approved Gate 4 at 09:30 on 5 Nov
A2 = "20261105-093055-weather"  # same minute
B = "20261106-140000-weather"   # deploy-only from A
C = "20261107-080000-weather"   # deploy-only from B
OLD = "20261101-172823-weather"  # deployed before per-run names: "-cs"


@pytest.fixture
def plain(env, monkeypatch):
    monkeypatch.delenv("CURATE_DEPLOY_NAME_SUFFIX", raising=False)  # no suffix: the name is the workflow id
    return monkeypatch


BUNDLE = {"workflow.json": json.dumps(workflow(), indent=2), "functions/plot_weather.py": "def plot_weather(): pass\n",
          "functions/download_station.py": "def download_station(): pass\n"}


@pytest.fixture
def project(tmp_path):
    versioned_bundle(tmp_path, BUNDLE)
    return tmp_path


def make_run(project, run_id, copied_from=None, deploy=None, stage="deploy", log=None, rid=RID, **extra):
    """A run of weather-v1 whose execution (run id `rid`) built the bundle (tests/versioned.py)."""
    fields, _ = versioned_bundle(project, BUNDLE, rid=rid)
    h = fields["execution"]["bundle_hash"]
    r = project / ".curate" / "runs" / run_id
    (r / "deploy").mkdir(parents=True)
    hist = [{"gate": 4, "decision": "copied", "from_run": copied_from,
             "snapshot": "005-gate4-approve.json"}] if copied_from \
        else [{"gate": 4, "decision": "approve"}]
    st = {"schema_version": 2, "run_id": run_id, "stage": stage, **fields,
          "deploy_choice": "yes", "history": hist, **extra}
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


# ── naming: one deployment per version ──

V = "weather-v1"


def test_the_name_is_the_versions_workflow_id_for_every_run(project, plain, capsys):
    a = make_run(project, A)
    b = make_run(project, B, copied_from=A, rid="20261106-140000-beef")  # deploy-only, its own run id
    for r, rid in ((a, RID), (b, "20261106-140000-beef")):
        code, out = cli(capsys, "preflight", "--run", str(r))
        assert code == 0 and out["ok"], out["problems"]
        assert out["deploy_name"] == V and out["suffix"] == "" and out["name_source"] == "the version's workflow id"
        assert out["s3_targets"][0]["prefix"] == f"{V}/{rid}/" and out["payload"] == f"workflows/{V}/{rid}.json"
        assert f"Deploys as: `{V}` (name from: the version's workflow id), run `{rid}`" in out["summary"]


def test_a_suffix_names_another_deployment_of_the_same_version(project, plain, capsys):
    r = make_run(project, A)
    plain.setenv("CURATE_DEPLOY_NAME_SUFFIX", "-alice")
    code, out = cli(capsys, "preflight", "--run", str(r))
    assert out["ok"] and out["deploy_name"] == f"{V}-alice"
    assert out["name_source"] == "the version's workflow id + CURATE_DEPLOY_NAME_SUFFIX / --name-suffix"


def test_curates_plain_deployment_is_left_alone(project, plain, capsys):
    curate = {"workflows/weather.json": json.dumps({"WorkflowName": "weather"}),
              ".github/workflows/weather-plot.yml": "name: weather-plot\n"}
    r = make_run(project, A)
    code, out = cli(capsys, "preflight", "--run", str(r), fake=FakeGitHub(files=curate))
    assert out["ok"] and out["curate_files"] == ["workflows/weather.json", ".github/workflows/weather-plot.yml"]


def test_the_plain_workflow_name_is_never_a_deploy_name(project, plain):
    r = make_run(project, A)
    real = D.deploy_copy

    def same_name(wf, name, *a):  # a deploy copy that kept the plain name (defence in depth)
        copy_wf, targets, s3_targets = real(wf, name, *a)
        return {**copy_wf, "WorkflowName": wf["WorkflowName"]}, targets, s3_targets
    import unittest.mock as m
    with m.patch.object(D, "deploy_copy", same_name):
        pf = D.preflight(str(r), D.deploy_name(r), s3_factory=Bucket())
    assert any("is the workflow's own name, which CURATE's deployment uses" in p for p in pf["problems"])


def test_old_style_runs_stay_readable(project, plain, capsys):
    old = make_run(project, OLD, deploy=deployed("weather-cs", "-cs"), stage="done")
    code, out = cli(capsys, "status", "--run", str(old))
    assert out["ok"] and out["deploy_name"] == "weather-cs" and out["per_run_name"] and out["next_step"] is None


# ── list ──

def version_deployed(rid, deployment="made", **extra):
    d = deployed(V, "", scheme="version", deployment=deployment, payload=f"workflows/{V}/{rid}.json", **extra)
    return d


def test_list_shows_each_deployment_once_with_its_runs(project, plain, capsys, monkeypatch):
    make_run(project, OLD, deploy=deployed("weather-cs", "-cs"), stage="done")
    make_run(project, A, deploy=deployed("weather-1105-0930", "-1105-0930"), stage="done")
    reused = deployed("weather-1105-0930", "-1105-0930", reused_from=A)
    reused["steps"]["invoke"]["ts"] = "2026-11-06T14:05:00+00:00"
    make_run(project, B, copied_from=A, deploy=reused, stage="done")
    make_run(project, "20261107-090000-weather", deploy=deployed(
        "weather-1107-0900", "-1107-0900", teardown={"ts": "2026-11-08T00:00:00+00:00"}), stage="done")
    make_run(project, C)  # never deployed: not listed
    # one deployment per version, made by the first run; the other runs' statuses
    marker = {"projects": ["p-1"], "deployed_at": "2026-11-05T10:01:00+00:00", "seen": "2026-11-05T10:01:00+00:00"}
    ra, rb, rc, rd = (f"20261110-0{h}0000-{x}" for h, x in ((9, "aaaa"), (8, "bbbb"), (7, "cccc"), (6, "dddd")))
    make_run(project, ra, rid=ra, deploy=version_deployed(
        ra, teardown={"ts": "2026-11-11T00:00:00+00:00"}, marker=marker), stage="done")
    make_run(project, rb, rid=rb, deploy=version_deployed(rb, "reused", marker={
        **marker, "projects": ["p-1", "p-2"], "seen": "2026-11-06T00:00:00+00:00"}),
        monitor={"finished": True, "result": "success"}, stage="done")
    make_run(project, rc, rid=rc, deploy=version_deployed(rc, "reused"), monitor={"result": "pending"}, stage="monitor")
    rd_dep = version_deployed(rd, "reused")
    del rd_dep["steps"]["invoke"]
    make_run(project, rd, rid=rd, deploy=rd_dep, stage="deploy")
    monkeypatch.chdir(project)
    code, out = cli(capsys, "list")
    assert code == 0
    rows = {r["name"]: r for r in out["deployments"]}
    assert set(rows) == {"weather-cs", "weather-1105-0930", "weather-1107-0900", V}
    # legacy, date-suffixed: by the session that made it
    a = rows["weather-1105-0930"]
    assert a["scheme"] == "legacy" and a["session"] == A and a["also_invoked_by"] == [B]
    assert a["deployed"] == "2026-11-05T10:00:00+00:00" and len(a["runs"]) == 2
    assert rows["weather-1107-0900"]["torn_down"] == "2026-11-08T00:00:00+00:00"
    # the version's deployment, not attributed to a run, with every run underneath
    v = rows[V]
    assert v["scheme"] == "version" and v["torn_down"] is None  # a run's teardown leaves the deployment
    assert v["deployed"] == "2026-11-05T10:01:00+00:00" and v["projects"] == ["p-1", "p-2"]
    status = {x["run_id"]: x["status"] for x in v["runs"]}
    assert status == {ra: "torn down", rb: "finished", rc: "active", rd: "not invoked"}
    assert set(D.RUN_STATUSES) == set(status.values())
    blocks = out["markdown"].split("\n\n")
    assert blocks[1].splitlines() == [
        "**`weather-1105-0930`**", "- Repo: alice/faasr-actions", "- Deployed: 2026-11-05 10:00 UTC",
        f"- Session: {A}", "- Last invoked: 2026-11-06 14:05 UTC (2 invocation(s))", f"- Also invoked by: {B}"]
    assert blocks[2].splitlines()[0] == "**`weather-1107-0900`** — torn down"
    assert blocks[2].splitlines()[-1] == "- Torn down: 2026-11-08 00:00 UTC" and "|" not in out["markdown"]
    vb = next(b for b in blocks if b.startswith(f"**`{V}`**")).splitlines()
    assert vb[:5] == [f"**`{V}`**", "- Repo: alice/faasr-actions", "- Deployed: 2026-11-05 10:01 UTC",
                      "- Projects: p-1, p-2 (as of 2026-11-06 00:00 UTC)", "- Runs:"]
    run_lines = {x: next(line for line in vb if x in line) for x in (ra, rb, rc, rd)}
    assert run_lines[ra].endswith("— torn down 2026-11-11 00:00 UTC · invocation `inv`, 2026-11-05 10:03 UTC")
    assert "— finished · invocation `inv`" in run_lines[rb] and "— active · invocation `inv`" in run_lines[rc]
    assert run_lines[rd].endswith("— not invoked")


def test_list_after_teardown_all_and_without_a_recorded_marker(project, plain, capsys):
    ra = "20261110-090000-aaaa"
    gone = {"ts": "2026-11-12T00:00:00+00:00"}
    make_run(project, ra, rid=ra, deploy=version_deployed(ra, teardown=gone, deployment_teardown=gone), stage="done")
    code, out = cli(capsys, "list", "--project", str(project))
    v = out["deployments"][0]
    assert v["torn_down"] == gone["ts"] and v["projects"] is None and v["runs"][0]["status"] == "torn down"
    assert out["markdown"].splitlines()[0] == f"**`{V}`** — torn down"
    assert "- Projects: not recorded (deployed before this was kept)" in out["markdown"]


def test_list_with_no_runs(tmp_path, capsys):
    code, out = cli(capsys, "list", "--project", str(tmp_path))
    assert code == 0 and out["deployments"] == [] and "No deployments" in out["markdown"]


# ── teardown of a run deployed under its own per-run name (before one deployment per version) ──

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
    assert [e["path"] for e in log] == [f"functions/{V}/download_station.py", f"functions/{V}/plot_weather.py",
                                        f"workflows/{V}/{RID}.json"]
    assert all(e["sha"] == D.blob_sha(fake.files[e["path"]]) for e in log)


# ── docs ──

def test_docs_describe_version_names_list_and_teardown_and_no_cs_default():
    plugin = Path(__file__).resolve().parents[1]
    readme = " ".join((plugin / "README.md").read_text().split())
    assert "A workflow version is deployed once, named after its workflow id (`<workflow-id>`)" in readme
    assert "`/curate-skills:curate-deploy --list` shows this project's deployments, each version's with its runs " \
           "underneath (active, finished, not invoked or torn down)" in readme
    assert "`--teardown --run <workflow-id>/<run-id>` deletes that run's payload, outputs and logs" in readme
    assert "MMDD" not in readme
    for f in ("README.md", ".env.template", "skills/curate-deploy/SKILL.md", "references/stages/deploy.md",
              "references/stages/monitor.md", "scripts/doctor.py"):
        assert "-cs" not in (plugin / f).read_text(), f
    skill = " ".join((plugin / "skills" / "curate-deploy" / "SKILL.md").read_text().split())
    assert "`deploy.py teardown <target>` (deletes nothing)" in skill and "--teardown --workflow <workflow-id> --all" in skill
    assert "Ask once with AskUserQuestion" in skill and "--confirmed --plan <plan_id> --outputs keep|delete|download" in skill
    assert "Never pass `--confirmed` or `--other-projects` otherwise" in skill
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


# ── one deployment per version: a run's teardown, and --workflow <wid> --all ──

RA, RB = "20261110-090000-aaaa", "20261110-100000-bbbb"
FNS_V = [f"functions/{V}/download_station.py", f"functions/{V}/plot_weather.py"]
ACT_V = [f".github/workflows/{V}-download.yml", f".github/workflows/{V}-plot.yml"]
MARK = f"workflows/{V}/deployment.json"
TS = "2026-11-10-09-01-00"


def payload(rid):
    return f"workflows/{V}/{rid}.json"


def vrun(project, rid, deployment="made", finished=True, **kw):
    """A run of weather-v1 that deployed (one deployment per version) and was monitored to the end."""
    log = [{"run_id": rid, "deploy_name": V, "step": "upload", "repo": "alice/faasr-actions", "branch": "main",
            "path": payload(rid), "sha": D.blob_sha(json.dumps({"WorkflowName": V, "run": rid}))},
           {"run_id": rid, "deploy_name": V, "step": "invoke", "bucket": "b", "prefix": f"{V}/{rid}/"},
           {"run_id": rid, "deploy_name": V, "step": "invoke", "bucket": "b", "prefix": f"FaaSrLog/{V}/{TS}/inv-{rid}/"}]
    if deployment == "made":
        log = [{"run_id": rid, "deploy_name": V, "step": "publish", "repo": "alice/faasr-actions", "branch": "main",
                "path": p, "sha": D.blob_sha(f"code of {p}")} for p in FNS_V] + log
    r = make_run(project, rid, rid=rid, stage="done", log=log,
                 deploy=version_deployed(rid, deployment, invocation_timestamp=TS),
                 monitor={"finished": finished, "result": "success" if finished else "pending"}, **kw)
    copy_wf = D.deploy_copy(workflow(), V, rid, "alice", "faasr-actions", "main")[0]  # what upload kept
    (r / "deploy" / "workflow.json").write_text(json.dumps(copy_wf))
    return r


def vworld(project, projects=None):
    vdir = ids.version_dir(project, V)
    deployment = {p: f"code of {p}" for p in FNS_V + ACT_V}
    marker = {"curate_deployment": 1, "deploy_name": V, "workflow_id": V, "content_hash": ids.draft_content_hash(vdir),
              "files": {p: D.blob_sha(c) for p, c in deployment.items()},
              "projects": projects or [ids.project_id(project)]}
    files = {**deployment, MARK: json.dumps(marker),
             payload(RA): json.dumps({"WorkflowName": V, "run": RA}), payload(RB): json.dumps({"WorkflowName": V, "run": RB}),
             "workflows/weather.json": "CURATE's", "workflows/weather-v2/x.json": json.dumps({"WorkflowName": "weather-v2"})}
    s3 = Bucket({f"{V}/{RA}/station.csv", f"{V}/{RA}/plot.png", f"{V}/{RB}/station.csv",
                 f"FaaSrLog/{V}/{TS}/inv-{RA}/download.txt", f"FaaSrLog/{V}/{TS}/inv-{RB}/download.txt",
                 "weather_data/station.csv", f"{V}-alice/{RA}/x.csv", "weather-v2/r/x.csv"})
    return FakeGitHub(files=files, writable=True), s3


def test_a_runs_teardown_removes_only_its_payload_outputs_and_logs(project, plain, capsys):
    a, b = vrun(project, RA), vrun(project, RB, "reused")
    fake, s3 = vworld(project)
    code, plan = cli(capsys, "teardown", "--run", str(b), fake=fake, s3=s3)
    assert code == 1 and plan["needs_confirmation"], plan.get("problems")
    assert [f["path"] for f in plan["repo_files"]] == [payload(RB)]
    assert sorted(o["key"] for o in plan["s3_objects"]) == [f"FaaSrLog/{V}/{TS}/inv-{RB}/download.txt", f"{V}/{RB}/station.csv"]
    assert plan["summary"].startswith(f"**Teardown of run `{RB}` of `{V}`**")
    assert f"Not deleted: the version's deployment `{V}`" in plan["summary"] and "--all removes it" in plan["summary"]
    code, out = cli(capsys, "teardown", "--run", str(b), "--confirmed", "--plan", plan["plan_id"], "--outputs", "delete",
                    fake=fake, s3=s3)
    assert code == 0 and out["ok"], out
    assert [p for p, _ in fake.deletes] == [payload(RB)]
    assert all(p in fake.files for p in FNS_V + ACT_V + [MARK, payload(RA)])  # the deployment and run A stay
    assert sorted(s3.deleted) == [f"FaaSrLog/{V}/{TS}/inv-{RB}/download.txt", f"{V}/{RB}/station.csv"]
    assert b.is_dir() and (b / "state.json").is_file()  # the local run folder stays


def test_a_runs_teardown_can_save_its_outputs_into_its_own_outputs_folder(project, plain, capsys):
    b = vrun(project, RB, "reused")
    fake, s3 = vworld(project)
    s3.content[f"{V}/{RB}/station.csv"] = "date,prcp\n"
    _, plan = cli(capsys, "teardown", "--run", str(b), fake=fake, s3=s3)
    code, out = cli(capsys, "teardown", "--run", str(b), "--confirmed", "--plan", plan["plan_id"], "--outputs", "download",
                    fake=fake, s3=s3)
    assert code == 0 and out["ok"], out
    # as download-outputs --all fills it: every object under _all/, without the <name>/<run id>/ prefix
    assert (b / "outputs" / "_all" / "station.csv").read_text() == "date,prcp\n"
    assert not (b / "outputs" / "station.csv").exists()
    assert out["downloaded"]["to"] == str((b / "outputs").resolve())
    m = json.loads((b / "outputs" / "manifest.json").read_text())
    assert m["invocation_id"] == json.loads((b / "state.json").read_text())["deploy"]["invocation_id"]
    assert m["target"] == "faasr" and m["by"] == "teardown"


def test_all_lists_the_deployment_and_every_runs_files_and_deletes_nothing_without_yes(project, plain, capsys,
                                                                                         monkeypatch):
    vrun(project, RA), vrun(project, RB, "reused")
    fake, s3 = vworld(project)
    monkeypatch.chdir(project)
    code, plan = cli(capsys, "teardown", "--workflow", V, "--all", fake=fake, s3=s3)
    assert code == 1 and plan.get("needs_confirmation"), (plan.get("problems"), plan.get("error"))
    kinds = {f["path"]: f["kind"] for f in plan["repo_files"]}
    assert kinds == {**{p: "deployment" for p in FNS_V + ACT_V}, payload(RA): "payload", payload(RB): "payload",
                     MARK: "marker"}
    assert sorted(o["key"] for o in plan["s3_objects"]) == sorted(
        [f"{V}/{RA}/station.csv", f"{V}/{RA}/plot.png", f"{V}/{RB}/station.csv",
         f"FaaSrLog/{V}/{TS}/inv-{RA}/download.txt", f"FaaSrLog/{V}/{TS}/inv-{RB}/download.txt"])
    assert f"{V}-alice/{RA}/x.csv" not in json.dumps(plan)  # a suffixed deployment is another one
    assert "their local folders and outputs/ stay" in plan["summary"] and plan["other_projects"] == []
    assert fake.deletes == [] and s3.deleted == []


def test_all_deletes_everything_listed_but_never_the_local_run_folders(project, plain, capsys, monkeypatch):
    a, b = vrun(project, RA), vrun(project, RB, "reused")
    (a / "outputs").mkdir()
    (a / "outputs" / "plot.png").write_text("png")
    fake, s3 = vworld(project)
    monkeypatch.chdir(project)
    _, plan = cli(capsys, "teardown", "--workflow", V, "--all", fake=fake, s3=s3)
    code, out = cli(capsys, "teardown", "--workflow", V, "--all", "--confirmed", "--plan", plan["plan_id"],
                    "--outputs", "delete", fake=fake, s3=s3)
    assert code == 0 and out["ok"], out
    deleted = [p for p, _ in fake.deletes]
    assert deleted[:2] == ACT_V and deleted[-1] == MARK  # Actions files first, the marker last
    assert sorted(deleted) == sorted(FNS_V + ACT_V + [payload(RA), payload(RB), MARK])
    assert set(fake.files) == {"workflows/weather.json", "workflows/weather-v2/x.json"}
    assert s3.keys == {"weather_data/station.csv", f"{V}-alice/{RA}/x.csv", "weather-v2/r/x.csv"}
    assert (a / "outputs" / "plot.png").read_text() == "png" and (b / "state.json").is_file()
    for r in (a, b):
        dep = json.loads((r / "state.json").read_text())["deploy"]
        assert dep["deployment_teardown"]["repo_files"] == 7 and dep["teardown"]
    code, out = cli(capsys, "list", "--project", str(project))
    assert {r["name"]: r for r in out["deployments"]}[V]["torn_down"]


def test_all_refuses_while_a_run_of_the_version_is_in_flight(project, plain, capsys, monkeypatch):
    vrun(project, RA)
    b = vrun(project, RB, "reused", finished=False)
    fake, s3 = vworld(project)
    monkeypatch.chdir(project)
    code, out = cli(capsys, "teardown", "--workflow", V, "--all", "--confirmed", "--plan", "x", fake=fake, s3=s3)
    msg = next(p for p in out["problems"] if "may still be running" in p)
    assert f"{V}/{RB}" in msg and f"{V}/{RA}" not in msg and "curate-monitor" in msg
    assert f"For a run that is stuck or timed out, tear that run down first: " \
           f"/curate-skills:curate-deploy --teardown --run {V}/{RB}" in msg
    assert code == 1 and fake.deletes == [] and s3.deleted == []
    json.loads((b / "state.json").read_text())  # untouched


def test_all_warns_and_needs_a_separate_yes_when_other_projects_use_it(project, plain, capsys, monkeypatch):
    vrun(project, RA), vrun(project, RB, "reused")
    fake, s3 = vworld(project, projects=[ids.project_id(project), "0123456789abcdef"])
    monkeypatch.chdir(project)
    code, plan = cli(capsys, "teardown", "--workflow", V, "--all", fake=fake, s3=s3)
    assert plan["other_projects"] == ["0123456789abcdef"] and "--other-projects" in plan["error"]
    assert "**Warning:** the deployment's marker lists 1 other project(s)" in plan["summary"]
    code, out = cli(capsys, "teardown", "--workflow", V, "--all", "--confirmed", "--plan", plan["plan_id"],
                    fake=fake, s3=s3)
    assert code == 1 and out["needs_confirmation"] and "other projects" in out["error"] and fake.deletes == []
    code, out = cli(capsys, "teardown", "--workflow", V, "--all", "--confirmed", "--plan", plan["plan_id"],
                    "--other-projects", fake=fake, s3=s3)
    assert code == 0 and out["ok"] and MARK in [p for p, _ in fake.deletes]


@pytest.mark.parametrize("case,needle", [
    ("other_version", "made from another version"),
    ("changed", "changed since the deployment was written"),
    ("no_marker", "nothing is known to be weather-v1's deployment"),
    ("foreign_payload", "is not a payload of 'weather-v1'"),
])
def test_all_refuses_what_it_cannot_prove_is_this_versions(project, plain, capsys, monkeypatch, case, needle):
    vrun(project, RA)
    fake, s3 = vworld(project)
    if case == "other_version":
        fake.files[MARK] = json.dumps({**json.loads(fake.files[MARK]), "content_hash": "f" * 64})
    if case == "changed":
        fake.files[FNS_V[1]] = "edited by hand"
    if case == "no_marker":
        del fake.files[MARK]
    if case == "foreign_payload":
        fake.files[f"workflows/{V}/stray.json"] = json.dumps({"WorkflowName": "someone-else"})
    monkeypatch.chdir(project)
    code, out = cli(capsys, "teardown", "--workflow", V, "--all", "--confirmed", "--plan", "x", fake=fake, s3=s3)
    assert code == 1 and any(needle in p for p in out["problems"]), out["problems"]
    assert fake.deletes == [] and s3.deleted == []


def test_teardown_takes_a_run_or_a_version_with_all(capsys):
    code, out = cli(capsys, "teardown")
    assert code == 2 and "--workflow <workflow-id> --all" in out["error"]
    code, out = cli(capsys, "teardown", "--workflow", V)
    assert code == 2


# ── update-deployment's main() check, on a real published version ──

from test_flags import deployed as approved_session  # noqa: E402,F401  (fixture: Gate 4 published a version)
from test_start import source_run, state  # noqa: E402,F401  (fixtures)


def test_every_main_is_checked_against_the_versions_code(approved_session):  # noqa: F811
    s = state(approved_session)
    project, wid = Path(s["project_dir"]), s["workflow_id"]
    built, vs, vdir = D._version_build(project, wid)  # from the version's approved.json, in memory
    assert D.main_problems(built, vdir, vs) == []
    main = next(k for k in built["files"] if k.endswith("_main.py"))
    built["files"][main] += "# not the version's code\n"
    assert D.main_problems(built, vdir, vs) == [
        f"{main} is not byte-identical to the version's " + next(
            str(st.function["code"]) for sid, st in __import__("execution").load_workflow(vdir, vs).steps.items()
            if main.endswith(f"{sid}_main.py"))]


# ── a session from before runs kept their own state, linking a run folder without state.json ──

def legacy_shape(project):
    """As on a real project made before execution runs had their own state: the session holds the
    FaaSr records (deployed, invoked once, not torn down), and its execution folder (workflows/<wid>/
    runs/<rid>/) has only bundle/ and manifest.json."""
    r = torn(project)
    ex = ids.execution_dir(project, V, RID)
    assert sorted(x.name for x in ex.iterdir()) == ["bundle", "manifest.json"]
    return r, ex


@pytest.mark.parametrize("how", ["session id", "wid/rid", "run folder", "run id"])
def test_a_pre_change_session_is_torn_down_by_any_name_of_it(project, plain, capsys, monkeypatch, how):
    r, ex = legacy_shape(project)
    monkeypatch.chdir(project)
    ref = {"session id": A, "wid/rid": f"{V}/{RID}", "run folder": str(ex), "run id": RID}[how]
    fake, s3 = world()
    code = D.main(["teardown", "--run", ref], opener=fake, s3_factory=s3)
    cap = capsys.readouterr()
    plan = json.loads(cap.out)
    assert code == 1 and plan.get("needs_confirmation"), plan
    assert sorted(f["path"] for f in plan["repo_files"]) == sorted(FILES)
    assert A in cap.err and ("from before runs kept their own state" in cap.err or "using its session" in cap.err)
    code = D.main(["teardown", "--run", ref, "--confirmed", "--plan", plan["plan_id"], "--outputs", "delete"],
                  opener=fake, s3_factory=s3)
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["ok"], out
    assert json.loads((r / "state.json").read_text())["deploy"]["teardown"]["repo_files"] == 5
    assert not (ex / "state.json").exists()  # the run folder is left as it was


def test_status_and_monitor_given_the_pre_change_session_use_the_session(project, plain, capsys, monkeypatch):
    import monitor as MON
    legacy_shape(project)
    monkeypatch.chdir(project)
    for mod in (D, MON):
        outs = []
        for ref in (A, f"{V}/{RID}"):
            mod.main(["status", "--run", ref])
            cap = capsys.readouterr()
            outs.append(json.loads(cap.out))
            assert A in cap.err  # the note names the session used
        assert outs[0] == outs[1] and outs[0]["ok"], outs
    assert outs[0] != {} and D.deploy_status(str(project / ".curate" / "runs" / A))["deploy_name"] == NAME


def _runs(project):
    import argparse
    import spec as S
    return S.cmd_runs(argparse.Namespace(project_dir=str(project)))


def test_runs_lists_a_pre_change_deployment_by_its_session_and_marks_torn_down_runs(project, plain, capsys):
    legacy_shape(project)
    # a run with its own state, deployed and torn down (monitor said success before that)
    done = ids.execution_dir(project, V, "20261126-214030-cb91")
    done.mkdir(parents=True)
    (done / "state.json").write_text(json.dumps({
        "schema_version": 2, "kind": "execution", "run_id": "20261126-214030-cb91", "reference": f"{V}/20261126-214030-cb91",
        "workflow_id": V, "stage": "done", "session": None, "execution": {"target": "faasr"},
        "monitor": {"result": "success"},
        "deploy": {**deployed(V, ""), "teardown": {"ts": "2026-11-26T22:00:00+00:00"}}}))
    out = _runs(project)
    ses = next(x for x in out["sessions"] if x["session_id"] == A)
    assert ses["run"] is None  # its run folder has no state of its own: the session is named
    assert ses["deploy"] == {"name": NAME, "invoked": True, "torn_down": None, "teardown_run": A}
    gone = next(x for x in out["runs"] if x["run_id"] == "20261126-214030-cb91")
    assert gone["deploy"]["torn_down"] == "2026-11-26T22:00:00+00:00" and gone["deploy"]["teardown_run"] is None


def test_a_run_with_no_faasr_deployment_says_so_and_names_whose_it_is(project, plain, capsys, monkeypatch):
    legacy_shape(project)
    peg_rid = "20261125-090000-peg1"
    make_run(project, "20261125-085900-pegs", rid=peg_rid, stage="done", deploy_choice="pegasus",
             pegasus={"steps": {"submit": {"ok": True}}})
    monkeypatch.chdir(project)
    code, out = cli(capsys, "teardown", "--run", f"{V}/{peg_rid}")
    assert code == 1 and "has no FaaSr deployment (it ran on Pegasus)" in out["error"], out
    assert f"{NAME} (the session {A}'s: teardown --run {A})" in out["error"]
    assert "state.json" not in out["error"]


def test_a_folder_that_is_not_a_run_says_so(project, plain, capsys, tmp_path):
    stray = tmp_path / "stray"
    stray.mkdir()
    code, out = cli(capsys, "teardown", "--run", str(stray))
    assert code == 1 and "is not a run" in out["error"] and "no session links it" in out["error"]


def test_teardown_download_into_the_runs_outputs_places_deliverables_like_download_outputs(tmp_path, monkeypatch):
    s3 = Bucket([f"{V}/{RB}/reduce__output1__1__wc_1.json", f"{V}/{RB}/map__output1__1__m_1.json"])
    for k in s3.keys:
        s3.content[k] = k.rsplit("/", 1)[1]
    monkeypatch.setattr(D, "_s3_client", lambda wf, bucket, factory: s3)
    dest = tmp_path / "outputs"
    objs = [{"bucket": "b", "key": k} for k in sorted(s3.keys)]
    got = D._download({}, objs, dest, None, None, f"{V}/{RB}/",
                      {f"{V}/{RB}/reduce__output1__1__wc_1.json": "results/wc_1.json"},
                      {"run": RB, "target": "faasr", "invocation_id": "inv"})
    assert got["files"] == 2 and got["skipped"] == []
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == [
        "_all/map__output1__1__m_1.json", "_all/reduce__output1__1__wc_1.json", "manifest.json", "results/wc_1.json"]
    assert json.loads((dest / "manifest.json").read_text())["invocation_id"] == "inv"


def test_the_teardown_question_names_what_it_removes(project, plain, capsys, monkeypatch):
    vrun(project, RA)
    b = vrun(project, RB, "reused")
    fake, s3 = vworld(project)
    _, plan = cli(capsys, "teardown", "--run", str(b), fake=fake, s3=s3)
    assert plan["question"] == (f"Tear down run {V}/{RB}? 1 repo files, 1 log objects and 1 output objects "
                                "are listed above.")
    monkeypatch.chdir(project)
    _, plan = cli(capsys, "teardown", "--workflow", V, "--all", fake=fake, s3=s3)
    assert plan["question"].startswith(f"Tear down deployment {V} and all its runs? ")
    skill = (Path(__file__).resolve().parents[1] / "skills" / "curate-deploy" / "SKILL.md").read_text()
    assert "question: the returned `question`, exactly" in skill and "Tear down <deploy_name>?" not in skill


def test_a_legacy_runs_teardown_question_names_its_deployment(project, plain, capsys):
    fake, s3 = world()
    code, plan = cli(capsys, "teardown", "--run", str(torn(project)), fake=fake, s3=s3)
    assert plan["question"] == (f"Tear down deployment {NAME} (session {A})? 5 repo files, 1 log objects and "
                                "2 output objects are listed above.")
