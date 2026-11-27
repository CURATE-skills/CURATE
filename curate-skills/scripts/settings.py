"""Plugin settings as they reach the scripts: `--config-<key> '<value>'`, the value filled in by
Claude Code from `${user_config.<key>}` in skill text.

When it isn't filled in (the plugin loaded with --plugin-dir, or the setting never saved), the
skill passes the placeholder as written, or the model may rewrite it into a bare name. Those
leftover forms mean "not set": they are matched exactly, so a real value that only contains such a
word (e.g. a path /data/user_config/catalog) is kept.
"""
from __future__ import annotations

import os

# plugin setting key -> its environment variable
ENV = {"catalog_dir": "CURATE_CATALOG_DIR", "mga_retries": "CURATE_MGA_RETRIES",
       "stubtest_mode": "CURATE_STUBTEST_MODE", "monitor_minutes": "CURATE_MONITOR_MINUTES"}

NOT_FILLED = "default (plugin setting not filled in)"


def leftover_forms(key: str) -> set[str]:
    """The exact texts that are an unfilled placeholder for `key`, not a value."""
    forms = {f"${{user_config.{key}}}", f"user_config.{key}", key, f"<{key}>",
             f"${{{key}}}", f"${key}"}
    if key in ENV:
        forms |= {ENV[key], f"${ENV[key]}", f"${{{ENV[key]}}}"}
    return forms


def is_leftover(raw, key: str) -> bool:
    return raw is not None and str(raw).strip().strip("'\"") in leftover_forms(key)


def plugin_value(raw, key: str):
    """The plugin setting's value, or None when it is empty or an unfilled placeholder."""
    if raw is None:
        return None
    v = str(raw).strip()
    if not v or is_leftover(v, key):
        return None
    return v


def present(value) -> str | None:
    """A flag or environment value, stripped; None if empty."""
    if value is None:
        return None
    v = str(value).strip()
    return v or None


def resolve(flag, key: str, config, default, cast, env_var: str | None = None,
            flag_source: str = "flag") -> tuple:
    """flag > environment variable > plugin setting > default, as (value, source). A plugin
    setting left as a placeholder falls to the default with source NOT_FILLED. `cast` raising
    ValueError becomes ValueError naming the source (callers turn it into their input error)."""
    env_var = env_var or ENV.get(key)
    cfg = plugin_value(config, key)
    for value, source in ((present(flag), flag_source),
                          (present(os.environ.get(env_var)) if env_var else None, env_var),
                          (cfg, "plugin setting")):
        if value is not None:
            try:
                return cast(value), source
            except ValueError as e:
                raise ValueError(f"bad value {value!r} from {source}: {e}") from e
    return default, (NOT_FILLED if config is not None and str(config).strip() and cfg is None else "default")
