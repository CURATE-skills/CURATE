"""D0: vendored deploy/runtime code, leak checks and the read-only deploy preflight."""
import base64
import io
import json
import sys
import types
import urllib.error
from pathlib import Path

import pytest

import deploy as D
import leakcheck as L

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
VENDOR = SCRIPTS / "vendor"
ORIG = Path(__file__).resolve().parents[2] / "ref" / "curate" / "src" / "faasr_agents"

FAKE = {"GH_PAT": "ghp_" + "A" * 36, "S3_AccessKey": "AKIA" + "B" * 16, "S3_SecretKey": "s3cr3t-" + "c" * 33}


# ── vendored code: only the listed edits ──

VENDORED = {
    "curate_deploy/register_workflow.py": "deploy/register_workflow.py",
    "curate_deploy/invoke_workflow.py": "deploy/invoke_workflow.py",
    "faasr_runtime/enums.py": "faasr/runtime/enums.py",
    "faasr_runtime/utils.py": "faasr/runtime/utils.py",
    "faasr_runtime/s3_client.py": "faasr/runtime/s3_client.py",
    "faasr_runtime/faasr_function.py": "faasr/runtime/faasr_function.py",
    "faasr_runtime/faasr_function_logger.py": "faasr/runtime/faasr_function_logger.py",
    "faasr_runtime/workflow_runner.py": "faasr/runtime/workflow_runner.py",
    "faasr_runtime/artifacts.py": "faasr/artifacts.py",
    "curate_deploy/native_containers.txt": "deploy/native_containers.txt",
}
ALLOWED_EDITS = {  # (original line, vendored line)
    ("from faasr_agents.faasr.validate import validate_faasr_json", "from faasr_validate import validate_faasr_json"),
    ("    stream=sys.stdout,", "    stream=sys.stderr,"),
}


@pytest.mark.skipif(not ORIG.is_dir(), reason="ref/curate checkout not present")
@pytest.mark.parametrize("ours,theirs", sorted(VENDORED.items()))
def test_vendored_files_differ_only_by_listed_edits(ours, theirs):
    mine = (VENDOR / ours).read_text().splitlines()
    if ours.endswith(".py"):
        assert mine[0].startswith("# Copyright (c) 2026 Nolan Cutler. MIT License")
        assert f"src/faasr_agents/{theirs}" in mine[1] and "3fd5c16" in mine[2]
        mine = mine[3:]
    orig = (ORIG / theirs).read_text().splitlines()
    assert len(mine) == len(orig)
    for a, b in zip(orig, mine):
        if a != b:
            ok = (a, b) in ALLOWED_EDITS or (a.replace("from faasr_agents.faasr.runtime.", "from .") == b)
            assert ok, (a, b)


def _stub_modules(monkeypatch):
    for name in ("boto3", "requests", "github", "faasr_validate"):
        mod = types.ModuleType(name)
        mod.Github = object
        mod.validate_faasr_json = lambda wf: (True, None)
        monkeypatch.setitem(sys.modules, name, mod)


def test_required_secret_names_match_vendored_yaml_secrets(monkeypatch):
    _stub_modules(monkeypatch)
    monkeypatch.syspath_prepend(str(VENDOR))
    from curate_deploy import register_workflow as R
    wf = workflow(secrets=["NOAA_TOKEN"])
    text = R.generate_github_secret_imports(wf) + "\n" + R.generate_user_defined_secret_imports(wf)
    names = sorted({line.split(":")[0].strip() for line in text.splitlines() if line.strip()})
    assert D.required_secret_names(wf) == names == ["GH_PAT", "NOAA_TOKEN", "S3_AccessKey", "S3_SecretKey"]


# ── leakcheck ──

def test_leakcheck_by_value_and_by_pattern(monkeypatch):
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
    text = f"x {FAKE['S3_SecretKey']} y"
    assert L.find_leaks(text) == ["S3_SecretKey"]
    assert L.find_leaks("token ghp_" + "z" * 36) == ["github_token"]          # pattern, any owner
    assert L.find_leaks("id AKIAZZZZZZZZZZZZZZZZ") == ["aws_access_key_id"]
    assert L.find_leaks("tok", extra_values={"gh_token": "tok"}) == []           # too short to match
    assert L.find_leaks("my-gh-token-xyz", extra_values={"gh_token": "my-gh-token-xyz"}) == ["gh_token"]
    red = L.redact(f"a {FAKE['GH_PAT']} b {FAKE['S3_SecretKey']}")
    assert FAKE["GH_PAT"] not in red and FAKE["S3_SecretKey"] not in red
    assert "[REDACTED:GH_PAT]" in red and "[REDACTED:S3_SecretKey]" in red
    assert L.find_leaks("nothing here") == []


# ── preflight ──

def workflow(user="alice", repo="faasr-actions", secrets=()):
    wf = {
        "WorkflowName": "weather", "EntryPoint": "download",
        "ComputeServers": {"GH": {"FaaSType": "GitHubActions", "UserName": user, "ActionRepoName": repo,
                                  "Branch": "main", "UseSecretStore": True}},
        "DataStores": {"S3": {"Endpoint": "https://s3.amazonaws.com", "Bucket": "b", "Region": "us-west-2",
                              "AccessKey": "S3_AccessKey", "SecretKey": "S3_SecretKey", "Writable": "TRUE"}},
        "ActionList": {
            "download": {"FaaSServer": "GH", "Type": "Python", "FunctionName": "download_station",
                         "Arguments": {"folder": "weather_data", "output1": "station.csv"},
                         "InvokeNext": ["plot"]},
            "plot": {"FaaSServer": "GH", "Type": "Python", "FunctionName": "plot_weather",
                     "Arguments": {"folder": "weather_data", "input1": "station.csv", "output1": "plot.png"},
                     "InvokeNext": []}},
        "FunctionGitRepo": {"download_station": f"{user}/{repo}/main/functions/download_station.py",
                            "plot_weather": f"{user}/{repo}/main/functions/plot_weather.py"},
        "ActionContainers": {"download": "ghcr.io/faasr/github-actions-python:latest",
                             "plot": "ghcr.io/faasr/github-actions-python:latest"},
    }
    if secrets:
        wf["Secrets"] = list(secrets)
    return wf


@pytest.fixture
def run(tmp_path):
    bundle = tmp_path / "faasr_output" / "weather-r1"
    (bundle / "functions").mkdir(parents=True)
    (bundle / "workflow.json").write_text(json.dumps(workflow(), indent=2))
    (bundle / "functions" / "plot_weather.py").write_text("def plot_weather(): pass\n")
    r = tmp_path / ".curate" / "runs" / "r1"
    r.mkdir(parents=True)
    (r / "state.json").write_text(json.dumps({
        "schema_version": 2, "run_id": "r1", "stage": "done", "bundle_dir": str(bundle),
        "history": [{"gate": 4, "decision": "approve"}]}))
    return r


@pytest.fixture
def env(monkeypatch):
    for k, v in FAKE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("FAASR_GH_USERNAME", "alice")
    monkeypatch.setenv("FAASR_ACTION_REPO", "faasr-actions")
    for k in ("FAASR_ACTION_BRANCH", "CUSTOM_CONTAINER", "CURATE_DEPLOY_NAME_SUFFIX",
              "FAASR_S3_BUCKET", "FAASR_S3_ENDPOINT", "FAASR_S3_REGION"):
        monkeypatch.delenv(k, raising=False)


class FakeGitHub:
    """Serves GET paths from a dict; records every request; refuses anything but GET."""

    def __init__(self, files=None, commits=None, secrets=("GH_PAT", "S3_AccessKey", "S3_SecretKey"),
                 scopes="repo, workflow", default_branch="main", push=True):
        self.files = files or {}
        self.commits = commits or {}
        self.secrets, self.scopes, self.default_branch, self.push = secrets, scopes, default_branch, push
        self.requests = []

    def __call__(self, req, timeout=None):
        assert req.get_method() == "GET"
        self.requests.append(req)
        assert req.headers["Authorization"] == f"Bearer {FAKE['GH_PAT']}"
        url = req.full_url.split("https://api.github.com", 1)[1]
        path, _, query = url.partition("?")
        if path == "/user":
            return self._ok({"login": "alice"}, {"X-OAuth-Scopes": self.scopes} if self.scopes is not None else {})
        if path == "/repos/alice/faasr-actions":
            return self._ok({"default_branch": self.default_branch, "permissions": {"push": self.push}})
        if path == "/repos/alice/faasr-actions/actions/secrets":
            return self._ok({"secrets": [{"name": n} for n in self.secrets]})
        if path.startswith("/repos/alice/faasr-actions/contents/"):
            p = path.split("/contents/", 1)[1]
            if p in self.files:
                return self._ok({"content": base64.b64encode(self.files[p].encode()).decode()})
        if path == "/repos/alice/faasr-actions/commits":
            p = dict(x.split("=", 1) for x in query.split("&"))["path"].replace("%2F", "/")
            if p in self.commits:
                return self._ok([{"commit": {"message": self.commits[p]}}])
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))

    @staticmethod
    def _ok(body, headers=None):
        class R(io.BytesIO):
            status = 200

            def __init__(self):
                super().__init__(json.dumps(body).encode())
                self.headers = headers or {}
        return R()


class FakeS3:
    """head_bucket and list_objects_v2 only; prefixes in `objects` hold one key. Anything else fails."""

    def __init__(self, objects=(), bucket="b", reachable=True):
        self.objects, self.bucket, self.reachable = set(objects), bucket, reachable
        self.calls, self.made_with = [], None

    def __call__(self, cfg, access_key, secret_key):  # the factory
        self.made_with = (cfg, access_key, secret_key)
        return self

    def head_bucket(self, Bucket):
        self.calls.append(("head_bucket", Bucket))
        if not self.reachable or Bucket != self.bucket:
            e = Exception("denied")
            e.response = {"Error": {"Code": "403"}}
            raise e

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        self.calls.append(("list_objects_v2", Prefix))
        assert MaxKeys == 1
        hit = any(o.startswith(Prefix) for o in self.objects)
        return {"KeyCount": int(hit), **({"Contents": [{"Key": Prefix + "x"}]} if hit else {})}

    def __getattr__(self, name):
        raise AssertionError(f"S3 call {name} is not read-only-allowed")


def pre(capsys, run, fake=None, *extra, s3=None):
    code = D.main(["preflight", "--run", str(run), *extra], opener=fake, s3_factory=s3 or FakeS3())
    return code, json.loads(capsys.readouterr().out)


def test_preflight_ready_lists_suffixed_targets_only(run, env, capsys):
    fake = FakeGitHub()
    code, out = pre(capsys, run, fake)
    assert code == 0 and out["ok"], out["problems"]
    assert out["deploy_name"] == "weather-cs" and out["approved_name"] == "weather"
    paths = [t["path"] for t in out["targets"]]
    assert paths == ["functions/weather-cs/download_station.py", "functions/weather-cs/plot_weather.py",
                     "workflows/weather-cs.json", ".github/workflows/weather-cs-download.yml",
                     ".github/workflows/weather-cs-plot.yml"]
    assert out["function_git_repo"] == {
        "download_station": "alice/faasr-actions/main/functions/weather-cs/download_station.py",
        "plot_weather": "alice/faasr-actions/main/functions/weather-cs/plot_weather.py"}
    assert all(not t["exists"] for t in out["targets"])
    assert out["secrets"]["missing"] == [] and out["token_source"] == "GH_PAT"
    assert "nothing was written" in out["summary"]
    assert all(r.get_method() == "GET" for r in fake.requests)  # read-only
    assert [t["prefix"] for t in out["s3_targets"]] == ["weather_data-cs/", "FaaSrLog/weather-cs/"]
    assert not any(t["exists"] for t in out["s3_targets"]) and out["s3"]["checked"]
    assert out["existing_paths"] == []


def test_deploy_copy_suffixes_the_s3_folder_and_leaves_the_bundle_alone(run, env, capsys):
    wf_path = Path(json.loads((run / "state.json").read_text())["bundle_dir"], "workflow.json")
    before = wf_path.read_text()
    copy_wf, _, s3_targets = D.deploy_copy(workflow(), "-cs", "alice", "faasr-actions", "main")
    assert {a["Arguments"]["folder"] for a in copy_wf["ActionList"].values()} == {"weather_data-cs"}
    assert copy_wf["ActionList"]["plot"]["Arguments"]["input1"] == "station.csv"
    assert len(s3_targets) == 2
    pre(capsys, run, FakeGitHub())
    assert wf_path.read_text() == before


def test_preflight_fills_placeholders_in_the_deploy_copy_only(run, env, capsys, monkeypatch):
    wf = workflow()
    wf["DataStores"]["S3"]["Bucket"] = "<FAASR_S3_BUCKET>"
    wf["ComputeServers"]["GH"]["UserName"] = "<FAASR_GH_USERNAME>"
    wf_path = Path(json.loads((run / "state.json").read_text())["bundle_dir"], "workflow.json")
    wf_path.write_text(json.dumps(wf))
    monkeypatch.setenv("FAASR_S3_BUCKET", "b")
    s3 = FakeS3()
    code, out = pre(capsys, run, FakeGitHub(), s3=s3)
    assert out["ok"], out["problems"]
    assert out["filled_placeholders"] == {"ComputeServers.GH.UserName": "$FAASR_GH_USERNAME = alice",
                                          "DataStores.S3.Bucket": "$FAASR_S3_BUCKET = b"}
    assert s3.made_with[0]["Bucket"] == "b" and s3.made_with[1:] == (FAKE["S3_AccessKey"], FAKE["S3_SecretKey"])
    assert "<FAASR_S3_BUCKET>" in wf_path.read_text()  # the approved bundle is unchanged
    assert "Placeholders filled in the deploy copy only" in out["summary"]


def test_preflight_refuses_existing_s3_paths_and_reports_curates(run, env, capsys):
    s3 = FakeS3(objects={"weather_data-cs/station.csv", "FaaSrLog/weather-cs/2026/x.txt",
                         "weather_data/station.csv", "FaaSrLog/weather/2026/x.txt"})
    code, out = pre(capsys, run, FakeGitHub(), s3=s3)
    assert code == 1
    assert out["existing_paths"] == ["s3://b/weather_data-cs/", "s3://b/FaaSrLog/weather-cs/"]
    assert sum("already holds objects" in x for x in out["problems"]) == 2
    assert out["s3"]["curate_prefixes"] == ["weather_data/", "FaaSrLog/weather/"]
    assert "Paths that already exist (refused)" in out["summary"]
    assert {c[0] for c in s3.calls} == {"head_bucket", "list_objects_v2"}  # read-only


@pytest.mark.parametrize("case,needle", [
    ("unreachable", "not reachable with these keys: Exception 403"),
    ("no_keys", "S3 not checked"),
    ("no_folder", "has no \"folder\" argument"),
    ("external", "would not have them"),
])
def test_preflight_s3_problems(run, env, capsys, monkeypatch, case, needle):
    s3 = FakeS3(reachable=case != "unreachable")
    if case == "no_keys":
        monkeypatch.delenv("S3_SecretKey")
    bundle = Path(json.loads((run / "state.json").read_text())["bundle_dir"])
    if case == "no_folder":
        wf = workflow()
        del wf["ActionList"]["plot"]["Arguments"]["folder"]
        (bundle / "workflow.json").write_text(json.dumps(wf))
    if case == "external":
        (bundle / "external_inputs.json").write_text(json.dumps([{"file": "in.csv", "s3_path": "weather_data/in.csv"}]))
    code, out = pre(capsys, run, FakeGitHub(), s3=s3)
    assert code == 1 and any(needle in p for p in out["problems"]), out["problems"]
    assert all(v not in json.dumps(out) for v in FAKE.values())


def test_preflight_never_prints_or_writes_secrets(run, env, capsys):
    before = {p: p.read_bytes() for p in run.parent.parent.parent.rglob("*") if p.is_file()}
    code, out = pre(capsys, run, FakeGitHub())
    text = json.dumps(out)
    assert all(v not in text for v in FAKE.values())
    after = {p: p.read_bytes() for p in run.parent.parent.parent.rglob("*") if p.is_file()}
    assert before == after  # nothing written locally either


def test_preflight_leaves_curate_files_alone_and_reports_them(run, env, capsys):
    curate = {"workflows/weather.json": json.dumps({"WorkflowName": "weather"}),
              "functions/plot_weather.py": "x", ".github/workflows/weather-plot.yml": "name: weather-plot\n"}
    code, out = pre(capsys, run, FakeGitHub(files=curate))
    assert out["ok"] and sorted(out["curate_files"]) == sorted(curate)
    assert not any(t["path"] in curate for t in out["targets"])
    assert "left untouched" in out["summary"]


def test_preflight_refuses_targets_that_are_not_ours(run, env, capsys):
    files = {"workflows/weather-cs.json": json.dumps({"WorkflowName": "something-else"}),
             "functions/weather-cs/plot_weather.py": "x",
             ".github/workflows/weather-cs-plot.yml": "name: other\n"}
    code, out = pre(capsys, run, FakeGitHub(files=files, commits={
        "functions/weather-cs/plot_weather.py": "Add plot (faasr-agents)"}))
    assert code == 1 and not out["ok"]
    refused = [x for x in out["problems"] if "does not belong" in x]
    assert len(refused) == 3 and "EXISTS, NOT OURS" in out["summary"]


def test_preflight_refuses_even_its_own_earlier_deployment(run, env, capsys):
    files = {"workflows/weather-cs.json": json.dumps({"WorkflowName": "weather-cs"}),
             "functions/weather-cs/plot_weather.py": "x",
             ".github/workflows/weather-cs-plot.yml": "name: weather-cs-plot\non: ..."}
    code, out = pre(capsys, run, FakeGitHub(files=files, commits={
        "functions/weather-cs/plot_weather.py": "Add/update plot_weather (curate-skills)"}))
    assert code == 1 and not out["ok"]
    assert sum(1 for t in out["targets"] if t["exists"] and t["ours"]) == 3
    assert sorted(out["existing_paths"]) == sorted(files)
    assert sum("earlier curate-skills deployment" in x for x in out["problems"]) == 3


@pytest.mark.parametrize("kw,needle", [
    ({"scopes": "repo"}, "lacks scope(s) ['workflow']"),
    ({"push": False}, "cannot push"),
    ({"default_branch": "master"}, "default branch is 'master'"),
    ({"secrets": ("GH_PAT",)}, "GitHub secrets missing in alice/faasr-actions: S3_AccessKey, S3_SecretKey"),
])
def test_preflight_repo_problems(run, env, capsys, kw, needle):
    code, out = pre(capsys, run, FakeGitHub(**kw))
    assert code == 1 and any(needle in p for p in out["problems"]), out["problems"]


def test_fine_grained_token_is_reported_not_refused(run, env, capsys):
    code, out = pre(capsys, run, FakeGitHub(scopes=None))
    assert out["ok"] and out["token_scopes"].startswith("fine-grained")


def test_preflight_offline_problems(run, env, capsys, monkeypatch):
    monkeypatch.delenv("GH_PAT")
    monkeypatch.setattr(D.shutil, "which", lambda name: None)  # no gh either
    wf = workflow()
    wf["DataStores"]["S3"]["Bucket"] = "<FAASR_S3_BUCKET>"
    wf["ActionContainers"]["plot"] = "me/custom:1"
    Path(json.loads((run / "state.json").read_text())["bundle_dir"], "workflow.json").write_text(json.dumps(wf))
    code, out = pre(capsys, run)
    probs = " | ".join(out["problems"])
    assert code == 1 and "placeholders" in probs and "FAASR_S3_BUCKET not set" in probs
    assert "DataStores.S3.Bucket" in probs
    assert "native list" in probs and "no GitHub token" in probs and not out["github_checked"]


def test_preflight_refuses_unapproved_or_changed_bundles(run, env, capsys):
    s = json.loads((run / "state.json").read_text())
    s["history"].append({"gate": 4, "decision": "revise", "text": "use csv"})
    s["bundle_hash"] = "0" * 64
    (run / "state.json").write_text(json.dumps(s))
    code, out = pre(capsys, run, FakeGitHub())
    probs = " | ".join(out["problems"])
    assert "superseded" in probs and "bundle folder changed" in probs


def test_suffix_must_be_non_empty_and_simple(run, env, capsys):
    for bad in ("", "a/b", "../x"):
        code, out = pre(capsys, run, FakeGitHub(), f"--name-suffix={bad}")
        assert code == 2 and "suffix" in out["error"]
    code, out = pre(capsys, run, FakeGitHub(), "--name-suffix=-skills")
    assert out["deploy_name"] == "weather-skills"


def test_output_with_a_leaked_token_is_withheld(run, env, capsys, monkeypatch):
    wf = workflow()
    wf["WorkflowName"] = "weather-" + FAKE["S3_SecretKey"]  # a secret pasted into the bundle (echoed)
    Path(json.loads((run / "state.json").read_text())["bundle_dir"], "workflow.json").write_text(json.dumps(wf))
    code, out = pre(capsys, run, FakeGitHub())
    assert code == 1 and out == {"ok": False, "error": "output withheld: contains secret values",
                                 "leaked_secret_names": ["S3_SecretKey"]}
