"""Where todoist-cli keeps its configuration and its plan logs.

Config (``.env``, ``plan.toml``) lives in ``$TODOIST_CLI_CONFIG_DIR``, else
``$XDG_CONFIG_HOME/todoist-cli``, else ``~/.config/todoist-cli``. Plan logs go
under ``$XDG_STATE_HOME/todoist-cli``, else ``~/.local/state/todoist-cli``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

APP = "todoist-cli"


def _xdg(env: Mapping[str, str], var: str, fallback: str) -> Path:
    base = env.get(var)
    return Path(base) if base else Path.home() / fallback


def config_dir(env: Mapping[str, str] = os.environ) -> Path:
    override = env.get("TODOIST_CLI_CONFIG_DIR")
    if override:
        return Path(override)
    return _xdg(env, "XDG_CONFIG_HOME", ".config") / APP


def state_dir(env: Mapping[str, str] = os.environ) -> Path:
    return _xdg(env, "XDG_STATE_HOME", ".local/state") / APP
