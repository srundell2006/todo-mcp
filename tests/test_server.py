from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
import uvicorn
from mcp import Client

from todo_mcp.server import App, build_http_app

DESTRUCTIVE = {"delete_task_list", "delete_task", "remove_checklist_item"}


@pytest.fixture
def app(config, fake):
    return App(config, http=httpx.AsyncClient(transport=fake.transport()))


async def test_tool_surface_and_destructive_marking(app):
    async with Client(app.mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert len(tools) == 17
    for name, tool in tools.items():
        if name in DESTRUCTIVE:
            assert tool.annotations.destructive_hint is True
            assert tool.description.startswith("DESTRUCTIVE:")
            assert "confirm" in tool.description.lower()
        else:
            assert tool.annotations.destructive_hint in (False, None)
            assert not tool.description.startswith("DESTRUCTIVE")
    for name in ("list_task_lists", "list_tasks", "search_tasks", "get_task", "auth_status"):
        assert tools[name].annotations.read_only_hint is True


async def test_not_signed_in_tool_error_is_clear(app):
    async with Client(app.mcp) as client:
        result = await client.call_tool("list_task_lists", {})
    assert result.is_error
    assert "start_microsoft_sign_in" in result.content[0].text


async def test_sign_in_tool_returns_link_and_code(app, fake):
    fake.device_polls_before_success = 10_000
    async with Client(app.mcp) as client:
        result = await client.call_tool("start_microsoft_sign_in", {})
    assert not result.is_error
    assert "ABCD-EFGH" in result.content[0].text and "microsoft.com/devicelogin" in result.content[0].text
    app.auth._pending.task.cancel()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_streamable_http_end_to_end(app, mode):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(build_http_app(app), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with httpx.AsyncClient() as h:
            health = await h.get(f"http://127.0.0.1:{port}/healthz")
            assert health.json()["auth"] == "signed_out"
        # A Host/Origin like the real connector's must not be rejected.
        async with Client(f"http://127.0.0.1:{port}/mcp", mode=mode) as client:
            tools = (await client.list_tools()).tools
            assert len(tools) == 17
            status = await client.call_tool("auth_status", {})
            assert not status.is_error and "signed_out" in status.content[0].text
    finally:
        server.should_exit = True
        await task
