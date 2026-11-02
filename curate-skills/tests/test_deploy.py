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
    (bundle / "functions" / "download_station.py").write_text("def download_station(): pass\n")
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
    """Serves GET paths from a dict; records every request. PUT (a contents write) only when
    writable=True: it stores the file and its commit message; paths in fail_put get HTTP 500."""

    def __init__(self, files=None, commits=None, secrets=("GH_PAT", "S3_AccessKey", "S3_SecretKey"),
                 scopes="repo, workflow", default_branch="main", push=True, writable=False, fail_put=()):
        self.files = dict(files or {})
        self.commits = dict(commits or {})
        self.secrets, self.scopes, self.default_branch, self.push = secrets, scopes, default_branch, push
        self.writable, self.fail_put = writable, set(fail_put)
        self.requests, self.puts = [], []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if req.get_method() == "PUT":
            assert self.writable, "a write was attempted where none is allowed"
            assert req.headers["Authorization"] == f"Bearer {FAKE['GH_PAT']}"
            p = req.full_url.split("/contents/", 1)[1]
            body = json.loads(req.data)
            if p in self.fail_put:
                raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b"{}"))
            self.puts.append((p, body))
            self.files[p] = base64.b64decode(body["content"]).decode()
            self.commits[p] = body["message"]
            return self._ok({"content": {"path": p}})
        assert req.get_method() == "GET"
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
                return self._ok({"content": base64.b64encode(self.files[p].encode()).decode(), "sha": "sha-" + p})
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
    assert "| `DataStores.S3.Bucket` | `<FAASR_S3_BUCKET>` | `b` | placeholder $FAASR_S3_BUCKET |" in out["summary"]


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


def test_secret_names_match_without_regard_to_case(run, env, capsys):
    # GitHub lists secret names in upper case; ${{ secrets.S3_AccessKey }} resolves to S3_ACCESSKEY.
    code, out = pre(capsys, run, FakeGitHub(secrets=("GH_PAT", "S3_ACCESSKEY", "S3_SECRETKEY")))
    assert out["ok"], out["problems"]
    assert out["secrets"]["present"] == ["GH_PAT", "S3_AccessKey", "S3_SecretKey"] and out["secrets"]["missing"] == []


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


# ── deploy copy vs approved bundle ──

def test_summary_diffs_the_deploy_copy_field_by_field(run, env, capsys, monkeypatch):
    wf = workflow()
    wf["DataStores"]["S3"]["Bucket"] = "<FAASR_S3_BUCKET>"
    wf["FunctionGitRepo"]["plot_weather"] = "<FAASR_GH_USERNAME>/<FAASR_ACTION_REPO>/main/functions/plot_weather.py"
    Path(json.loads((run / "state.json").read_text())["bundle_dir"], "workflow.json").write_text(json.dumps(wf))
    monkeypatch.setenv("FAASR_S3_BUCKET", "b")
    code, out = pre(capsys, run, FakeGitHub())
    assert out["ok"], out["problems"]
    rows = {r["field"]: r for r in out["deploy_diff"]}
    assert rows["FunctionGitRepo.plot_weather"]["reason"] == \
        "placeholder $FAASR_GH_USERNAME, $FAASR_ACTION_REPO; per-deployment function folder"
    assert set(rows) == {"WorkflowName", "ActionList.download.Arguments.folder", "ActionList.plot.Arguments.folder",
                         "FunctionGitRepo.download_station", "FunctionGitRepo.plot_weather", "DataStores.S3.Bucket"}
    assert rows["WorkflowName"]["approved"] == "weather" and rows["WorkflowName"]["deploy"] == "weather-cs"
    assert rows["DataStores.S3.Bucket"]["reason"] == "placeholder $FAASR_S3_BUCKET"
    assert rows["ActionList.plot.Arguments.folder"]["reason"] == "suffix (S3 folder)"
    assert {d["what"]: d["deploy"] for d in out["derived_changes"]}["S3 log prefix (FaaSr)"] == "FaaSrLog/weather-cs/"
    summary = out["summary"]
    assert "**Deploy copy vs approved bundle**" in summary
    assert "| `ActionList.plot.Arguments.folder` | `weather_data` | `weather_data-cs` | suffix (S3 folder) |" in summary
    assert "| S3 log prefix (FaaSr) (derived from the name) | `FaaSrLog/weather/` | `FaaSrLog/weather-cs/` |" in summary


def test_a_diff_outside_the_allowed_fields_is_refused(run, env, capsys, monkeypatch):
    real = D.deploy_copy

    def tampered(*a, **kw):
        wf, targets, s3 = real(*a, **kw)
        wf["ActionList"]["plot"]["Arguments"]["output1"] = "other.png"
        return wf, targets, s3
    monkeypatch.setattr(D, "deploy_copy", tampered)
    code, out = pre(capsys, run, FakeGitHub())
    assert code == 1 and any("ActionList.plot.Arguments.output1, which is not an allowed" in p
                             for p in out["problems"])
    assert {r["field"]: r["reason"] for r in out["deploy_diff"]}["ActionList.plot.Arguments.output1"] == "NOT ALLOWED"


# ── retrying a partial deploy: --overwrite-own-deploy ──

OWN_FILES = {"workflows/weather-cs.json": json.dumps({"WorkflowName": "weather-cs"}),
             "functions/weather-cs/plot_weather.py": "x"}
OWN_COMMITS = {"functions/weather-cs/plot_weather.py": "Add/update plot_weather (curate-skills)"}


def write_log(run, *entries):
    (run / "deploy").mkdir(exist_ok=True)
    (run / "deploy" / "deploy-log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))


def logged(path=None, **kw):
    base = {"run_id": "r1", "deploy_name": "weather-cs", "step": "publish"}
    if path:
        base.update(path=path, repo="alice/faasr-actions", branch="main")
    return {**base, **kw}


def test_overwrite_own_is_off_by_default(run, env, capsys):
    write_log(run, *(logged(p) for p in OWN_FILES))
    code, out = pre(capsys, run, FakeGitHub(files=OWN_FILES, commits=OWN_COMMITS))
    assert code == 1 and sorted(out["refused_paths"]) == sorted(OWN_FILES) and out["overwrite_paths"] == []
    assert any("--overwrite-own-deploy" in p for p in out["problems"])


def test_overwrite_own_allows_only_logged_paths_of_this_run(run, env, capsys):
    write_log(run, *(logged(p) for p in OWN_FILES),
              logged(step="invoke", bucket="b", prefix="weather_data-cs/"))
    s3 = FakeS3(objects={"weather_data-cs/station.csv", "FaaSrLog/weather-cs/x.txt"})
    code, out = pre(capsys, run, FakeGitHub(files=OWN_FILES, commits=OWN_COMMITS), "--overwrite-own-deploy", s3=s3)
    assert sorted(out["overwrite_paths"]) == sorted([*OWN_FILES, "s3://b/weather_data-cs/"])
    assert out["refused_paths"] == ["s3://b/FaaSrLog/weather-cs/"]  # not in the log → still refused
    assert code == 1 and "will be overwritten (--overwrite-own-deploy)" in out["summary"]


@pytest.mark.parametrize("entry", [
    logged("functions/weather-cs/plot_weather.py", run_id="other-run"),       # another run's log line
    logged("functions/weather-cs/plot_weather.py", deploy_name="weather-x"),  # another deployment
    logged("functions/weather-cs/plot_weather.py", repo="bob/faasr-actions"), # another repo
])
def test_overwrite_own_ignores_log_lines_that_are_not_this_deploy(run, env, capsys, entry):
    write_log(run, entry)
    code, out = pre(capsys, run, FakeGitHub(files={"functions/weather-cs/plot_weather.py": "x"},
                                            commits=OWN_COMMITS), "--overwrite-own-deploy")
    assert code == 1 and out["refused_paths"] == ["functions/weather-cs/plot_weather.py"]


def test_overwrite_own_refuses_a_logged_path_someone_else_changed(run, env, capsys):
    write_log(run, logged("functions/weather-cs/plot_weather.py"), "not json")
    code, out = pre(capsys, run, FakeGitHub(files={"functions/weather-cs/plot_weather.py": "x"},
                                            commits={"functions/weather-cs/plot_weather.py": "hand edit"}),
                    "--overwrite-own-deploy")
    assert code == 1 and out["refused_paths"] == ["functions/weather-cs/plot_weather.py"]
    assert any("no longer carries its mark" in p for p in out["problems"])
    assert any("line 2 is not a JSON object" in w for w in out["warnings"])


def test_overwrite_own_never_allows_curates_files(run, env, capsys):
    # CURATE's paths are never targets, so even a forged log line cannot make them writable.
    write_log(run, logged("workflows/weather.json"), logged("functions/plot_weather.py"))
    code, out = pre(capsys, run, FakeGitHub(files={"workflows/weather.json": "{}"}), "--overwrite-own-deploy")
    assert out["ok"] and "workflows/weather.json" not in [t["path"] for t in out["targets"]]
    assert out["overwrite_paths"] == []


# ── S3 keys: S3_AccessKey or GitHub's S3_ACCESSKEY ──

@pytest.mark.parametrize("camel,upper,spelling", [
    (True, False, "S3_AccessKey"),
    (False, True, "S3_ACCESSKEY"),
    (True, True, "S3_AccessKey and S3_ACCESSKEY (same value)"),
])
def test_s3_keys_accept_either_spelling(run, env, capsys, monkeypatch, camel, upper, spelling):
    for name in ("S3_AccessKey", "S3_SecretKey"):
        if not camel:
            monkeypatch.delenv(name)
        if upper:
            monkeypatch.setenv(name.upper(), FAKE[name])
    s3 = FakeS3()
    code, out = pre(capsys, run, FakeGitHub(), s3=s3)
    assert out["ok"], out["problems"]
    assert s3.made_with[1:] == (FAKE["S3_AccessKey"], FAKE["S3_SecretKey"])
    assert out["s3"]["key_vars"]["S3_AccessKey"] == spelling
    assert f"- S3 keys read from: `{spelling}`, `{spelling.replace('AccessKey', 'SecretKey').replace('ACCESSKEY', 'SECRETKEY')}`" \
        in out["summary"]
    assert all(v not in json.dumps(out) for v in FAKE.values())


def test_s3_keys_set_twice_with_different_values_are_refused(run, env, capsys, monkeypatch):
    monkeypatch.setenv("S3_ACCESSKEY", "AKIA" + "D" * 16)  # differs from S3_AccessKey
    monkeypatch.setenv("S3_SECRETKEY", FAKE["S3_SecretKey"])  # same: fine on its own
    s3 = FakeS3()
    code, out = pre(capsys, run, FakeGitHub(), s3=s3)
    assert code == 1 and s3.made_with is None  # no S3 client built
    msg = [p for p in out["problems"] if "both set" in p]
    assert msg == ["S3 not checked: S3_AccessKey and S3_ACCESSKEY are both set, with different values; "
                   "unset one (values not shown), so the bucket and existing S3 paths could not be checked"]
    text = json.dumps(out)
    assert "D" * 16 not in text and all(v not in text for v in FAKE.values())


def test_uppercase_spellings_are_leak_checked_and_scrubbed(monkeypatch):
    import emit
    import stubtest
    monkeypatch.setenv("S3_SECRETKEY", "upper-" + "e" * 30)
    text = "x upper-" + "e" * 30
    assert L.find_leaks(text) == ["S3_SECRETKEY"]
    assert "[REDACTED:S3_SECRETKEY]" in L.redact(text)
    assert emit.find_leaked_secrets(text, emit.BASE_SECRET_NAMES) == ["S3_SECRETKEY"]
    assert "S3_SECRETKEY" not in stubtest._env(None, None, [])


def test_tree_hash_ignores_os_metadata_but_not_content(tmp_path):
    (tmp_path / "functions").mkdir()
    (tmp_path / "workflow.json").write_text("{}")
    (tmp_path / "functions" / "f.py").write_text("x = 1\n")
    before = D.tree_hash(tmp_path)
    (tmp_path / ".DS_Store").write_bytes(b"finder")
    (tmp_path / "functions" / "._f.py").write_bytes(b"resource fork")
    (tmp_path / "functions" / "__pycache__").mkdir()
    (tmp_path / "functions" / "__pycache__" / "f.cpython-313.pyc").write_bytes(b"pyc")
    assert D.tree_hash(tmp_path) == before
    (tmp_path / "functions" / "f.py").write_text("x = 2\n")
    assert D.tree_hash(tmp_path) != before


# ── the four deploy steps (fakes only: nothing reaches GitHub or S3) ──

@pytest.fixture
def deploying(run):
    """The run after Gate 4 was approved with Approve & deploy."""
    s = json.loads((run / "state.json").read_text())
    s.update(stage="deploy", deploy_choice="yes", bundle_hash=D.tree_hash(Path(s["bundle_dir"])))
    (run / "state.json").write_text(json.dumps(s))
    return run


@pytest.fixture
def runners(monkeypatch):
    """Stand-ins for CURATE's register and invoke code; they record what they were given."""
    calls = {}

    def register(copy_wf, token, extra):
        calls["register"] = copy_wf
        for action in copy_wf["ActionList"]:
            calls["fake"].files[f".github/workflows/{copy_wf['WorkflowName']}-{action}.yml"] = \
                f"name: {copy_wf['WorkflowName']}-{action}\n"
        return ["INFO: registered"]

    def invoke(workflow_path, token, extra):
        calls["invoke"] = workflow_path
        return {"invocation_id": "inv-123", "invocation_timestamp": "2026-11-01-12-00-00", "log": []}
    monkeypatch.setattr(D, "RUNNERS", {"register": register, "invoke": invoke})
    return calls


def step(capsys, run, name, fake, *extra, s3=None):
    code = D.main([name, "--run", str(run), *extra], opener=fake, s3_factory=s3 or FakeS3())
    return code, json.loads(capsys.readouterr().out)


def log_lines(run):
    p = run / "deploy" / "deploy-log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_a_step_without_confirmed_writes_nothing_and_says_what_it_would_do(deploying, env, capsys):
    fake = FakeGitHub(writable=True)
    code, out = step(capsys, deploying, "publish", fake)
    assert code == 1 and out.get("needs_confirmation"), out.get("problems") or out
    assert "ask the user first" in out["error"]
    assert out["plan"]["paths"] == ["functions/weather-cs/download_station.py", "functions/weather-cs/plot_weather.py"]
    assert out["plan"]["repo"] == "alice/faasr-actions" and fake.puts == [] and log_lines(deploying) == []
    assert "deploy" not in json.loads((deploying / "state.json").read_text()) or \
        not json.loads((deploying / "state.json").read_text()).get("deploy")


def test_all_four_steps_in_order(deploying, env, capsys, runners):
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    for name in D.STEPS:
        code, out = step(capsys, deploying, name, fake, "--confirmed")
        assert code == 0 and out["ok"], (name, out)
    # publish: the bundle's function files, each with curate-skills' mark
    assert [p for p, _ in fake.puts] == ["functions/weather-cs/download_station.py",
                                         "functions/weather-cs/plot_weather.py", "workflows/weather-cs.json"]
    assert all("(curate-skills)" in b["message"] and b["branch"] == "main" for _, b in fake.puts)
    assert fake.files["functions/weather-cs/plot_weather.py"] == "def plot_weather(): pass\n"
    # upload: the deploy copy (renamed, -cs folder), also kept in the run
    uploaded = json.loads(fake.files["workflows/weather-cs.json"])
    assert uploaded["WorkflowName"] == "weather-cs"
    assert {a["Arguments"]["folder"] for a in uploaded["ActionList"].values()} == {"weather_data-cs"}
    assert json.loads((deploying / "deploy" / "workflow.json").read_text()) == uploaded
    assert runners["register"] == uploaded and runners["invoke"] == "workflows/weather-cs.json"
    # state, log, stage
    s = json.loads((deploying / "state.json").read_text())
    assert all(s["deploy"]["steps"][x]["ok"] for x in D.STEPS)
    assert s["deploy"]["invocation_id"] == "inv-123"
    assert s["stage"] == "monitor" and out["next_skill"] == "curate-skills:curate-monitor"
    assert s["history"][-1]["decision"] == "deployed"
    import spec as S
    S.main(["next", "--run", str(deploying)])
    nxt = json.loads(capsys.readouterr().out)
    assert nxt["next_skill"] == "curate-skills:curate-monitor"
    logged = [(e["step"], e.get("path") or e.get("prefix")) for e in log_lines(deploying)]
    assert logged == [("publish", "functions/weather-cs/download_station.py"),
                      ("publish", "functions/weather-cs/plot_weather.py"),
                      ("upload", "workflows/weather-cs.json"),
                      ("register", ".github/workflows/weather-cs-download.yml"),
                      ("register", ".github/workflows/weather-cs-plot.yml"),
                      ("invoke", "weather_data-cs/"), ("invoke", "FaaSrLog/weather-cs/")]
    assert all(e["run_id"] == "r1" and e["deploy_name"] == "weather-cs" for e in log_lines(deploying))
    # secrets nowhere
    for f in [deploying / "state.json", deploying / "deploy" / "deploy-log.jsonl", deploying / "deploy" / "workflow.json"]:
        assert all(v not in f.read_text() for v in FAKE.values())
    assert all(v not in json.dumps(b) for _, b in fake.puts for v in FAKE.values())


def test_steps_go_in_order_and_a_finished_step_is_skipped(deploying, env, capsys, runners):
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    code, out = step(capsys, deploying, "upload", fake, "--confirmed")
    assert code == 1 and out["next_step"] == "publish" and fake.puts == []
    step(capsys, deploying, "publish", fake, "--confirmed")
    n = len(fake.puts)
    code, out = step(capsys, deploying, "publish", fake, "--confirmed")
    assert code == 0 and out["skipped"] and len(fake.puts) == n
    code, out = step(capsys, deploying, "upload", fake)  # publish's files exist: not a problem
    assert out["needs_confirmation"], out.get("problems")
    assert "written by this deploy" in out["preflight_summary"]


@pytest.mark.parametrize("case,needle", [
    ("changed", "bundle folder changed after Gate 4"),
    ("no_hash", "no bundle hash was recorded"),
    ("stage", "not deploy"),
    ("no_deploy", "without deploying"),
    ("actions_env", "GITHUB_REPOSITORY is set here"),
])
def test_steps_refuse_before_writing(deploying, env, capsys, monkeypatch, case, needle):
    s = json.loads((deploying / "state.json").read_text())
    if case == "changed":
        (Path(s["bundle_dir"]) / "functions" / "plot_weather.py").write_text("def plot_weather(): return 1\n")
    if case == "no_hash":
        s.pop("bundle_hash")
    if case == "stage":
        s["stage"] = "finalize"
    if case == "no_deploy":
        s["deploy_choice"] = "no"
    if case == "actions_env":
        monkeypatch.setenv("GITHUB_REPOSITORY", "someone/else")
    (deploying / "state.json").write_text(json.dumps(s))
    fake = FakeGitHub(writable=True)
    code, out = step(capsys, deploying, "publish", fake, "--confirmed")
    assert code == 1 and needle in json.dumps(out), out
    assert fake.puts == [] and log_lines(deploying) == []


def test_a_secret_in_function_code_is_never_pushed(deploying, env, capsys):
    s = json.loads((deploying / "state.json").read_text())
    bundle = Path(s["bundle_dir"])
    (bundle / "functions" / "plot_weather.py").write_text(f"KEY = '{FAKE['S3_SecretKey']}'\n")
    s["bundle_hash"] = D.tree_hash(bundle)  # as if it had been approved like this
    (deploying / "state.json").write_text(json.dumps(s))
    fake = FakeGitHub(writable=True)
    code, out = step(capsys, deploying, "publish", fake, "--confirmed")
    assert code == 1 and out["failed"] and "contains secret values (S3_SecretKey)" in out["error"]
    assert fake.puts == [] and log_lines(deploying) == []  # every file is checked before the first push
    assert FAKE["S3_SecretKey"] not in json.dumps(out) + (deploying / "state.json").read_text()


def test_a_failed_step_can_be_retried_only_with_overwrite_own_deploy(deploying, env, capsys):
    fake = FakeGitHub(writable=True, fail_put={"functions/weather-cs/plot_weather.py"})
    code, out = step(capsys, deploying, "publish", fake, "--confirmed")
    assert code == 1 and out["failed"] and "HTTP 500" in out["error"]
    assert out["resume_command"] == "/curate-skills:curate-deploy --run r1"
    s = json.loads((deploying / "state.json").read_text())
    assert s["stage"] == "deploy" and s["deploy"]["steps"]["publish"]["ok"] is False
    assert [e["path"] for e in log_lines(deploying)] == ["functions/weather-cs/download_station.py"]
    fake.fail_put = set()
    code, out = step(capsys, deploying, "publish", fake, "--confirmed")  # its own file now exists
    assert code == 1 and out["problems"] and "--overwrite-own-deploy" in " ".join(out["problems"])
    code, out = step(capsys, deploying, "publish", fake, "--confirmed", "--overwrite-own-deploy")
    assert code == 0 and out["ok"], out
    assert sum(p == "functions/weather-cs/download_station.py" for p, _ in fake.puts) == 2


def test_status_reports_done_and_next(deploying, env, capsys, runners):
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    step(capsys, deploying, "publish", fake, "--confirmed")
    D.main(["status", "--run", str(deploying)])
    out = json.loads(capsys.readouterr().out)
    assert out["done"] == ["publish"] and out["next_step"] == "upload" and out["deploy_log"]


def test_publish_never_pushes_os_metadata(deploying, env, capsys, runners):
    # The same files tree_hash ignores: they can appear after approval without changing the hash,
    # and publish only pushes functions/<fn>.py for the functions in FunctionGitRepo.
    bundle = Path(json.loads((deploying / "state.json").read_text())["bundle_dir"])
    (bundle / ".DS_Store").write_bytes(b"finder")
    (bundle / "functions" / ".DS_Store").write_bytes(b"finder")
    (bundle / "functions" / "._plot_weather.py").write_bytes(b"apple double")
    (bundle / "functions" / "__pycache__").mkdir()
    (bundle / "functions" / "__pycache__" / "plot_weather.cpython-313.pyc").write_bytes(b"pyc")
    (bundle / "functions" / "Thumbs.db").write_bytes(b"thumbs")
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    for name in ("publish", "upload"):
        code, out = step(capsys, deploying, name, fake, "--confirmed")
        assert code == 0, out  # the hash still matches
    assert [p for p, _ in fake.puts] == ["functions/weather-cs/download_station.py",
                                         "functions/weather-cs/plot_weather.py", "workflows/weather-cs.json"]
