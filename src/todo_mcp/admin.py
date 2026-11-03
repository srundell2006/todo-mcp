"""todo-mcp-admin: operator commands, run inside the container with `docker exec`.

    todo-mcp-admin status          sign-in state (no tokens shown)
    todo-mcp-admin login [--wait]  start the Microsoft device-code sign-in; prints the link and code
    todo-mcp-admin reset --yes     forget the token cache and the linked account
    todo-mcp-admin ntfy-hash       bcrypt hash of the ntfy password, for `ntfy user add`
    todo-mcp-admin notify-test     send a test alert to the ntfy topic
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

from .config import Config
from .notify import Notifier


def _admin_url(config: Config, path: str) -> str:
    return f"http://127.0.0.1:{config.admin_port}{path}"


def _print(obj: dict) -> None:
    print(json.dumps(obj, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="todo-mcp-admin")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    login = sub.add_parser("login")
    login.add_argument("--wait", action="store_true", help="wait until sign-in finishes")
    reset = sub.add_parser("reset")
    reset.add_argument("--yes", action="store_true")
    sub.add_parser("ntfy-hash")
    sub.add_parser("notify-test")
    args = parser.parse_args(argv)
    config = Config.from_env()

    if args.cmd == "ntfy-hash":
        print(Notifier(config, httpx.AsyncClient()).password_hash())
        return 0

    with httpx.Client(timeout=40.0) as client:
        if args.cmd == "status":
            _print(client.get(_admin_url(config, "/status")).json())
            return 0
        if args.cmd == "notify-test":
            _print(client.post(_admin_url(config, "/notify-test")).json())
            return 0
        if args.cmd == "reset":
            if not args.yes:
                print("This forgets the Microsoft sign-in and the linked account. Re-run with --yes.")
                return 2
            _print(client.post(_admin_url(config, "/reset"), json={"confirm": True}).json())
            return 0
        if args.cmd == "login":
            resp = client.post(_admin_url(config, "/login"))
            body = resp.json()
            if resp.status_code != 200:
                print(body.get("error", body), file=sys.stderr)
                return 1
            print(body["instructions"])
            if not args.wait:
                return 0
            deadline = time.time() + body.get("expires_in_seconds", 900) + 10
            while time.time() < deadline:
                time.sleep(5)
                status = client.get(_admin_url(config, "/status")).json()
                if "sign_in_pending" not in status:
                    print(status.get("last_sign_in_attempt") or status.get("state"))
                    return 0 if status.get("state") == "signed_in" else 1
            print("timed out waiting for sign-in", file=sys.stderr)
            return 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
