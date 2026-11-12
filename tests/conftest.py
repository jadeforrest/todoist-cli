import os
import tempfile

import pytest

# Point config and state at an empty scratch dir before todoist_cli.cli is
# imported (it reads .env and resolves plan.toml/plan_logs paths at import), so
# the suite never sees, or writes into, the developer's real configuration.
_SANDBOX = tempfile.mkdtemp(prefix="todoist-cli-tests-")
os.environ["TODOIST_CLI_CONFIG_DIR"] = os.path.join(_SANDBOX, "config")
os.environ["XDG_STATE_HOME"] = os.path.join(_SANDBOX, "state")
os.environ.pop("TODOIST_API_TOKEN", None)
os.environ.pop("GOG_ACCOUNT", None)

TEST_ACCOUNT = "someone@example.com"


@pytest.fixture(autouse=True)
def _gog_account(monkeypatch):
    monkeypatch.setenv("GOG_ACCOUNT", TEST_ACCOUNT)
