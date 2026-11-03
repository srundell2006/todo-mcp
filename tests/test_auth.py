from __future__ import annotations

import json
import logging
import os
import stat
from urllib.parse import parse_qs

import pytest
from conftest import sign_in

from todo_mcp.auth import AccountMismatch, AuthManager, AuthUnavailable, ReauthRequired, SignInRequired
from todo_mcp.notify import Notifier


def _form(req) -> dict:
    return {k: v[0] for k, v in parse_qs(req.content.decode()).items()}


async def test_not_signed_in_gives_clear_error(stack):
    assert stack.auth.state == "signed_out"
    with pytest.raises(SignInRequired, match="start_microsoft_sign_in"):
        await stack.auth.access_token()


async def test_device_code_sign_in_requests_exact_scopes_and_stores_privately(stack, fake, config, caplog):
    caplog.set_level(logging.DEBUG)
    await sign_in(stack)
    device_req = next(r for r in fake.requests if r.url.path.endswith("/devicecode"))
    assert str(device_req.url).startswith("https://login.microsoftonline.com/consumers/")
    assert _form(device_req)["scope"] == "Tasks.ReadWrite User.Read offline_access"
    path = os.path.join(config.data_dir, "token.json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.load(open(path))
    assert data["account"] == {"id": "acct-1", "username": "stephen@example.com", "name": "Stephen"}
    assert data["refresh_token"] in fake.refresh_tokens
    status = stack.auth.status()
    assert status["state"] == "signed_in" and status["account"] == "stephen@example.com"
    dumped = json.dumps(status)
    assert data["access_token"] not in dumped and data["refresh_token"] not in dumped
    # No token, device code or user code in the logs.
    logged = caplog.text
    for secret in [data["refresh_token"], data["access_token"], "DEVICE-SECRET", "ABCD-EFGH"]:
        assert secret not in logged


async def test_pending_sign_in_is_reused(stack, fake):
    fake.device_polls_before_success = 10_000
    first = await stack.auth.start_sign_in()
    second = await stack.auth.start_sign_in()
    assert second["already_pending"] and second["user_code"] == first["user_code"]
    assert sum(r.url.path.endswith("/devicecode") for r in fake.requests) == 1
    stack.auth._pending.task.cancel()


async def test_declined_sign_in_is_reported(stack, fake):
    fake.device_error = "authorization_declined"
    await stack.auth.start_sign_in()
    await stack.auth._pending.task
    assert stack.auth.state == "signed_out"
    assert "declined" in stack.auth.status()["last_sign_in_attempt"]


async def test_refresh_rotates_and_persists_new_refresh_token(stack, fake, clock, config):
    await sign_in(stack)
    old_rt = stack.auth._data["refresh_token"]
    clock.t += 3600  # access token now expired
    token = await stack.auth.access_token()
    assert token in fake.access_tokens
    data = json.load(open(os.path.join(config.data_dir, "token.json")))
    assert data["refresh_token"] != old_rt and data["refresh_token"] in fake.refresh_tokens
    refresh_req = [r for r in fake.requests if _form(r).get("grant_type") == "refresh_token"][-1]
    assert _form(refresh_req)["scope"] == "Tasks.ReadWrite User.Read offline_access"


async def test_restart_keeps_sign_in(stack, fake, config, http, clock):
    await sign_in(stack)
    # A new process reading the same volume: no sign-in needed, refresh works.
    again = AuthManager(config, http, Notifier(config, http, clock=clock), clock=clock)
    assert again.state == "signed_in"
    clock.t += 7200
    assert await again.access_token() in fake.access_tokens


async def test_invalid_grant_needs_reauth_alerts_once_then_restores(stack, fake, clock, config):
    await sign_in(stack)
    fake.refresh_error = (400, {"error": "invalid_grant", "error_description": "AADSTS700082: expired"})
    clock.t += 3600
    with pytest.raises(ReauthRequired, match=r"invalid_grant AADSTS700082.*start_microsoft_sign_in"):
        await stack.auth.access_token()
    assert stack.auth.state == "needs_reauth"
    with pytest.raises(ReauthRequired):
        await stack.auth.access_token()  # no network call, same clear error
    with pytest.raises(ReauthRequired):
        await stack.auth.refresh(force=True)  # second failure within 24 h: no second alert
    alerts = [json.loads(r.content) for r in fake.requests if str(r.url).startswith("http://ntfy.test")]
    assert len(alerts) == 1
    assert alerts[0]["topic"] == "homelab-warning" and "sign-in needed" in alerts[0]["title"]
    ntfy_req = next(r for r in fake.requests if str(r.url).startswith("http://ntfy.test"))
    assert ntfy_req.headers["Authorization"].startswith("Basic ")
    assert "rt-" not in alerts[0]["message"] and "at-" not in alerts[0]["message"]

    # After a day, still broken: one reminder.
    clock.t += 25 * 3600
    with pytest.raises(ReauthRequired):
        await stack.auth.refresh(force=True)
    assert sum(str(r.url).startswith("http://ntfy.test") for r in fake.requests) == 2

    # Signing in again fixes it and sends "restored".
    fake.refresh_error = None
    fake._polls = 0
    await sign_in(stack)
    titles = [json.loads(r.content)["title"] for r in fake.requests if str(r.url).startswith("http://ntfy.test")]
    assert titles[-1] == "To Do MCP: Microsoft sign-in restored"


async def test_transient_refresh_failure_is_not_reauth(stack, fake, clock):
    await sign_in(stack)
    fake.refresh_error = (503, {"error": "temporarily_unavailable"})
    clock.t += 3600
    with pytest.raises(AuthUnavailable):
        await stack.auth.access_token()
    assert stack.auth.state == "signed_in"
    assert not any(str(r.url).startswith("http://ntfy.test") for r in fake.requests)


async def test_other_account_is_refused(stack, fake):
    await sign_in(stack)
    fake.account_id, fake.username = "acct-2", "someone@else.com"
    fake._polls = 0
    await stack.auth.start_sign_in()
    await stack.auth._pending.task
    assert "linked to stephen@example.com" in stack.auth.status()["last_sign_in_attempt"]
    assert stack.auth.account["id"] == "acct-1"
    with pytest.raises(AccountMismatch):
        await stack.auth._complete_sign_in(fake.issue_tokens())


async def test_reset_forgets_everything(stack, config):
    await sign_in(stack)
    await stack.auth.reset()
    assert stack.auth.state == "signed_out"
    assert not os.path.exists(os.path.join(config.data_dir, "token.json"))


async def test_keepalive_forces_refresh_after_interval(stack, fake, clock):
    await sign_in(stack)
    before = stack.auth._data["refresh_token"]
    clock.t += 11 * 3600
    await stack.auth.keepalive_once()
    assert stack.auth._data["refresh_token"] == before
    clock.t += 2 * 3600
    await stack.auth.keepalive_once()
    assert stack.auth._data["refresh_token"] != before


async def test_ntfy_password_is_private_and_hash_verifies(config, http):
    import bcrypt

    n = Notifier(config, http)
    pw = n.ensure_password()
    path = os.path.join(config.data_dir, "ntfy_password")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert bcrypt.checkpw(pw.encode(), n.password_hash().encode())
    assert n.ensure_password() == pw
