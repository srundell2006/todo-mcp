"""Runtime configuration, read from environment variables.

Nothing in here is a secret. The Microsoft client ID identifies a public client (no
secret exists for it); the ntfy password is generated inside the container and kept
in the data volume, never in the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

# Delegated Microsoft Graph permissions, exactly as registered on the Entra app.
# offline_access is what makes Microsoft return a refresh token.
SCOPES: tuple[str, ...] = ("Tasks.ReadWrite", "User.Read", "offline_access")

DEFAULT_AUTHORITY = "https://login.microsoftonline.com/consumers"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _float(env: dict[str, str], key: str, default: float) -> float:
    raw = env.get(key, "").strip()
    return float(raw) if raw else default


def _int(env: dict[str, str], key: str, default: int) -> int:
    raw = env.get(key, "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    client_id: str
    authority: str = DEFAULT_AUTHORITY
    timezone: str = "America/Chicago"
    data_dir: str = "/data"
    host: str = "0.0.0.0"
    port: int = 3000
    admin_port: int = 3001
    keepalive_hours: float = 12.0
    ntfy_url: str = ""
    ntfy_topic: str = ""
    ntfy_user: str = ""
    graph_base: str = GRAPH_BASE
    log_level: str = "INFO"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def scopes(self) -> tuple[str, ...]:
        return SCOPES

    @property
    def ntfy_enabled(self) -> bool:
        return bool(self.ntfy_url and self.ntfy_topic and self.ntfy_user)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        authority = env.get("TODO_MCP_AUTHORITY", "").strip().rstrip("/") or DEFAULT_AUTHORITY
        if authority.endswith("/organizations"):
            # Personal Microsoft accounts are rejected by /organizations.
            raise ValueError("TODO_MCP_AUTHORITY must be .../consumers or .../common for a personal account")
        timezone = env.get("TODO_MCP_TIMEZONE", "").strip() or "America/Chicago"
        ZoneInfo(timezone)  # fail fast on a typo
        return cls(
            client_id=env.get("TODO_MCP_CLIENT_ID", "").strip(),
            authority=authority,
            timezone=timezone,
            data_dir=env.get("TODO_MCP_DATA_DIR", "").strip() or "/data",
            host=env.get("TODO_MCP_HOST", "").strip() or "0.0.0.0",
            port=_int(env, "TODO_MCP_PORT", 3000),
            admin_port=_int(env, "TODO_MCP_ADMIN_PORT", 3001),
            keepalive_hours=_float(env, "TODO_MCP_KEEPALIVE_HOURS", 12.0),
            ntfy_url=env.get("NTFY_URL", "").strip().rstrip("/"),
            ntfy_topic=env.get("NTFY_TOPIC", "").strip(),
            ntfy_user=env.get("NTFY_USER", "").strip(),
            graph_base=env.get("TODO_MCP_GRAPH_BASE", "").strip().rstrip("/") or GRAPH_BASE,
            log_level=(env.get("LOG_LEVEL", "").strip() or "INFO").upper(),
        )
