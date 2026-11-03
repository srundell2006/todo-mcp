from __future__ import annotations

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(__file__))

from fake_ms import FakeMicrosoft  # noqa: E402

from todo_mcp.auth import AuthManager  # noqa: E402
from todo_mcp.config import Config  # noqa: E402
from todo_mcp.graph import GraphClient  # noqa: E402
from todo_mcp.notify import Notifier  # noqa: E402
from todo_mcp.todo import TodoService  # noqa: E402


class Clock:
    def __init__(self, t: float = 1_793_750_000.0):  # 2026-11-03 ~ 14:00 Central
        self.t = t

    def __call__(self) -> float:
        return self.t


class Sleeper:
    def __init__(self, clock: Clock | None = None):
        self.calls: list[float] = []
        self.clock = clock

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self.clock:
            self.clock.t += seconds


@pytest.fixture
def fake() -> FakeMicrosoft:
    return FakeMicrosoft()


@pytest.fixture
def config(tmp_path) -> Config:
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    return Config(client_id="client-123", data_dir=str(data), ntfy_url="http://ntfy.test",
                  ntfy_topic="homelab-warning", ntfy_user="todo-mcp")


@pytest.fixture
async def http(fake):
    async with httpx.AsyncClient(transport=fake.transport()) as client:
        yield client


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def stack(config, http, clock):
    """Signed-in auth + graph + service against the fake."""

    class Stack:
        pass

    s = Stack()
    s.notifier = Notifier(config, http, clock=clock)
    s.sleeper = Sleeper(clock)
    s.auth = AuthManager(config, http, s.notifier, clock=clock, sleep=s.sleeper)
    s.graph = GraphClient(s.auth, http, config.graph_base, sleep=s.sleeper)
    s.todo = TodoService(s.graph, config.tz)
    return s


async def sign_in(stack) -> None:
    info = await stack.auth.start_sign_in()
    assert info["user_code"] == "ABCD-EFGH"
    await stack.auth._pending.task
    assert stack.auth.state == "signed_in", stack.auth.status()
