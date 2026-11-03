"""Microsoft sign-in for one personal account.

OAuth 2.0 device authorization grant + refresh tokens against the Microsoft identity
platform v2 endpoints (https://learn.microsoft.com/entra/identity-platform/v2-oauth2-device-code),
requesting exactly the delegated scopes Tasks.ReadWrite, User.Read and offline_access.
(MSAL Python always adds "openid" and can't be told not to, which is why the protocol is
spoken directly here; it is three small requests.)

- The token cache is /data/token.json (0600, atomic writes). Every refresh returns a new
  refresh token, which is saved before the new access token is used.
- The access token is renewed five minutes before it expires, and a forced refresh runs
  every `keepalive_hours` so the refresh token's 90-day inactivity window keeps rolling
  and a revoked sign-in is noticed (and alerted) without waiting for someone to use it.
- After the first sign-in the server is locked to that Microsoft account; signing in as
  anyone else is refused until `todo-mcp-admin reset`.
- Tokens, device codes and user codes are never logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from . import securefile
from .config import Config
from .notify import Notifier

log = logging.getLogger("todo_mcp.auth")

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
REFRESH_MARGIN_SECONDS = 300
CHECK_EVERY_SECONDS = 900
REAUTH_RETRY_SECONDS = 24 * 3600

# Token-endpoint errors that mean "a person has to sign in again".
REAUTH_ERRORS = {
    "invalid_grant",
    "interaction_required",
    "consent_required",
    "login_required",
    "invalid_client",
    "unauthorized_client",
    "invalid_scope",
}

SIGN_IN_HINT = (
    "To fix it, call the start_microsoft_sign_in tool and follow its instructions "
    "(or run `docker exec todo-mcp todo-mcp-admin login` on the server), then enter the "
    "code at https://microsoft.com/devicelogin."
)


class AuthError(Exception):
    """A sign-in problem, with a message meant for the user."""


class SignInRequired(AuthError):
    pass


class ReauthRequired(AuthError):
    pass


class AuthUnavailable(AuthError):
    """Microsoft's sign-in service couldn't be reached; nothing for the user to do yet."""


class AccountMismatch(AuthError):
    pass


def _aadsts(body: dict) -> str:
    match = re.search(r"AADSTS\d+", str(body.get("error_description", "")))
    return match.group(0) if match else ""


def _error_label(body: dict, status: int) -> str:
    parts = [body.get("error") or f"HTTP {status}", _aadsts(body)]
    return " ".join(p for p in parts if p)


def _scope_names(scope: str) -> set[str]:
    return {s.rsplit("/", 1)[-1].lower() for s in scope.split()}


@dataclass
class _Pending:
    device_code: str = field(repr=False)
    user_code: str = field(repr=False)
    verification_uri: str
    expires_at: float
    interval: float
    task: asyncio.Task | None = None
    outcome: str | None = None


class AuthManager:
    def __init__(
        self,
        config: Config,
        http: httpx.AsyncClient,
        notifier: Notifier,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ):
        self.config = config
        self.http = http
        self.notifier = notifier
        self.clock = clock
        self.sleep = sleep
        self.token_path = os.path.join(config.data_dir, "token.json")
        self.token_url = f"{config.authority}/oauth2/v2.0/token"
        self.devicecode_url = f"{config.authority}/oauth2/v2.0/devicecode"
        self._lock = asyncio.Lock()
        self._pending: _Pending | None = None
        self._last_outcome: str | None = None
        self._data: dict | None = securefile.read_json(self.token_path)

    # ------------------------------------------------------------------ state

    def _usable(self) -> bool:
        d = self._data
        return bool(
            d
            and d.get("refresh_token")
            and d.get("client_id") == self.config.client_id
            and d.get("authority") == self.config.authority
        )

    @property
    def state(self) -> str:
        if not self.config.client_id:
            return "not_configured"
        if not self._usable():
            return "signed_out"
        if self._data.get("needs_reauth"):
            return "needs_reauth"
        return "signed_in"

    @property
    def account(self) -> dict | None:
        return (self._data or {}).get("account")

    def _local(self, ts: float | None) -> str | None:
        if not ts:
            return None
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(self.config.tz).isoformat(timespec="seconds")

    def _pending_info(self) -> dict | None:
        p = self._pending
        if not p or p.outcome or p.expires_at <= self.clock():
            return None
        return {
            "verification_uri": p.verification_uri,
            "user_code": p.user_code,
            "expires_at": self._local(p.expires_at),
            "expires_in_seconds": int(p.expires_at - self.clock()),
        }

    def status(self) -> dict:
        d = self._data or {}
        acct = d.get("account") or {}
        info: dict[str, Any] = {
            "state": self.state,
            "account": acct.get("username"),
            "account_name": acct.get("name"),
            "authority": self.config.authority,
            "scopes": list(self.config.scopes),
            "signed_in_at": self._local(d.get("signed_in_at")),
            "last_token_refresh": self._local(d.get("last_refresh_at")),
            "access_token_expires": self._local(d.get("expires_at")) if self._usable() else None,
        }
        if d.get("needs_reauth") and self._usable():
            info["problem"] = d["needs_reauth"].get("reason")
            info["problem_since"] = self._local(d["needs_reauth"].get("since"))
        pending = self._pending_info()
        if pending:
            info["sign_in_pending"] = pending
        if self._last_outcome:
            info["last_sign_in_attempt"] = self._last_outcome
        if self.state != "signed_in":
            info["how_to_fix"] = SIGN_IN_HINT
        return info

    def _save(self) -> None:
        if self._data is None:
            securefile.remove(self.token_path)
        else:
            securefile.write_private_json(self.token_path, self._data)

    # ------------------------------------------------------------ access token

    async def access_token(self) -> str:
        state = self.state
        if state == "not_configured":
            raise SignInRequired("The server has no Microsoft client ID configured (TODO_MCP_CLIENT_ID).")
        if state == "signed_out":
            raise SignInRequired("This server isn't signed in to Microsoft yet. " + SIGN_IN_HINT)
        if state == "needs_reauth":
            reason = self._data["needs_reauth"].get("reason", "renewal failed")
            raise ReauthRequired(
                f"Microsoft sign-in has expired or was revoked ({reason}), so To Do can't be reached. "
                + SIGN_IN_HINT
            )
        d = self._data
        if d.get("access_token") and float(d.get("expires_at", 0)) - self.clock() > REFRESH_MARGIN_SECONDS:
            return d["access_token"]
        return await self.refresh()

    async def refresh(self, force: bool = False) -> str:
        async with self._lock:
            if not self._usable():
                raise SignInRequired("This server isn't signed in to Microsoft yet. " + SIGN_IN_HINT)
            d = self._data
            if (
                not force
                and not d.get("needs_reauth")
                and d.get("access_token")
                and float(d.get("expires_at", 0)) - self.clock() > REFRESH_MARGIN_SECONDS
            ):
                return d["access_token"]  # another caller refreshed while we waited
            d["last_attempt_at"] = self.clock()
            try:
                resp = await self.http.post(
                    self.token_url,
                    data={
                        "client_id": self.config.client_id,
                        "grant_type": "refresh_token",
                        "refresh_token": d["refresh_token"],
                        "scope": " ".join(self.config.scopes),
                    },
                    timeout=30.0,
                )
            except httpx.HTTPError as exc:
                log.warning("token refresh: sign-in service unreachable (%s)", type(exc).__name__)
                raise AuthUnavailable(
                    "Microsoft's sign-in service couldn't be reached. Try again in a minute."
                ) from exc
            try:
                body = resp.json()
            except ValueError:
                body = {}
            if resp.status_code == 200 and body.get("access_token"):
                was_broken = bool(d.get("needs_reauth"))
                self._apply_tokens(body)
                self._save()
                log.info("token refreshed (expires %s)", self._local(self._data["expires_at"]))
                if was_broken:
                    await self.notifier.restored((self.account or {}).get("username"))
                return self._data["access_token"]
            label = _error_label(body, resp.status_code)
            if resp.status_code >= 500 or body.get("error") == "temporarily_unavailable":
                self._save()
                log.warning("token refresh: transient failure (%s)", label)
                raise AuthUnavailable("Microsoft's sign-in service is having trouble. Try again in a minute.")
            # Anything else from the token endpoint means the refresh token is no good.
            d["needs_reauth"] = {"reason": label, "since": (d.get("needs_reauth") or {}).get("since") or self.clock()}
            self._save()
            log.error("token refresh failed, re-authentication required (%s)", label)
        # Outside the lock: the alert is a network call (deduplicated by the notifier).
        await self.notifier.reauth_needed(label)
        raise ReauthRequired(
            f"Microsoft sign-in has expired or was revoked ({label}), so To Do can't be reached. " + SIGN_IN_HINT
        )

    def _apply_tokens(self, body: dict) -> None:
        d = self._data
        now = self.clock()
        d["access_token"] = body["access_token"]
        d["expires_at"] = now + int(body.get("expires_in", 3600))
        if body.get("refresh_token"):
            d["refresh_token"] = body["refresh_token"]
        if body.get("scope"):
            d["scope"] = body["scope"]
        d["last_refresh_at"] = now
        d.pop("needs_reauth", None)

    # ---------------------------------------------------------- device sign-in

    async def start_sign_in(self) -> dict:
        if not self.config.client_id:
            raise SignInRequired("The server has no Microsoft client ID configured (TODO_MCP_CLIENT_ID).")
        existing = self._pending_info()
        if existing and existing["expires_in_seconds"] > 60:
            return {**existing, "already_pending": True, "instructions": self._instructions(existing)}
        try:
            resp = await self.http.post(
                self.devicecode_url,
                data={"client_id": self.config.client_id, "scope": " ".join(self.config.scopes)},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise AuthUnavailable("Microsoft's sign-in service couldn't be reached. Try again in a minute.") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or not body.get("device_code"):
            label = _error_label(body, resp.status_code)
            raise AuthError(
                f"Microsoft wouldn't start a sign-in ({label}). Check the Entra app registration: "
                "supported account types must include personal Microsoft accounts and "
                "'Allow public client flows' must be Yes."
            )
        now = self.clock()
        if self._pending and self._pending.task and not self._pending.task.done():
            self._pending.task.cancel()
        pending = _Pending(
            device_code=body["device_code"],
            user_code=body["user_code"],
            verification_uri=body.get("verification_uri") or "https://microsoft.com/devicelogin",
            expires_at=now + int(body.get("expires_in", 900)),
            interval=float(body.get("interval", 5)),
        )
        self._pending = pending
        self._last_outcome = None
        pending.task = asyncio.create_task(self._poll(pending))
        log.info("device-code sign-in started (expires in %ss)", int(pending.expires_at - now))
        info = self._pending_info()
        return {**info, "already_pending": False, "instructions": self._instructions(info)}

    @staticmethod
    def _instructions(info: dict) -> str:
        return (
            f"Open {info['verification_uri']} and enter the code {info['user_code']} "
            f"(valid until {info['expires_at']}). Sign in with the personal Microsoft account "
            "that holds your To Do and accept the permissions (read and write tasks, read your "
            "profile, keep access). The server finishes on its own; check auth_status afterwards."
        )

    async def _poll(self, p: _Pending) -> None:
        interval = p.interval
        try:
            while self.clock() < p.expires_at:
                await self.sleep(interval)
                try:
                    resp = await self.http.post(
                        self.token_url,
                        data={"grant_type": DEVICE_GRANT, "client_id": self.config.client_id,
                              "device_code": p.device_code},
                        timeout=30.0,
                    )
                except httpx.HTTPError:
                    continue
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                if resp.status_code == 200 and body.get("access_token"):
                    try:
                        account = await self._complete_sign_in(body)
                        p.outcome = f"signed in as {account}"
                    except AuthError as exc:
                        p.outcome = f"failed: {exc}"
                    self._last_outcome = p.outcome
                    log.info("device-code sign-in %s", "succeeded" if p.outcome.startswith("signed") else "rejected")
                    return
                err = body.get("error")
                if err == "authorization_pending":
                    continue
                if err == "slow_down":
                    interval += 5
                    continue
                reasons = {
                    "authorization_declined": "the sign-in was declined",
                    "expired_token": "the code expired before sign-in finished",
                    "bad_verification_code": "Microsoft didn't recognise the code",
                }
                p.outcome = f"failed: {reasons.get(err, _error_label(body, resp.status_code))}"
                self._last_outcome = p.outcome
                log.warning("device-code sign-in %s", p.outcome)
                return
            p.outcome = "failed: the code expired before sign-in finished"
            self._last_outcome = p.outcome
        except asyncio.CancelledError:
            p.outcome = p.outcome or "cancelled"
            raise

    async def _complete_sign_in(self, body: dict) -> str:
        granted = _scope_names(body.get("scope", ""))
        if "tasks.readwrite" not in granted:
            raise AuthError("the consent didn't include Tasks.ReadWrite")
        if not body.get("refresh_token"):
            raise AuthError("Microsoft didn't return a refresh token (offline_access missing)")
        try:
            me = await self.http.get(
                f"{self.config.graph_base}/me",
                params={"$select": "id,displayName,userPrincipalName,mail"},
                headers={"Authorization": f"Bearer {body['access_token']}"},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise AuthError("couldn't read the signed-in account from Microsoft Graph") from exc
        if me.status_code != 200:
            raise AuthError(f"couldn't read the signed-in account from Microsoft Graph (HTTP {me.status_code})")
        prof = me.json()
        account = {
            "id": prof.get("id"),
            "username": prof.get("userPrincipalName") or prof.get("mail"),
            "name": prof.get("displayName"),
        }
        async with self._lock:
            locked = (self._data or {}).get("account")
            if locked and locked.get("id") and locked["id"] != account["id"]:
                raise AccountMismatch(
                    f"that was {account['username']}, but this server is linked to {locked.get('username')}. "
                    "Sign in with that account, or run `todo-mcp-admin reset --yes` first to link a different one."
                )
            now = self.clock()
            self._data = {
                "version": 1,
                "client_id": self.config.client_id,
                "authority": self.config.authority,
                "account": account,
                "signed_in_at": now,
            }
            self._apply_tokens(body)
            self._save()
        await self.notifier.restored(account["username"])
        return account["username"] or "(unknown account)"

    async def reset(self) -> None:
        async with self._lock:
            if self._pending and self._pending.task and not self._pending.task.done():
                self._pending.task.cancel()
            self._pending = None
            self._data = None
            self._save()
        log.info("token cache cleared; account link removed")

    # --------------------------------------------------------------- keepalive

    async def keepalive_once(self) -> None:
        if not self._usable():
            return
        d = self._data
        now = self.clock()
        if d.get("needs_reauth"):
            if now - float(d.get("last_attempt_at", 0)) >= REAUTH_RETRY_SECONDS:
                log.info("retrying token refresh for a broken sign-in")
                await self.refresh(force=True)
            return
        if now - float(d.get("last_refresh_at", 0)) >= self.config.keepalive_hours * 3600:
            await self.refresh(force=True)

    async def keepalive_loop(self, every: float = CHECK_EVERY_SECONDS) -> None:
        while True:
            try:
                await self.keepalive_once()
            except (ReauthRequired, SignInRequired):
                pass
            except AuthUnavailable:
                log.info("keepalive: sign-in service unavailable, will retry")
            except Exception:  # pragma: no cover - defensive
                log.exception("keepalive failed")
            await self.sleep(every)
