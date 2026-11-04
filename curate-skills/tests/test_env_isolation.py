"""conftest clears deployment settings and credentials for every test."""
import os

from conftest import ENV_VARS, isolated_env_names


def test_names_matched():
    names = ["FAASR_S3_BUCKET", "FAASR_GH_USERNAME", "faasr_action_repo", "GH_PAT", "S3_AccessKey",
             "S3_SECRETKEY", "CURATE_DEPLOY_NAME_SUFFIX", "CURATE_MONITOR_MINUTES", "CURATE_CATALOG_DIR",
             "PATH", "HOME", "GH_PATH", "MY_FAASR_X", "CURATE_GUARD_DEBUG"]
    assert isolated_env_names(names) == names[:9]


def test_none_left_in_a_test():
    assert isolated_env_names(list(os.environ)) == []
    assert not any(v in os.environ for v in ENV_VARS)
