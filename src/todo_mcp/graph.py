"""Minimal Microsoft Graph v1.0 client: auth, retries, throttling and paging.

- 429 / 503 / 504: wait for Retry-After (seconds or an HTTP date), otherwise back off
  exponentially with jitter; give up after 6 attempts or ~2 minutes of waiting.
- Network errors: same backoff.
- 401: refresh the access token once and retry.
- Collections: follow @odata.nextLink until done (or until max_items).
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import time
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .auth import AuthManager

log = logging.getLogger("todo_mcp.graph")

RETRY_STATUSES = {429, 503, 504}
MAX_ATTEMPTS = 6
MAX_SINGLE_WAIT = 60.0
MAX_TOTAL_WAIT = 120.0


def seg(value: str) -> str:
    """Quote an ID for use as one URL path segment."""
    return quote(value, safe="=")


class GraphError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(self.user_message())

    def user_message(self) -> str:
        if self.status == 404:
            return f"Not found ({self.message or self.code}). It may have been deleted, or the ID or list is wrong."
        if self.status == 403:
            return f"Microsoft Graph refused the request ({self.code}: {self.message})."
        if self.status == 429:
            return "Microsoft Graph is throttling requests right now. Try again in a minute."
        return f"Microsoft Graph error {self.status} ({self.code}): {self.message}"


def retry_after_seconds(value: str | None, now: float | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - (now if now is not None else time.time()))


class GraphClient:
    def __init__(
        self,
        auth: AuthManager,
        http: httpx.AsyncClient,
        base: str,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self.auth = auth
        self.http = http
        self.base = base.rstrip("/")
        self.sleep = sleep

    def _url(self, path: str) -> str:
        return path if path.startswith("https://") else f"{self.base}{path}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
    ) -> dict | None:
        url = self._url(path)
        refreshed = False
        waited = 0.0
        attempt = 0
        while True:
            attempt += 1
            token = await self.auth.access_token()
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            try:
                resp = await self.http.request(method, url, params=params, json=json, headers=headers, timeout=60.0)
            except httpx.TransportError as exc:
                if attempt >= MAX_ATTEMPTS or waited >= MAX_TOTAL_WAIT:
                    raise GraphError(0, type(exc).__name__, "couldn't reach Microsoft Graph") from exc
                delay = min(MAX_SINGLE_WAIT, 2 ** (attempt - 1)) + random.uniform(0, 0.5)
                log.info("graph %s: network error, retry %d in %.1fs", method, attempt, delay)
                waited += delay
                await self.sleep(delay)
                continue

            if resp.status_code == 401 and not refreshed:
                refreshed = True
                await self.auth.refresh(force=True)
                continue

            if resp.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS and waited < MAX_TOTAL_WAIT:
                hinted = retry_after_seconds(resp.headers.get("Retry-After"))
                delay = hinted if hinted is not None else 2 ** (attempt - 1) + random.uniform(0, 0.5)
                delay = min(delay, MAX_SINGLE_WAIT, MAX_TOTAL_WAIT - waited)
                log.info("graph %s: HTTP %d, retry %d in %.1fs", method, resp.status_code, attempt, delay)
                waited += delay
                await self.sleep(delay)
                continue

            if resp.status_code == 204 or not resp.content:
                if resp.is_success:
                    return None
            if resp.is_success:
                return resp.json()
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            raise GraphError(resp.status_code, err.get("code", "error"), err.get("message", resp.reason_phrase))

    async def get(self, path: str, params: dict[str, str] | None = None) -> dict:
        return await self.request("GET", path, params=params) or {}

    async def get_all(self, path: str, params: dict[str, str] | None = None, max_items: int | None = None) -> list[dict]:
        items: list[dict] = []
        page = await self.get(path, params)
        while True:
            items.extend(page.get("value", []))
            if max_items is not None and len(items) >= max_items:
                return items[:max_items]
            next_link = page.get("@odata.nextLink")
            if not next_link:
                return items
            page = await self.get(next_link)  # nextLink already carries the query
