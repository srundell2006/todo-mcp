# todo-mcp

A small, self-hosted **remote MCP server for Microsoft To Do**, for use as a custom connector
in Claude.ai. It talks to Microsoft Graph v1.0 (`/me/todo`) on behalf of **one personal
Microsoft account**, using delegated permissions only.

- Streamable HTTP at `/mcp` (stateless, JSON responses), meant to sit behind a reverse proxy
  that does the client authentication (here: Traefik + Authentik + `mcp-auth-gate`).
- One-time **device code** sign-in; the refresh token is stored in a Docker volume and renewed
  automatically.
- Time zone aware (default `America/Chicago`), handles Graph paging and throttling.

## Tools

| Area | Tools |
|---|---|
| Sign-in | `auth_status`, `start_microsoft_sign_in` |
| Lists | `list_task_lists`, `create_task_list`, `rename_task_list`, `delete_task_list` ⚠️ |
| Tasks | `list_tasks` (filters: list, status, due on/from/to, overdue, no due date, importance, category), `search_tasks` (all lists: title, notes, checklist, categories), `get_task`, `create_task`, `update_task`, `complete_task`, `reopen_task`, `delete_task` ⚠️ |
| Checklist | `add_checklist_item`, `update_checklist_item`, `remove_checklist_item` ⚠️ |

Tasks support title, notes, due date, reminder, importance, recurrence (daily, weekdays,
weekly, monthly or yearly, by date or "second Tuesday" style, with an end date or count) and
categories.

Repeating tasks behave as To Do series. Completing one stores that occurrence as a separate
completed task and moves the same task ID to the next occurrence. Moving a repeating task's
due date off its pattern can split it off as a one-off task. The tools say when either happens.

⚠️ tools start their description with `DESTRUCTIVE:` and carry
`destructiveHint: true`, so Claude asks before calling them.

## Microsoft auth

- Authority `https://login.microsoftonline.com/consumers` (personal accounts only).
  `/organizations` rejects personal accounts.
- Scopes requested: exactly `Tasks.ReadWrite User.Read offline_access`. The OAuth device
  authorization grant is spoken directly
  ([protocol](https://learn.microsoft.com/entra/identity-platform/v2-oauth2-device-code)),
  because MSAL Python always adds `openid`.
- Token cache: `/data/token.json`, mode 0600 in a 0700 directory, written atomically. Each
  refresh returns a new refresh token, which is saved before it's used.
- The access token is renewed 5 minutes before expiry. A forced refresh every 12 hours keeps
  the 90-day inactivity window rolling and detects revocation early.
- Locked to the first account that signs in. Run `todo-mcp-admin reset --yes` to link a
  different one.
- If renewal fails, every tool returns a clear "sign in again" error and an ntfy alert is sent
  (once, then at most daily, plus a "restored" message).
- Tokens, device codes and user codes are never logged.

### Entra app registration

- Supported account types: *Any Entra ID tenant + personal Microsoft accounts*.
- Platform *Mobile and desktop applications*, redirect URI
  `https://login.microsoftonline.com/common/oauth2/nativeclient`.
- *Allow public client flows*: **Yes**.
- API permissions (Microsoft Graph, delegated): `Tasks.ReadWrite`, `User.Read`, `offline_access`.
- No client secret or certificate.

## Configuration (environment)

| Variable | Default | |
|---|---|---|
| `TODO_MCP_CLIENT_ID` | (required) | Entra application (client) ID |
| `TODO_MCP_AUTHORITY` | `https://login.microsoftonline.com/consumers` | or `.../common` |
| `TODO_MCP_TIMEZONE` | `America/Chicago` | IANA zone for all dates |
| `TODO_MCP_DATA_DIR` | `/data` | token cache, ntfy password, alert state |
| `TODO_MCP_KEEPALIVE_HOURS` | `12` | forced refresh interval |
| `NTFY_URL`, `NTFY_TOPIC`, `NTFY_USER` | unset (alerts off) | ntfy server, topic, publishing user |

## Operating it

Run these inside the container (`docker exec todo-mcp ...`):

```
todo-mcp-admin status          # sign-in state, account, last refresh (no tokens)
todo-mcp-admin login [--wait]  # start device-code sign-in; prints the link and code
todo-mcp-admin reset --yes     # forget the token cache and linked account
todo-mcp-admin refresh         # renew the Microsoft token now
todo-mcp-admin ntfy-hash       # bcrypt hash of the generated ntfy password
todo-mcp-admin notify-test     # send a test alert
```

`login` and `status` talk to an admin endpoint bound to the container's loopback
(`127.0.0.1:3001`), which only `docker exec` can reach.

ntfy setup, run in the ntfy container (the password stays in this server's volume):

```
NTFY_PASSWORD_HASH='<output of todo-mcp-admin ntfy-hash>' ntfy user add todo-mcp
ntfy access todo-mcp homelab-warning write-only
```

## Deploy

`deploy/docker-compose.yml` is the Portainer repository stack (Ubuntugutility, pinned
`172.30.0.98` on the `traefik` network). Set `MS_CLIENT_ID` and `AUTHENTIK_CLIENT_ID` as stack
environment variables. The container runs as uid 10001 with a read-only root filesystem,
`cap_drop: ALL` and `no-new-privileges`.

## Development

```
pip install -e '.[test]'
pytest
```

The tests run against an in-memory fake of the Microsoft identity platform and Graph To Do
(`tests/fake_ms.py`), including paging, 429 / Retry-After, token rotation and re-auth alerts.
