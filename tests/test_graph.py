from __future__ import annotations

import httpx
import pytest
from conftest import sign_in

from todo_mcp.graph import GraphError, retry_after_seconds


async def test_429_waits_for_retry_after(stack, fake):
    await sign_in(stack)
    fake.throttle_next = [
        httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "TooManyRequests"}}),
        httpx.Response(429, headers={"Retry-After": "3"}, json={"error": {"code": "TooManyRequests"}}),
    ]
    stack.sleeper.calls.clear()
    data = await stack.graph.get("/me/todo/lists")
    assert len(data["value"]) == 2
    assert stack.sleeper.calls == [7.0, 3.0]


async def test_503_without_header_backs_off(stack, fake):
    await sign_in(stack)
    fake.throttle_next = [httpx.Response(503), httpx.Response(504)]
    stack.sleeper.calls.clear()
    await stack.graph.get("/me/todo/lists")
    assert len(stack.sleeper.calls) == 2 and 1 <= stack.sleeper.calls[0] < 2 and 2 <= stack.sleeper.calls[1] < 3


async def test_gives_up_after_max_attempts(stack, fake):
    await sign_in(stack)
    fake.throttle_next = [httpx.Response(429, headers={"Retry-After": "1"}) for _ in range(10)]
    with pytest.raises(GraphError, match="throttling"):
        await stack.graph.get("/me/todo/lists")


async def test_long_retry_after_is_capped(stack, fake):
    await sign_in(stack)
    fake.throttle_next = [httpx.Response(429, headers={"Retry-After": "600"})]
    stack.sleeper.calls.clear()
    await stack.graph.get("/me/todo/lists")
    assert stack.sleeper.calls == [60.0]


def test_retry_after_http_date():
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:10 GMT", now=1445412480.0) == 10.0
    assert retry_after_seconds("garbage") is None
    assert retry_after_seconds(None) is None


async def test_401_refreshes_once_and_retries(stack, fake):
    await sign_in(stack)
    fake.access_tokens.clear()  # Graph no longer accepts the current access token
    data = await stack.graph.get("/me/todo/lists")
    assert len(data["value"]) == 2
    assert sum(1 for r in fake.requests if b"grant_type=refresh_token" in r.content) == 1


async def test_pagination_follows_next_link(stack, fake):
    await sign_in(stack)
    for i in range(5):
        fake.add_list(f"List {i}")
    lists = await stack.graph.get_all("/me/todo/lists")
    assert len(lists) == 7  # 2 built-in + 5, over 4 pages of 2
    assert sum(1 for r in fake.requests if r.url.path.endswith("/me/todo/lists")) == 4


async def test_404_message(stack):
    await sign_in(stack)
    with pytest.raises(GraphError, match="Not found"):
        await stack.graph.get("/me/todo/lists/NOPE")
