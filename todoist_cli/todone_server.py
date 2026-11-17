#!/usr/bin/env python3
"""Line-delimited JSON server the Todone Mac app talks to over stdin/stdout.

Each request is one line: {"id": 1, "method": "snapshot", "params": {...}}.
Each response is one line: {"id": 1, "result": ...} or {"id": 1, "error": "..."}.
Only the methods in METHODS can be called. Diagnostics go to stderr.

    python -m todoist_cli.todone_server            # real Todoist and Google Calendar
    python -m todoist_cli.todone_server --fake     # in-memory sample data; nothing leaves the machine
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

from . import cli, planner, tracker
from .client import TodoistAPIError
from .todone_core import Services, Todone, TodoneError

METHODS = {
    "refresh", "snapshot", "set_capacity", "move", "propose_plan", "cancel_plan", "confirm_plan",
    "complete", "toggle_fixed", "start", "start_log", "stop", "discard_timer", "details", "update_details",
    "add_comment", "edit_comment", "colors", "quick_update",
}


def handle(app: Todone, request: dict) -> dict:
    """Run one request and build its response; never raises."""
    rid = request.get("id")
    method = request.get("method")
    if method not in METHODS:
        return {"id": rid, "error": f"unknown method {method!r}"}
    try:
        result = getattr(app, method)(**(request.get("params") or {}))
    except (TodoneError, TodoistAPIError, ValueError, TypeError) as exc:
        return {"id": rid, "error": str(exc)}
    except Exception as exc:  # keep serving; the app shows the message
        traceback.print_exc(file=sys.stderr)
        return {"id": rid, "error": f"internal error: {exc}"}
    return {"id": rid, "result": result}


def serve(app: Todone, stdin=sys.stdin, stdout=sys.stdout) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {"id": None, "error": f"bad request: {exc}"}
        else:
            response = handle(app, request)
        stdout.write(json.dumps(response) + "\n")
        stdout.flush()


def real_services(config_path: Path) -> Services:
    config = planner.load_config(config_path)
    account = cli._resolve_account(config.account)

    def fetch_busy(start, end):
        return tracker.fetch_busy(account, config.calendars, start, end)

    def write_event(title, start, end, description=None, color=None):
        return tracker.create_calendar_event(title, start, end, description=description,
                                             account=account, event_color=color)

    return Services(
        client=cli._get_client(),
        config=config,
        fetch_busy=fetch_busy,
        write_event=write_event,
        now=lambda: datetime.now().astimezone(),
        calendar_help=lambda message: tracker.gog_error_help(message, account),
    )


def fake_services(config_path: Path) -> Services:
    from . import todone_fake

    config = planner.load_config(config_path)
    today = datetime.now(config.timezone).date()
    return Services(
        client=todone_fake.sample_data(today),
        config=config,
        fetch_busy=todone_fake.fake_busy(config.timezone),
        write_event=todone_fake.fake_write_event,
        now=lambda: datetime.now().astimezone(),
        write_plan_log=lambda *a, **k: print("[fake] plan log not written", file=sys.stderr),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fake", action="store_true", help="use in-memory sample data")
    parser.add_argument("--config", default=str(cli._PLAN_CONFIG_FILE))
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    services = fake_services(config_path) if args.fake else real_services(config_path)
    serve(Todone(services))


if __name__ == "__main__":
    main()
