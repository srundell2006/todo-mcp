"""ntfy alerts for "Microsoft sign-in needs attention".

The ntfy password is generated here on first start and kept only in the data volume
(0600). ntfy is given its bcrypt hash (`todo-mcp-admin ntfy-hash`), so the password
itself never leaves the container. Alerts never contain tokens or codes.

One alert per incident, repeated at most once a day while the problem lasts, and a
single "restored" message when sign-in works again. The state survives restarts.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Callable

import bcrypt
import httpx

from . import securefile
from .config import Config

log = logging.getLogger("todo_mcp.notify")

REPEAT_SECONDS = 24 * 3600


class Notifier:
    def __init__(self, config: Config, http: httpx.AsyncClient, clock: Callable[[], float] = time.time):
        self.config = config
        self.http = http
        self.clock = clock
        self.password_path = os.path.join(config.data_dir, "ntfy_password")
        self.state_path = os.path.join(config.data_dir, "alert_state.json")

    @property
    def enabled(self) -> bool:
        return self.config.ntfy_enabled

    def ensure_password(self) -> str:
        try:
            with open(self.password_path, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
            if value:
                return value
        except FileNotFoundError:
            pass
        value = secrets.token_urlsafe(32)
        securefile.write_private(self.password_path, value.encode("utf-8"))
        return value

    def password_hash(self) -> str:
        return bcrypt.hashpw(self.ensure_password().encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("ascii")

    def _state(self) -> dict:
        return securefile.read_json(self.state_path) or {}

    def _save_state(self, state: dict) -> None:
        securefile.write_private_json(self.state_path, state)

    async def _publish(self, title: str, message: str, priority: int, tags: list[str]) -> bool:
        if not self.enabled:
            log.info("ntfy not configured; alert not sent: %s", title)
            return False
        payload = {
            "topic": self.config.ntfy_topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags,
        }
        try:
            resp = await self.http.post(
                self.config.ntfy_url,
                json=payload,
                auth=(self.config.ntfy_user, self.ensure_password()),
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            log.warning("ntfy publish failed: %s", type(exc).__name__)
            return False
        if resp.status_code >= 300:
            log.warning("ntfy publish failed: HTTP %s", resp.status_code)
            return False
        log.info("ntfy alert sent to topic %s: %s", self.config.ntfy_topic, title)
        return True

    async def reauth_needed(self, reason: str) -> None:
        state = self._state()
        now = self.clock()
        if state.get("open") and now - float(state.get("last_sent", 0)) < REPEAT_SECONDS:
            return
        sent = await self._publish(
            "To Do MCP: Microsoft sign-in needed",
            "The To Do connector can't renew its Microsoft sign-in "
            f"({reason}). It won't work until you sign in again: ask Claude to run "
            "start_microsoft_sign_in, or run `docker exec todo-mcp todo-mcp-admin login` on "
            "Ubuntugutility, then finish at https://microsoft.com/devicelogin.",
            priority=4,
            tags=["warning", "key"],
        )
        state.update(open=True, reason=reason, since=state.get("since") or now)
        if sent:
            state["last_sent"] = now
        self._save_state(state)

    async def restored(self, account: str | None) -> None:
        state = self._state()
        if not state.get("open"):
            return
        await self._publish(
            "To Do MCP: Microsoft sign-in restored",
            f"Signed in again{f' as {account}' if account else ''}. The To Do connector is working.",
            priority=3,
            tags=["white_check_mark"],
        )
        self._save_state({"open": False, "last_closed": self.clock()})

    async def test(self) -> bool:
        return await self._publish(
            "To Do MCP: test alert",
            "Test message from the todo-mcp server. Sign-in alerts will arrive on this topic.",
            priority=3,
            tags=["test_tube"],
        )
