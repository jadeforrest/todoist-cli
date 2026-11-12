from pathlib import Path

from todoist_cli import paths


def test_config_dir_override_wins():
    env = {"TODOIST_CLI_CONFIG_DIR": "/x/cfg", "XDG_CONFIG_HOME": "/x/xdg"}
    assert paths.config_dir(env) == Path("/x/cfg")


def test_config_dir_follows_xdg():
    assert paths.config_dir({"XDG_CONFIG_HOME": "/x/xdg"}) == Path("/x/xdg/todoist-cli")


def test_config_dir_default():
    assert paths.config_dir({}) == Path.home() / ".config" / "todoist-cli"


def test_state_dir_follows_xdg():
    assert paths.state_dir({"XDG_STATE_HOME": "/x/state"}) == Path("/x/state/todoist-cli")


def test_state_dir_default():
    assert paths.state_dir({}) == Path.home() / ".local" / "state" / "todoist-cli"
