"""D0: vendored deploy/runtime code, leak checks and the read-only deploy preflight."""
import base64
import io
import json
import shutil
import sys
import types
import urllib.error
from pathlib import Path

import pytest

import deploy as D
import leakcheck as L
from versioned import RID, bundle_of, rebuilt, versioned_bundle

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
    """A run whose Gate 4 published weather-v1 and built this FaaSr bundle from it (tests/versioned.py)."""
    fields, _ = versioned_bundle(tmp_path, {
        "workflow.json": json.dumps(workflow(), indent=2), "functions/plot_weather.py": "def plot_weather(): pass\n",
        "functions/download_station.py": "def download_station(): pass\n"})
    r = tmp_path / ".curate" / "runs" / "r1"
    r.mkdir(parents=True)
    (r / "state.json").write_text(json.dumps({
        "schema_version": 2, "run_id": "r1", "stage": "done", **fields,
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
    _stub_modules(monkeypatch)  # CURATE's Actions-file generator (the deployment check) imports PyGithub


N = "weather-v1"                       # one deployment per version: named after its workflow id
PAYLOAD = f"workflows/{N}/{RID}.json"  # this run's payload
OUT = f"{N}/{RID}/"                    # this run's S3 folder
FNS = [f"functions/{N}/download_station.py", f"functions/{N}/plot_weather.py"]
ACTIONS = [f".github/workflows/{N}-download.yml", f".github/workflows/{N}-plot.yml"]
MARKER = f"workflows/{N}/deployment.json"


class FakeGitHub:
    """Serves GET paths from a dict; records every request. PUT (a contents write) only when
    writable=True: it stores the file and its commit message; paths in fail_put get HTTP 500."""

    def __init__(self, files=None, commits=None, secrets=("GH_PAT", "S3_AccessKey", "S3_SecretKey"),
                 scopes="repo, workflow", default_branch="main", push=True, writable=False, fail_put=()):
        self.files = dict(files or {})
        self.commits = dict(commits or {})
        self.secrets, self.scopes, self.default_branch, self.push = secrets, scopes, default_branch, push
        self.writable, self.fail_put = writable, set(fail_put)
        self.requests, self.puts, self.deletes = [], [], []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if req.get_method() == "DELETE":
            assert self.writable, "a delete was attempted where none is allowed"
            p = req.full_url.split("/contents/", 1)[1]
            body = json.loads(req.data)
            assert p in self.files and body["sha"] == D.blob_sha(self.files[p]), "delete of a changed/absent file"
            self.deletes.append((p, body))
            del self.files[p]
            return self._ok({"commit": {}})
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
                return self._ok({"content": base64.b64encode(self.files[p].encode()).decode(), "sha": D.blob_sha(self.files[p])})
            kids = sorted(f for f in self.files if f.startswith(p + "/") and "/" not in f[len(p) + 1:])
            if kids:  # a folder: its files
                return self._ok([{"name": f.rsplit("/", 1)[1], "path": f, "type": "file",
                                  "sha": D.blob_sha(self.files[f])} for f in kids])
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


def test_preflight_ready_lists_the_versions_deployment_and_the_runs_payload(run, env, capsys):
    fake = FakeGitHub()
    code, out = pre(capsys, run, fake)
    assert code == 0 and out["ok"], out["problems"]
    assert out["deploy_name"] == N and out["approved_name"] == "weather" and out["run_id"] == RID
    assert [t["path"] for t in out["targets"]] == [*FNS, PAYLOAD, *ACTIONS, MARKER]
    assert {t["path"]: t["scope"] for t in out["targets"]}[PAYLOAD] == "run"
    assert all(t["scope"] == "deployment" for t in out["targets"] if t["path"] != PAYLOAD)
    assert out["function_git_repo"] == {
        "download_station": f"alice/faasr-actions/main/functions/{N}/download_station.py",
        "plot_weather": f"alice/faasr-actions/main/functions/{N}/plot_weather.py"}
    assert all(not t["exists"] for t in out["targets"]) and out["deployment"]["state"] == "absent"
    assert out["secrets"]["missing"] == [] and out["token_source"] == "GH_PAT"
    assert "nothing was written" in out["summary"]
    assert all(r.get_method() == "GET" for r in fake.requests)  # read-only
    assert [(t["kind"], t["prefix"]) for t in out["s3_targets"]] == [("outputs", OUT), ("logs", None)]
    assert out["s3_targets"][1]["shown"] == f"FaaSrLog/{N}/<invocation timestamp>/<invocation id>/"
    assert not out["s3_targets"][0]["exists"] and out["s3"]["checked"]
    assert out["existing_paths"] == []


def test_deploy_copy_puts_each_runs_outputs_in_its_own_folder_and_leaves_the_bundle_alone(run, env, capsys):
    wf_path = (bundle_of(run) / "workflow.json")
    before = wf_path.read_text()
    one, _, s3_one = D.deploy_copy(workflow(), N, "run-a", "alice", "faasr-actions", "main")
    two, _, s3_two = D.deploy_copy(workflow(), N, "run-b", "alice", "faasr-actions", "main")
    assert {a["Arguments"]["folder"] for a in one["ActionList"].values()} == {f"{N}/run-a"}
    assert {a["Arguments"]["folder"] for a in two["ActionList"].values()} == {f"{N}/run-b"}
    assert s3_one[0]["prefix"] != s3_two[0]["prefix"]  # two runs never share an S3 folder
    assert one["WorkflowName"] == two["WorkflowName"] == N and one["FunctionGitRepo"] == two["FunctionGitRepo"]
    assert one["ActionList"]["plot"]["Arguments"]["input1"] == "station.csv"
    pre(capsys, run, FakeGitHub())
    assert wf_path.read_text() == before


def test_two_folders_stay_apart_under_the_runs_folder():
    wf = workflow()
    wf["ActionList"]["plot"]["Arguments"]["folder"] = "plots"
    copy_wf, _, s3 = D.deploy_copy(wf, N, "r", "alice", "faasr-actions", "main")
    assert copy_wf["ActionList"]["download"]["Arguments"]["folder"] == f"{N}/r/weather_data"
    assert copy_wf["ActionList"]["plot"]["Arguments"]["folder"] == f"{N}/r/plots"
    assert [t["prefix"] for t in s3 if t["kind"] == "outputs"] == [f"{N}/r/"]


def test_preflight_fills_placeholders_in_the_deploy_copy_only(run, env, capsys, monkeypatch):
    wf = workflow()
    wf["DataStores"]["S3"]["Bucket"] = "<FAASR_S3_BUCKET>"
    wf["ComputeServers"]["GH"]["UserName"] = "<FAASR_GH_USERNAME>"
    wf_path = (bundle_of(run) / "workflow.json")
    wf_path.write_text(json.dumps(wf))
    rebuilt(run)
    monkeypatch.setenv("FAASR_S3_BUCKET", "b")
    s3 = FakeS3()
    code, out = pre(capsys, run, FakeGitHub(), s3=s3)
    assert out["ok"], out["problems"]
    assert out["filled_placeholders"] == {"ComputeServers.GH.UserName": "$FAASR_GH_USERNAME = alice",
                                          "DataStores.S3.Bucket": "$FAASR_S3_BUCKET = b"}
    assert s3.made_with[0]["Bucket"] == "b" and s3.made_with[1:] == (FAKE["S3_AccessKey"], FAKE["S3_SecretKey"])
    assert "<FAASR_S3_BUCKET>" in wf_path.read_text()  # the approved bundle is unchanged
    assert "| `DataStores.S3.Bucket` | `<FAASR_S3_BUCKET>` | `b` | placeholder $FAASR_S3_BUCKET |" in \
        Path(out["diff_file"]).read_text()


def test_preflight_refuses_an_existing_run_folder_and_reports_curates(run, env, capsys):
    s3 = FakeS3(objects={f"{OUT}station.csv", f"FaaSrLog/{N}/2026/x.txt",  # other runs' logs: not this run's
                         "weather_data/station.csv", "FaaSrLog/weather/2026/x.txt"})
    code, out = pre(capsys, run, FakeGitHub(), s3=s3)
    assert code == 1
    assert out["existing_paths"] == [f"s3://b/{OUT}"]
    assert sum("already holds objects" in x for x in out["problems"]) == 1
    assert out["s3"]["curate_prefixes"] == ["weather_data/", "FaaSrLog/weather/"]
    assert "path(s) above already exist; every path the deploy writes must be new" in out["summary"]
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
    bundle = bundle_of(run)
    if case == "no_folder":
        wf = workflow()
        del wf["ActionList"]["plot"]["Arguments"]["folder"]
        (bundle / "workflow.json").write_text(json.dumps(wf))
    if case == "external":
        (bundle / "external_inputs.json").write_text(json.dumps([{"file": "in.csv", "s3_path": "weather_data/in.csv"}]))
    rebuilt(run)
    code, out = pre(capsys, run, FakeGitHub(), s3=s3)
    assert code == 1 and any(needle in p for p in out["problems"]), out["problems"]
    assert all(v not in json.dumps(out) for v in FAKE.values())


def test_preflight_never_prints_or_writes_secrets(run, env, capsys):
    before = {p: p.read_bytes() for p in run.parent.parent.parent.rglob("*") if p.is_file()}
    code, out = pre(capsys, run, FakeGitHub())
    text = json.dumps(out)
    assert all(v not in text for v in FAKE.values())
    after = {p: p.read_bytes() for p in run.parent.parent.parent.rglob("*") if p.is_file()}
    diff = run / "deploy" / "preflight-diff.md"  # the one local file: the full comparison
    pid = run.parent.parent / "project-id"       # and this project's id (made on first use)
    assert {k: v for k, v in after.items() if k not in (diff, pid)} == before and out["diff_file"] == str(diff)
    assert all(v.encode() not in after[diff] for v in FAKE.values())


def test_preflight_leaves_curate_files_alone_and_reports_them(run, env, capsys):
    curate = {"workflows/weather.json": json.dumps({"WorkflowName": "weather"}),
              "functions/plot_weather.py": "x", ".github/workflows/weather-plot.yml": "name: weather-plot\n"}
    code, out = pre(capsys, run, FakeGitHub(files=curate))
    assert out["ok"] and sorted(out["curate_files"]) == sorted(curate)
    assert not any(t["path"] in curate for t in out["targets"])
    assert "left untouched" in out["summary"]


def test_files_of_the_deployment_without_its_marker_are_refused(run, env, capsys):
    files = {FNS[1]: "x", ACTIONS[1]: "name: other\n"}  # a deploy that did not finish (or someone else's)
    code, out = pre(capsys, run, FakeGitHub(files=files))
    assert code == 1 and out["deployment"]["state"] == "partial"
    assert sum("already exists without the deployment's marker" in x for x in out["problems"]) == 2
    assert "already exists — refused" in out["summary"]


def test_a_runs_payload_is_never_rewritten(run, env, capsys):
    code, out = pre(capsys, run, FakeGitHub(files={PAYLOAD: json.dumps({"WorkflowName": N})}))
    assert code == 1 and any(f"{PAYLOAD} already exists" in x and "never rewritten" in x for x in out["problems"])


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
    (bundle_of(run) / "workflow.json").write_text(json.dumps(wf))
    rebuilt(run)
    code, out = pre(capsys, run)
    probs = " | ".join(out["problems"])
    assert code == 1 and "placeholders" in probs and "FAASR_S3_BUCKET not set" in probs
    assert "DataStores.S3.Bucket" in probs
    assert "native list" in probs and "no GitHub token" in probs and not out["github_checked"]


def test_preflight_refuses_unapproved_or_changed_bundles(run, env, capsys):
    s = json.loads((run / "state.json").read_text())
    s["history"].append({"gate": 4, "decision": "revise", "text": "use csv"})
    (run / "state.json").write_text(json.dumps(s))
    (bundle_of(run) / "README.md").write_text("edited after it was built\n")
    code, out = pre(capsys, run, FakeGitHub())
    probs = " | ".join(out["problems"])
    assert "superseded" in probs and "the bundle changed after it was built" in probs


def test_a_suffix_is_optional_and_simple(run, env, capsys, monkeypatch):
    for bad in ("a/b", "../x"):
        code, out = pre(capsys, run, FakeGitHub(), f"--name-suffix={bad}")
        assert code == 2 and "suffix" in out["error"]
    code, out = pre(capsys, run, FakeGitHub(), "--name-suffix=-alice")
    assert out["deploy_name"] == f"{N}-alice" and PAYLOAD.replace(N, f"{N}-alice") in [t["path"] for t in out["targets"]]
    monkeypatch.setenv("CURATE_DEPLOY_NAME_SUFFIX", "-bob")
    code, out = pre(capsys, run, FakeGitHub())
    assert out["deploy_name"] == f"{N}-bob" and "CURATE_DEPLOY_NAME_SUFFIX" in out["name_source"]


def test_output_with_a_leaked_token_is_withheld(run, env, capsys, monkeypatch):
    wf = workflow()
    wf["WorkflowName"] = "weather-" + FAKE["S3_SecretKey"]  # a secret pasted into the bundle (echoed)
    (bundle_of(run) / "workflow.json").write_text(json.dumps(wf))
    rebuilt(run)
    code, out = pre(capsys, run, FakeGitHub())
    assert code == 1 and out == {"ok": False, "error": "output withheld: contains secret values",
                                 "leaked_secret_names": ["S3_SecretKey"]}


# ── deploy copy vs approved bundle ──

def test_summary_diffs_the_deploy_copy_field_by_field(run, env, capsys, monkeypatch):
    wf = workflow()
    wf["DataStores"]["S3"]["Bucket"] = "<FAASR_S3_BUCKET>"
    wf["FunctionGitRepo"]["plot_weather"] = "<FAASR_GH_USERNAME>/<FAASR_ACTION_REPO>/main/functions/plot_weather.py"
    (bundle_of(run) / "workflow.json").write_text(json.dumps(wf))
    rebuilt(run)
    monkeypatch.setenv("FAASR_S3_BUCKET", "b")
    code, out = pre(capsys, run, FakeGitHub())
    assert out["ok"], out["problems"]
    rows = {r["field"]: r for r in out["deploy_diff"]}
    assert rows["FunctionGitRepo.plot_weather"]["reason"] == \
        "placeholder $FAASR_GH_USERNAME, $FAASR_ACTION_REPO; per-deployment function folder"
    assert set(rows) == {"WorkflowName", "ActionList.download.Arguments.folder", "ActionList.plot.Arguments.folder",
                         "FunctionGitRepo.download_station", "FunctionGitRepo.plot_weather", "DataStores.S3.Bucket"}
    assert rows["WorkflowName"]["approved"] == "weather" and rows["WorkflowName"]["deploy"] == N
    assert rows["DataStores.S3.Bucket"]["reason"] == "placeholder $FAASR_S3_BUCKET"
    assert rows["ActionList.plot.Arguments.folder"]["reason"] == "the run's own S3 folder (<deploy name>/<run id>)"
    derived = {d["what"]: d["deploy"] for d in out["derived_changes"]}
    assert derived["S3 log prefix (FaaSr)"] == f"FaaSrLog/{N}/"
    assert derived["workflow JSON in the repo (this run's payload)"] == PAYLOAD
    summary, full = out["summary"], Path(out["diff_file"]).read_text()
    assert "Deploy copy vs approved bundle" not in summary and "| Field |" not in summary  # checked silently
    assert f"Full comparison with the approved bundle: `{out['diff_file']}`" in summary
    assert f"| `ActionList.plot.Arguments.folder` | `weather_data` | `{N}/{RID}` | the run's own S3 folder" in full
    assert f"| S3 log prefix (FaaSr) (derived from the name) | `FaaSrLog/weather/` | `FaaSrLog/{N}/` |" in full


def test_a_diff_outside_the_allowed_fields_is_refused(run, env, capsys, monkeypatch):
    real = D.deploy_copy

    def tampered(*a, **kw):
        wf, targets, s3 = real(*a, **kw)
        wf["ActionList"]["plot"]["Arguments"]["output1"] = "other.png"
        return wf, targets, s3
    monkeypatch.setattr(D, "deploy_copy", tampered)
    code, out = pre(capsys, run, FakeGitHub())
    assert code == 1 and any("ActionList.plot.Arguments.output1 ('plot.png' → " in p and "which is not an allowed" in p
                             for p in out["problems"])
    assert {r["field"]: r["reason"] for r in out["deploy_diff"]}["ActionList.plot.Arguments.output1"] == "NOT ALLOWED"
    lines = [l for l in out["summary"].splitlines() if "ActionList.plot.Arguments.output1" in l]
    assert lines == ["- **Problem:** the deploy copy differs from the approved bundle at "
                     "ActionList.plot.Arguments.output1 ('plot.png' → 'other.png'), which is not an allowed deploy "
                     "change (refusing: the deploy must run what Gate 4 approved)"]


def test_the_preflight_lists_what_will_be_written_once_each(run, env, capsys):
    code, out = pre(capsys, run, FakeGitHub(files={PAYLOAD: "{}"}), s3=FakeS3(objects={f"{OUT}old.csv"}))
    head = out["summary"].split("\n\n")
    assert head[1].splitlines() == [
        f"Deploys as: `{N}` (name from: the version's workflow id), run `{RID}`",
        "Deployment (one per version): not in the repo yet — publish and register write it"]
    assert head[2].splitlines() == [
        "GitHub (`alice/faasr-actions`, branch `main`):",
        f"- `{FNS[0]}` — new", f"- `{FNS[1]}` — new",
        f"- `{PAYLOAD}` — already exists — refused (this run's payload)",
        f"- `{ACTIONS[0]}` — new", f"- `{ACTIONS[1]}` — new", f"- `{MARKER}` — new"]
    assert head[3].splitlines() == ["S3 (`b`), written when the workflow runs:",
                                    f"- outputs folder: `{OUT}` — already exists — refused",
                                    f"- logs folder: `FaaSrLog/{N}/<invocation timestamp>/<invocation id>/` "
                                    "(FaaSr's, one per invocation)"]
    for path in (PAYLOAD, OUT):
        assert out["summary"].count(path) == 1, path  # once: no "paths that exist" list, no per-path problem line
    assert out["summary"].count("**Problem:** 2 path(s) above already exist") == 1
    assert len(out["problems"]) == 2  # the JSON keeps every problem


# ── retrying a partial deploy: --overwrite-own-deploy ──

OWN_FILES = {PAYLOAD: json.dumps({"WorkflowName": N}), FNS[1]: "x"}
OWN_COMMITS = {FNS[1]: "Add/update plot_weather (curate-skills)"}


def write_log(run, *entries):
    (run / "deploy").mkdir(exist_ok=True)
    (run / "deploy" / "deploy-log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))


def logged(path=None, **kw):
    base = {"run_id": "r1", "deploy_name": N, "step": "publish"}
    if path:
        base.update(path=path, repo="alice/faasr-actions", branch="main")
    return {**base, **kw}


def test_overwrite_own_is_off_by_default(run, env, capsys):
    write_log(run, *(logged(p) for p in OWN_FILES))
    code, out = pre(capsys, run, FakeGitHub(files=OWN_FILES, commits=OWN_COMMITS))
    assert code == 1 and sorted(out["refused_paths"]) == sorted(OWN_FILES) and out["overwrite_paths"] == []
    assert any("--overwrite-own-deploy" in p for p in out["problems"])


def test_overwrite_own_allows_only_logged_paths_of_this_run(run, env, capsys):
    write_log(run, *(logged(p) for p in OWN_FILES), logged(step="invoke", bucket="b", prefix=OUT))
    s3 = FakeS3(objects={f"{OUT}station.csv"})
    code, out = pre(capsys, run, FakeGitHub(files=OWN_FILES, commits=OWN_COMMITS), "--overwrite-own-deploy", s3=s3)
    assert sorted(out["overwrite_paths"]) == sorted([*OWN_FILES, f"s3://b/{OUT}"])
    assert code == 0 and "will be overwritten (--overwrite-own-deploy)" in out["summary"], out["problems"]


@pytest.mark.parametrize("entry", [
    logged(FNS[1], run_id="other-run"),             # another run's log line
    logged(FNS[1], deploy_name="weather-x"),        # another deployment
    logged(FNS[1], repo="bob/faasr-actions"),       # another repo
])
def test_overwrite_own_ignores_log_lines_that_are_not_this_deploy(run, env, capsys, entry):
    write_log(run, entry)
    code, out = pre(capsys, run, FakeGitHub(files={FNS[1]: "x"}, commits=OWN_COMMITS), "--overwrite-own-deploy")
    assert code == 1 and out["refused_paths"] == [FNS[1]]


def test_overwrite_own_refuses_a_payload_someone_else_changed(run, env, capsys):
    write_log(run, logged(PAYLOAD), "not json")
    code, out = pre(capsys, run, FakeGitHub(files={PAYLOAD: json.dumps({"WorkflowName": "other"})}),
                    "--overwrite-own-deploy")
    assert code == 1 and out["refused_paths"] == [PAYLOAD]
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
    assert "S3_SECRETKEY" not in stubtest._env()


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

RID2 = "20261125-090000-ef01"


@pytest.fixture
def deploying(run):
    """The run after Gate 4 was approved with Approve & deploy."""
    s = json.loads((run / "state.json").read_text())
    s.update(stage="deploy", deploy_choice="yes")
    (run / "state.json").write_text(json.dumps(s))
    return run


@pytest.fixture
def second(run):
    """A second run (r2, its own run id) of the same version, at deploy, with the same bundle."""
    fields, _ = versioned_bundle(run.parent.parent.parent, {
        "workflow.json": json.dumps(workflow(), indent=2), "functions/plot_weather.py": "def plot_weather(): pass\n",
        "functions/download_station.py": "def download_station(): pass\n"}, rid=RID2)
    r = run.parent / "r2"
    r.mkdir()
    (r / "state.json").write_text(json.dumps({
        "schema_version": 2, "run_id": "r2", "stage": "deploy", "deploy_choice": "yes", **fields,
        "history": [{"gate": 4, "decision": "approve"}]}))
    return r


@pytest.fixture
def runners(monkeypatch):
    """Stand-ins for CURATE's register and invoke code; they record what they were given. register
    writes what CURATE's generator writes, so a later run finds the deployment byte-identical."""
    calls = {}

    def register(copy_wf, token, extra):
        calls.setdefault("register", []).append(copy_wf)
        calls["fake"].files.update(D.expected_actions_files(copy_wf))
        return ["INFO: registered"]

    def invoke(workflow_path, token, extra):
        calls.setdefault("invoke", []).append(workflow_path)
        return {"invocation_id": "inv-123", "invocation_timestamp": "2026-11-01-12-00-00", "log": []}
    monkeypatch.setattr(D, "RUNNERS", {"register": register, "invoke": invoke})
    return calls


def step(capsys, run, name, fake, *extra, s3=None):
    code = D.main([name, "--run", str(run), *extra], opener=fake, s3_factory=s3 or FakeS3())
    return code, json.loads(capsys.readouterr().out)


def deploy_all(capsys, run, fake):
    for name in D.STEPS:
        code, out = step(capsys, run, name, fake, "--confirmed")
        assert code == 0 and out["ok"], (name, out.get("problems") or out)
    return out


def log_lines(run):
    p = run / "deploy" / "deploy-log.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def marker_of(fake):
    return json.loads(fake.files[MARKER])


def test_a_step_without_confirmed_writes_nothing_and_says_what_it_would_do(deploying, env, capsys):
    fake = FakeGitHub(writable=True)
    code, out = step(capsys, deploying, "publish", fake)
    assert code == 1 and out.get("needs_confirmation"), out.get("problems") or out
    assert "ask the user first" in out["error"]
    assert out["plan"]["paths"] == FNS
    assert out["plan"]["repo"] == "alice/faasr-actions" and fake.puts == [] and log_lines(deploying) == []
    assert not json.loads((deploying / "state.json").read_text()).get("deploy")


def test_all_four_steps_in_order(deploying, env, capsys, runners):
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    out = deploy_all(capsys, deploying, fake)
    # publish: the version's function files; upload: this run's payload; register: the marker
    assert [p for p, _ in fake.puts] == [*FNS, PAYLOAD, MARKER]
    assert all("(curate-skills)" in b["message"] and b["branch"] == "main" for _, b in fake.puts)
    assert fake.files[FNS[1]] == "def plot_weather(): pass\n"
    uploaded = json.loads(fake.files[PAYLOAD])
    assert uploaded["WorkflowName"] == N
    assert {a["Arguments"]["folder"] for a in uploaded["ActionList"].values()} == {f"{N}/{RID}"}
    assert json.loads((deploying / "deploy" / "workflow.json").read_text()) == uploaded
    assert runners["register"] == [uploaded] and runners["invoke"] == [PAYLOAD]
    # the marker: this version, the files it wrote, this project
    m = marker_of(fake)
    s = json.loads((deploying / "state.json").read_text())
    assert m["curate_deployment"] == 1 and m["deploy_name"] == N and m["workflow_id"] == "weather-v1"
    assert m["content_hash"] == D.version_content_hash(s) and m["content_hash"]
    assert sorted(m["files"]) == sorted([*FNS, *ACTIONS])
    assert all(m["files"][f] == D.blob_sha(fake.files[f]) for f in m["files"])
    assert m["projects"] == [(deploying.parent.parent / "project-id").read_text().strip()]
    # state, log, stage
    assert all(s["deploy"]["steps"][x]["ok"] for x in D.STEPS)
    assert s["deploy"]["scheme"] == "version" and s["deploy"]["deployment"] == "made"
    assert s["deploy"]["payload"] == PAYLOAD and s["deploy"]["invocation_id"] == "inv-123"
    assert s["stage"] == "monitor" and out["next_skill"] == "curate-skills:curate-monitor"
    assert s["history"][-1]["decision"] == "deployed"
    import spec as S
    S.main(["next", "--session", str(deploying)])
    assert json.loads(capsys.readouterr().out)["next_skill"] == "curate-skills:curate-monitor"
    logged_ = [(e["step"], e.get("path") or e.get("prefix")) for e in log_lines(deploying)]
    assert logged_ == [("publish", FNS[0]), ("publish", FNS[1]), ("upload", PAYLOAD),
                       ("register", ACTIONS[0]), ("register", ACTIONS[1]), ("register", MARKER),
                       ("invoke", OUT), ("invoke", f"FaaSrLog/{N}/2026-11-01-12-00-00/inv-123/")]
    assert all(e["run_id"] == "r1" and e["deploy_name"] == N for e in log_lines(deploying))
    # secrets nowhere
    for f in [deploying / "state.json", deploying / "deploy" / "deploy-log.jsonl", deploying / "deploy" / "workflow.json"]:
        assert all(v not in f.read_text() for v in FAKE.values())
    assert all(v not in json.dumps(b) for _, b in fake.puts for v in FAKE.values())


def test_a_second_run_of_the_version_reuses_its_deployment_and_gets_its_own_folders(deploying, second, env, capsys,
                                                                                     runners):
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    deploy_all(capsys, deploying, fake)
    n = len(fake.puts)
    code, out = step(capsys, second, "publish", fake)
    assert code == 1 and out["needs_confirmation"] and out["plan"]["paths"] == [] and out["plan"]["reuses"] == N
    assert "nothing is written" in out["plan"]["what"]
    assert "already in the repo for this version, byte-identical — reused" in out["preflight_summary"]
    out = deploy_all(capsys, second, fake)
    payload2 = f"workflows/{N}/{RID2}.json"
    assert [p for p, _ in fake.puts[n:]] == [payload2]  # only its payload: the deployment is reused
    assert len(runners["register"]) == 1 and runners["invoke"] == [PAYLOAD, payload2]
    one, two = json.loads(fake.files[PAYLOAD]), json.loads(fake.files[payload2])
    assert {a["Arguments"]["folder"] for a in two["ActionList"].values()} == {f"{N}/{RID2}"}
    assert one["FunctionGitRepo"] == two["FunctionGitRepo"] and one["WorkflowName"] == two["WorkflowName"] == N
    s2 = json.loads((second / "state.json").read_text())
    assert s2["deploy"]["deployment"] == "reused"
    assert all(s2["deploy"]["steps"][x].get("reused") == N for x in ("publish", "register"))
    prefixes = {e.get("prefix") for e in log_lines(second) if e.get("prefix")}
    assert f"{N}/{RID2}/" in prefixes and OUT not in prefixes  # the two runs' S3 folders differ
    assert [e["path"] for e in log_lines(second) if e.get("path")] == [payload2]


def test_reusing_from_another_project_adds_it_to_the_marker(deploying, second, env, capsys, runners):
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    deploy_all(capsys, deploying, fake)
    m = marker_of(fake)
    m["projects"] = ["0123456789abcdef"]  # made by another project
    fake.files[MARKER] = json.dumps(m)
    code, out = step(capsys, second, "publish", fake)
    assert out["plan"]["paths"] == [MARKER] and "only this project is added to its marker" in out["plan"]["what"]
    step(capsys, second, "publish", fake, "--confirmed")
    pid = (second.parent.parent / "project-id").read_text().strip()
    assert marker_of(fake)["projects"] == sorted(["0123456789abcdef", pid])
    assert marker_of(fake)["files"] == m["files"]
    assert [e for e in log_lines(second) if e.get("path") == MARKER][0]["shared"] is True


def test_a_marker_naming_another_version_is_refused_with_the_ways_out(deploying, second, env, capsys, runners):
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    deploy_all(capsys, deploying, fake)
    m = marker_of(fake)
    m.update(workflow_id="weather-v1", content_hash="f" * 64)  # another project's v1: other content
    fake.files[MARKER] = json.dumps(m)
    n = len(fake.puts)
    code, out = step(capsys, second, "publish", fake, "--confirmed")
    msg = next(p for p in out["problems"] if "made from another version" in p)
    assert "--teardown --workflow weather-v1 --all" in msg and "update-deployment --workflow weather-v1" in msg
    assert "CURATE_DEPLOY_NAME_SUFFIX" in msg
    assert code == 1 and len(fake.puts) == n and "ANOTHER version — refused" in out["preflight_summary"]


def test_a_deployment_of_this_version_whose_files_differ_is_refused_with_update_deployment(
        deploying, second, env, capsys, runners):
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    deploy_all(capsys, deploying, fake)
    fake.files[FNS[1]] += "# edited in the repo\n"
    del fake.files[ACTIONS[0]]
    code, out = step(capsys, second, "publish", fake, "--confirmed")
    msg = next(p for p in out["problems"] if "its files are not what this version deploys" in p)
    assert f"{FNS[1]} differs" in msg and f"{ACTIONS[0]} is missing" in msg
    assert "`deploy.py update-deployment --workflow weather-v1`" in msg and code == 1


def test_a_run_that_started_deploying_under_its_own_name_is_not_continued(deploying, env, capsys):
    s = json.loads((deploying / "state.json").read_text())
    s["deploy"] = {"deploy_name": "weather-1124-1200", "suffix": "-1124-1200", "steps": {"publish": {"ok": True}}}
    (deploying / "state.json").write_text(json.dumps(s))
    code, out = step(capsys, deploying, "upload", FakeGitHub(writable=True), "--confirmed")
    assert code == 1 and any("its own per-run name" in p and "--start-at deploy --workflow weather-v1" in p
                             for p in out["problems"])
    D.main(["status", "--run", str(deploying)])
    st = json.loads(capsys.readouterr().out)
    assert st["per_run_name"] and st["next_step"] is None


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
    ("stage", "not deploy"),
    ("no_deploy", "without deploying"),
    ("actions_env", "GITHUB_REPOSITORY is set here"),
    ("version_changed", "approved version weather-v1 changed after it was published"),
    ("version_missing", "approved version weather-v1 is missing"),
    ("built_bundle_changed", "the bundle changed after it was built"),
    ("pre_version", "approved before workflow versions"),
])
def test_steps_refuse_before_writing(deploying, env, capsys, monkeypatch, case, needle):
    s = json.loads((deploying / "state.json").read_text())
    if case == "version_changed":
        (Path(s["version_dir"]) / "workflow.yml").write_text("class: Workflow\nedited: true\n")
    elif case == "version_missing":
        s["version_dir"] = str(deploying / "gone")
    elif case == "built_bundle_changed":
        (bundle_of(deploying) / "functions" / "plot_weather.py").write_text("def plot_weather(): return 1\n")
    elif case == "pre_version":  # the old emit's bundle, no version: no longer deployed
        for k in ("workflow_id", "version_dir", "version_hash", "execution"):
            s.pop(k)
        s["bundle_dir"] = str(deploying / "old-bundle")
    if case == "stage":
        s["stage"] = "finalize"
    if case == "no_deploy":
        s["deploy_choice"] = "no"
    if case == "actions_env":
        monkeypatch.setenv("GITHUB_REPOSITORY", "someone/else")
    (deploying / "state.json").write_text(json.dumps(s))
    fake = FakeGitHub(writable=True)
    code, out = step(capsys, deploying, "publish", fake, "--confirmed")
    assert code == (2 if case == "pre_version" else 1) and needle in json.dumps(out), out  # 2: not deployable at all
    assert fake.puts == [] and log_lines(deploying) == []


def test_a_secret_in_function_code_is_never_pushed(deploying, env, capsys):
    bundle = bundle_of(deploying)
    (bundle / "functions" / "plot_weather.py").write_text(f"KEY = '{FAKE['S3_SecretKey']}'\n")
    rebuilt(deploying)  # as if it had been built like this
    fake = FakeGitHub(writable=True)
    code, out = step(capsys, deploying, "publish", fake, "--confirmed")
    assert code == 1 and out["failed"] and "contains secret values (S3_SecretKey)" in out["error"]
    assert fake.puts == [] and log_lines(deploying) == []  # every file is checked before the first push
    assert FAKE["S3_SecretKey"] not in json.dumps(out) + (deploying / "state.json").read_text()


def test_a_failed_step_can_be_retried_only_with_overwrite_own_deploy(deploying, env, capsys):
    fake = FakeGitHub(writable=True, fail_put={FNS[1]})
    code, out = step(capsys, deploying, "publish", fake, "--confirmed")
    assert code == 1 and out["failed"] and "HTTP 500" in out["error"]
    # a session from before runs kept their own state is named by its session id (its records are its own)
    assert out["resume_command"] == f"/curate-skills:curate-deploy --run {json.loads((deploying / 'state.json').read_text())['run_id']}"
    s = json.loads((deploying / "state.json").read_text())
    assert s["stage"] == "deploy" and s["deploy"]["steps"]["publish"]["ok"] is False
    assert [e["path"] for e in log_lines(deploying)] == [FNS[0]]
    fake.fail_put = set()
    code, out = step(capsys, deploying, "publish", fake, "--confirmed")  # its own file now exists
    assert code == 1 and out["problems"] and "--overwrite-own-deploy" in " ".join(out["problems"])
    code, out = step(capsys, deploying, "publish", fake, "--confirmed", "--overwrite-own-deploy")
    assert code == 0 and out["ok"], out
    assert sum(p == FNS[0] for p, _ in fake.puts) == 2


def test_status_reports_done_and_next(deploying, env, capsys, runners):
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    step(capsys, deploying, "publish", fake, "--confirmed")
    D.main(["status", "--run", str(deploying)])
    out = json.loads(capsys.readouterr().out)
    assert out["done"] == ["publish"] and out["next_step"] == "upload" and out["deploy_log"]
    assert out["deployment"] == "made" and out["payload"] == PAYLOAD and "reuse_possible" not in out


def test_publish_never_pushes_os_metadata(deploying, env, capsys, runners):
    # The same files tree_hash ignores: they can appear after approval without changing the hash,
    # and publish only pushes functions/<fn>.py for the functions in FunctionGitRepo.
    bundle = bundle_of(deploying)
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
    assert [p for p, _ in fake.puts] == [*FNS, PAYLOAD]


def test_curates_yaml_generator_is_deterministic(monkeypatch):
    """The deployment check compares deployed Actions files byte for byte with freshly generated
    ones; that only works if CURATE's generator gives the same output for the same input."""
    _stub_modules(monkeypatch)
    from vendor.curate_deploy import register_workflow as R
    wf = D.deploy_copy(workflow(secrets=["NOAA_TOKEN"]), N, RID, "alice", "faasr-actions", "main")[0]
    secrets = R.generate_github_secret_imports(wf) + "\n" + R.generate_user_defined_secret_imports(wf)
    a = R.generate_serverless_yaml(f"{N}-plot", wf["ActionContainers"]["plot"], secrets)
    b = R.generate_serverless_yaml(f"{N}-plot", wf["ActionContainers"]["plot"], secrets)
    assert a == b and a.startswith(f"name: {N}-plot\n")
    assert D.expected_actions_files(wf) == D.expected_actions_files(wf)
    # the run id is only in the payload: every run of a version gets the same Actions files
    other = D.deploy_copy(workflow(secrets=["NOAA_TOKEN"]), N, RID2, "alice", "faasr-actions", "main")[0]
    assert D.expected_actions_files(other) == D.expected_actions_files(wf)


def test_a_non_deterministic_generator_refuses_the_check(deploying, env, capsys, runners, monkeypatch):
    from vendor.curate_deploy import register_workflow as R
    calls = iter(range(100))
    monkeypatch.setattr(R, "generate_serverless_yaml", lambda name, image, secrets: f"name: {name}\n# {next(calls)}\n")
    code, out = step(capsys, deploying, "publish", FakeGitHub(writable=True), "--confirmed")
    assert code == 1 and any("gave different output on two calls" in p for p in out["problems"])


# ── update-deployment: re-push the version's files (the same version only) ──

@pytest.fixture
def deployed_version(deploying, env, capsys, runners, monkeypatch):
    """r1 deployed weather-v1; update-deployment builds the version in memory (here: the bundle's files)."""
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    deploy_all(capsys, deploying, fake)
    s = json.loads((deploying / "state.json").read_text())
    s["monitor"] = {"finished": True, "result": "success"}  # the run is over: nothing reads the branch now
    (deploying / "state.json").write_text(json.dumps(s))
    bundle = bundle_of(deploying)
    built = {"workflow": workflow(), "files": {f"functions/{b}": (bundle / "functions" / b).read_text()
                                               for b in ("download_station.py", "plot_weather.py")}}
    monkeypatch.setattr(D, "_version_build", lambda project, wid: (built, s, Path(s["version_dir"])))
    monkeypatch.setattr(D, "main_problems", lambda built, vdir, s: [])
    global _project
    _project = deploying.parent.parent.parent
    return deploying, fake


_project = None


def update(capsys, fake, *extra, project=None):
    project = project or _project
    code = D.main(["update-deployment", "--workflow", "weather-v1", "--project", str(project), *extra], opener=fake)
    return code, json.loads(capsys.readouterr().out)


def test_update_deployment_repushes_only_the_files_that_differ(deployed_version, capsys):
    run, fake = deployed_version
    fake.files[FNS[1]] = "edited in the repo\n"
    del fake.files[ACTIONS[0]]
    n = len(fake.puts)
    code, out = update(capsys, fake)
    assert code == 1 and out["needs_confirmation"] and fake.puts[n:] == []
    assert out["plan"]["paths"] == [ACTIONS[0], FNS[1], MARKER]
    code, out = update(capsys, fake, "--confirmed")
    assert code == 0 and out["ok"], out
    assert [p for p, _ in fake.puts[n:]] == [FNS[1], ACTIONS[0], MARKER]
    assert fake.files[FNS[1]] == "def plot_weather(): pass\n"
    assert all(marker_of(fake)["files"][f] == D.blob_sha(fake.files[f]) for f in [*FNS, *ACTIONS])
    code, out = update(capsys, fake)
    assert code == 0 and "already matches" in out["message"]


def test_update_deployment_refuses_while_a_run_of_the_version_is_in_flight(deployed_version, capsys):
    run, fake = deployed_version
    fake.files[FNS[1]] = "edited in the repo\n"
    s = json.loads((run / "state.json").read_text())
    s["monitor"] = {"finished": False, "result": "timeout"}  # timed out: its Actions may still run
    (run / "state.json").write_text(json.dumps(s))
    n = len(fake.puts)
    code, out = update(capsys, fake, "--confirmed")
    msg = next(p for p in out["problems"] if "may still be running" in p)
    ref = s["execution"]["reference"]
    assert ref in msg and "reads the deployment's files from the branch at every action's start" in msg
    assert f"tear that run down first: /curate-skills:curate-deploy --teardown --run {ref}" in msg
    assert code == 1 and fake.puts[n:] == []


def test_update_deployment_refuses_another_version_or_no_deployment(deployed_version, capsys):
    run, fake = deployed_version
    m = marker_of(fake)
    fake.files[MARKER] = json.dumps({**m, "content_hash": "f" * 64})
    n = len(fake.puts)
    code, out = update(capsys, fake, "--confirmed")
    assert code == 1 and any("made from another version" in p and "only re-pushes the same version" in p
                             for p in out["problems"]) and fake.puts[n:] == []
    del fake.files[MARKER]
    code, out = update(capsys, fake, "--confirmed")
    assert code == 1 and any("there is no deployment of weather-v1 to update" in p for p in out["problems"])


def test_update_deployment_refuses_when_a_main_differs_from_the_version(deployed_version, capsys, monkeypatch):
    run, fake = deployed_version
    fake.files[FNS[1]] = "edited\n"
    monkeypatch.setattr(D, "main_problems", lambda built, vdir, s: [
        "functions/plot_weather_main.py is not byte-identical to the version's functions/plot.py"])
    n = len(fake.puts)
    code, out = update(capsys, fake, "--confirmed")
    assert code == 1 and "not byte-identical" in out["problems"][0] and fake.puts[n:] == []


# ── a plain step ships two files: its FaaSr wrapper and its unchanged function ──

PLAIN_MAIN = "def main(inputs, outputs, params):\n    open(outputs['output1'], 'w').write('png')\n"


@pytest.fixture
def plain_run(run):
    """plot_weather is a plain step: FunctionGitRepo lists the wrapper and plot_weather_main.py."""
    bundle = bundle_of(run)
    wf = workflow()
    wf["FunctionGitRepo"]["plot_weather"] = ["alice/faasr-actions/main/functions/plot_weather.py",
                                             "alice/faasr-actions/main/functions/plot_weather_main.py"]
    (bundle / "workflow.json").write_text(json.dumps(wf, indent=2))
    (bundle / "functions" / "plot_weather.py").write_text("# wrapper\ndef plot_weather(folder): pass\n")
    (bundle / "functions" / "plot_weather_main.py").write_text(PLAIN_MAIN)
    rebuilt(run)
    return run


def test_deploy_copy_keeps_both_files_of_a_plain_step(plain_run, env, capsys):
    code, out = pre(capsys, plain_run, FakeGitHub())
    assert code == 0 and out["ok"], out["problems"]
    assert out["function_git_repo"]["plot_weather"] == [
        f"alice/faasr-actions/main/functions/{N}/plot_weather.py",
        f"alice/faasr-actions/main/functions/{N}/plot_weather_main.py"]
    assert [t["path"] for t in out["targets"] if t["kind"] == "function"] == [
        *FNS, f"functions/{N}/plot_weather_main.py"]
    assert D.function_files(workflow()) == [("download_station", "download_station.py"),
                                            ("plot_weather", "plot_weather.py")]


def test_publish_pushes_the_unchanged_function_byte_for_byte(plain_run, env, capsys, runners):
    s = json.loads((plain_run / "state.json").read_text())
    s.update(stage="deploy", deploy_choice="yes")
    (plain_run / "state.json").write_text(json.dumps(s))
    fake = FakeGitHub(writable=True)
    runners["fake"] = fake
    code, out = step(capsys, plain_run, "publish", fake, "--confirmed")
    assert code == 0 and out["ok"], out
    assert [p for p, _ in fake.puts] == [*FNS, f"functions/{N}/plot_weather_main.py"]
    assert fake.files[f"functions/{N}/plot_weather_main.py"].encode() == PLAIN_MAIN.encode()


def test_the_deployment_check_compares_both_files_of_a_plain_step(plain_run, env):
    bundle = bundle_of(plain_run)
    copy_wf = D.deploy_copy(json.loads((bundle / "workflow.json").read_text()), N, RID,
                            "alice", "faasr-actions", "main")[0]
    functions = D.bundle_functions(bundle, copy_wf)
    files = D.expected_deployment(functions, copy_wf)
    files[MARKER] = json.dumps({"curate_deployment": 1, "content_hash": "h"})
    gh = D.GitHub(FAKE["GH_PAT"], opener=FakeGitHub(files=files))
    dep = D.deployment_state(gh, "alice", "faasr-actions", "main", copy_wf, functions, "h")
    assert dep["state"] == "same" and f"functions/{N}/plot_weather_main.py" in dep["present"]
    files[f"functions/{N}/plot_weather_main.py"] += "# edited\n"
    gh = D.GitHub(FAKE["GH_PAT"], opener=FakeGitHub(files=files))
    dep = D.deployment_state(gh, "alice", "faasr-actions", "main", copy_wf, functions, "h")
    assert dep["state"] == "changed" and dep["differs"] == [f"functions/{N}/plot_weather_main.py"]
