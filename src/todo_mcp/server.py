"""MCP server: Microsoft To Do tools over streamable HTTP.

Ports
- 0.0.0.0:3000  /mcp (streamable HTTP, stateless, JSON responses) and /healthz.
                 Reached only through Traefik + mcp-auth-gate; no published port.
- 127.0.0.1:3001 admin endpoint for `todo-mcp-admin` (sign-in, status, reset). Bound to
                 the container's loopback, so it is reachable only via `docker exec`.
"""

# No `from __future__ import annotations` here: tool signatures are evaluated at definition
# time, so their Field descriptions can include the configured time zone.
import asyncio
import contextlib
import logging
import os
import sys
from typing import Annotated, Any, Literal

import httpx
import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import __version__, securefile
from .auth import AuthError, AuthManager
from .config import Config
from .graph import GraphClient, GraphError
from .notify import Notifier
from .recurrence import RecurrenceSpec
from .todo import TodoError, TodoService

log = logging.getLogger("todo_mcp")

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
IDEMPOTENT_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

ListName = Annotated[str, Field(description="The list's name (case-insensitive) or its ID.")]
TaskId = Annotated[str, Field(description="Task ID, as returned by list_tasks, search_tasks or create_task.")]
Importance = Literal["low", "normal", "high"]
TaskStatus = Literal["notStarted", "inProgress", "completed", "waitingOnOthers", "deferred"]
DateArg = Annotated[
    str | None,
    Field(description="Calendar date: YYYY-MM-DD, 'today', 'tomorrow', 'yesterday', or a weekday name (next one)."),
]


class App:
    def __init__(self, config: Config, http: httpx.AsyncClient | None = None):
        self.config = config
        self.http = http or httpx.AsyncClient(headers={"User-Agent": f"todo-mcp/{__version__}"})
        self.notifier = Notifier(config, self.http)
        self.auth = AuthManager(config, self.http, self.notifier)
        self.graph = GraphClient(self.auth, self.http, config.graph_base)
        self.todo = TodoService(self.graph, config.tz)
        self.mcp = build_mcp(self)


async def _run(coro) -> dict[str, Any]:
    try:
        return await coro
    except (TodoError, AuthError, GraphError) as exc:
        raise ToolError(str(exc)) from exc


def build_mcp(app: App) -> MCPServer:
    tzname = app.config.timezone
    mcp = MCPServer(
        "todo-mcp",
        title="Microsoft To Do",
        version=__version__,
        instructions=(
            "Microsoft To Do for one personal Microsoft account.\n"
            f"- Dates and times are {tzname} unless they carry an offset. Due dates are calendar dates.\n"
            "- A list can be given by its name or its ID. Task IDs come from list_tasks, search_tasks or "
            "create_task, and belong to their list.\n"
            "- Tools whose description starts with DESTRUCTIVE delete data permanently. Always tell the user "
            "exactly what will be deleted and wait for an explicit yes before calling them.\n"
            "- If a tool says Microsoft sign-in is needed, call start_microsoft_sign_in and show the user "
            "the link and code."
        ),
    )
    todo, auth = app.todo, app.auth

    # ------------------------------------------------------------------ auth

    @mcp.tool(annotations=READ)
    async def auth_status() -> dict[str, Any]:
        """Show whether the server is signed in to Microsoft, as which account, and when the token
        was last renewed. Use it to diagnose sign-in problems."""
        return auth.status()

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def start_microsoft_sign_in() -> dict[str, Any]:
        """Start (or show the pending) Microsoft device-code sign-in. Only needed when another tool
        says sign-in is required or has expired. Show the user the link and code from the result;
        they finish in their browser and the server keeps the renewed sign-in from then on."""
        return await _run(auth.start_sign_in())

    # ----------------------------------------------------------------- lists

    @mcp.tool(annotations=READ)
    async def list_task_lists() -> dict[str, Any]:
        """List all To Do lists with their IDs. 'built_in' marks lists To Do manages itself
        (defaultList = 'Tasks', flaggedEmails), which can't be renamed or deleted."""
        return await _run(todo.list_lists())

    @mcp.tool(annotations=WRITE)
    async def create_task_list(name: Annotated[str, Field(description="Name for the new list.")]) -> dict[str, Any]:
        """Create a new To Do list."""
        return await _run(todo.create_list(name))

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def rename_task_list(
        list_name: ListName, new_name: Annotated[str, Field(description="The new name.")]
    ) -> dict[str, Any]:
        """Rename a To Do list."""
        return await _run(todo.rename_list(list_name, new_name))

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_task_list(list_name: ListName) -> dict[str, Any]:
        """DESTRUCTIVE: permanently deletes a whole To Do list AND every task in it. There is no undo
        and no recycle bin. Before calling, tell the user the list's name and how many tasks it holds
        (list_tasks with status 'all'), and call only after they explicitly confirm."""
        return await _run(todo.delete_list(list_name))

    # ----------------------------------------------------------------- tasks

    @mcp.tool(annotations=READ)
    async def list_tasks(
        list_name: Annotated[str | None, Field(description="Only this list (name or ID). Omit for all lists.")] = None,
        status: Annotated[
            Literal["open", "completed", "all", "notStarted", "inProgress", "waitingOnOthers", "deferred"],
            Field(description="'open' (default) = anything not completed."),
        ] = "open",
        due_on: DateArg = None,
        due_from: Annotated[str | None, Field(description="Due on or after this date (inclusive).")] = None,
        due_to: Annotated[str | None, Field(description="Due on or before this date (inclusive).")] = None,
        overdue: Annotated[bool, Field(description="Only tasks due before today and not completed.")] = False,
        no_due_date: Annotated[bool, Field(description="Only tasks without a due date.")] = False,
        importance: Annotated[Importance | None, Field(description="Only this importance.")] = None,
        category: Annotated[str | None, Field(description="Only tasks with this category.")] = None,
        limit: Annotated[int, Field(ge=1, le=500, description="Maximum tasks to return (default 50).")] = 50,
    ) -> dict[str, Any]:
        """List tasks, filtered by list, status and due date. Sorted by due date (no date last), then
        importance. Results include each task's ID and list for follow-up calls."""
        return await _run(todo.list_tasks(
            list_name, status, due_on, due_from, due_to, overdue, no_due_date, importance, category, limit
        ))

    @mcp.tool(annotations=READ)
    async def search_tasks(
        query: Annotated[str, Field(description="Words to find; every word must appear (case-insensitive).")],
        list_name: Annotated[str | None, Field(description="Only this list. Omit to search all lists.")] = None,
        include_completed: Annotated[bool, Field(description="Also search completed tasks.")] = False,
        limit: Annotated[int, Field(ge=1, le=200)] = 25,
    ) -> dict[str, Any]:
        """Search tasks across all lists by title, notes, checklist items and categories."""
        return await _run(todo.search_tasks(query, list_name, include_completed, limit))

    @mcp.tool(annotations=READ)
    async def get_task(list_name: ListName, task_id: TaskId) -> dict[str, Any]:
        """Get one task with all its details, including checklist items and their IDs."""
        return await _run(todo.get_task(list_name, task_id))

    @mcp.tool(annotations=WRITE)
    async def create_task(
        list_name: ListName,
        title: Annotated[str, Field(description="Task title.")],
        notes: Annotated[str | None, Field(description="Notes (plain text).")] = None,
        due_date: DateArg = None,
        reminder: Annotated[
            str | None,
            Field(description=f"Reminder date-time, {tzname} unless an offset is given: e.g. "
                              "2026-11-05T09:00, or 'tomorrow 08:30'."),
        ] = None,
        importance: Importance | None = None,
        recurrence: Annotated[
            RecurrenceSpec | None, Field(description="Make the task repeat (needs a due date; today if none).")
        ] = None,
        categories: Annotated[list[str] | None, Field(description="Category names (Outlook categories).")] = None,
        checklist: Annotated[list[str] | None, Field(description="Checklist items (steps) to add.")] = None,
    ) -> dict[str, Any]:
        """Create a task in a list. Returns the new task with its ID."""
        return await _run(todo.create_task(
            list_name, title, notes, due_date, reminder, importance, recurrence, categories, checklist
        ))

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def update_task(
        list_name: ListName,
        task_id: TaskId,
        title: str | None = None,
        notes: Annotated[str | None, Field(description="Replace the notes.")] = None,
        clear_notes: bool = False,
        due_date: DateArg = None,
        clear_due_date: bool = False,
        reminder: Annotated[str | None, Field(description=f"New reminder date-time ({tzname}).")] = None,
        clear_reminder: bool = False,
        importance: Importance | None = None,
        status: Annotated[
            TaskStatus | None, Field(description="Prefer complete_task / reopen_task for completing.")
        ] = None,
        recurrence: Annotated[RecurrenceSpec | None, Field(description="Replace the repeat pattern.")] = None,
        clear_recurrence: bool = False,
        categories: Annotated[list[str] | None, Field(description="Replace all categories ([] removes all).")] = None,
    ) -> dict[str, Any]:
        """Change a task. Only the fields you pass change; use the clear_* flags to remove a value."""
        return await _run(todo.update_task(
            list_name, task_id, title, notes, clear_notes, due_date, clear_due_date, reminder, clear_reminder,
            importance, status, recurrence, clear_recurrence, categories,
        ))

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def complete_task(list_name: ListName, task_id: TaskId) -> dict[str, Any]:
        """Mark a task completed. For a repeating task, To Do stores the completed occurrence as a
        separate completed task and moves this task (same ID) to the next occurrence; the result says so."""
        return await _run(todo.set_status(list_name, task_id, "completed"))

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def reopen_task(list_name: ListName, task_id: TaskId) -> dict[str, Any]:
        """Reopen a completed task (sets it back to not started)."""
        return await _run(todo.set_status(list_name, task_id, "notStarted"))

    @mcp.tool(annotations=DESTRUCTIVE)
    async def delete_task(list_name: ListName, task_id: TaskId) -> dict[str, Any]:
        """DESTRUCTIVE: permanently deletes a task, with its notes and checklist. There is no undo.
        Before calling, show the user the task's title and list and call only after they explicitly
        confirm. To mark it done instead, use complete_task."""
        return await _run(todo.delete_task(list_name, task_id))

    # ------------------------------------------------------------- checklist

    @mcp.tool(annotations=WRITE)
    async def add_checklist_item(
        list_name: ListName,
        task_id: TaskId,
        text: Annotated[str, Field(description="The checklist item (step) text.")],
        checked: bool = False,
    ) -> dict[str, Any]:
        """Add a checklist item (step) to a task."""
        return await _run(todo.add_checklist_item(list_name, task_id, text, checked))

    @mcp.tool(annotations=IDEMPOTENT_WRITE)
    async def update_checklist_item(
        list_name: ListName,
        task_id: TaskId,
        item_id: Annotated[str, Field(description="Checklist item ID, from get_task.")],
        text: str | None = None,
        checked: bool | None = None,
    ) -> dict[str, Any]:
        """Change a checklist item's text, or tick / untick it."""
        return await _run(todo.update_checklist_item(list_name, task_id, item_id, text, checked))

    @mcp.tool(annotations=DESTRUCTIVE)
    async def remove_checklist_item(
        list_name: ListName,
        task_id: TaskId,
        item_id: Annotated[str, Field(description="Checklist item ID, from get_task.")],
    ) -> dict[str, Any]:
        """DESTRUCTIVE: permanently removes one checklist item (step) from a task. No undo. Confirm with
        the user first. To tick it off instead, use update_checklist_item with checked=true."""
        return await _run(todo.remove_checklist_item(list_name, task_id, item_id))

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__, "auth": auth.state})

    return mcp


def build_http_app(app: App) -> Starlette:
    return app.mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        # Only reachable through Traefik + forwardAuth (no published port), and Claude's
        # connector may send an Origin header, so host/origin checks would only get in the way.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host=app.config.host,
    )


def build_admin_app(app: App) -> Starlette:
    async def status(_: Request) -> JSONResponse:
        return JSONResponse(app.auth.status())

    async def login(_: Request) -> JSONResponse:
        try:
            return JSONResponse(await app.auth.start_sign_in())
        except AuthError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def reset(request: Request) -> JSONResponse:
        body = await request.json()
        if body.get("confirm") is not True:
            return JSONResponse({"error": "send {\"confirm\": true}"}, status_code=400)
        await app.auth.reset()
        return JSONResponse({"ok": True})

    async def notify_test(_: Request) -> JSONResponse:
        return JSONResponse({"sent": await app.notifier.test(), "enabled": app.notifier.enabled})

    async def refresh(_: Request) -> JSONResponse:
        try:
            await app.auth.refresh(force=True)
        except AuthError as exc:
            return JSONResponse({"error": str(exc), **app.auth.status()}, status_code=400)
        return JSONResponse(app.auth.status())

    return Starlette(routes=[
        Route("/status", status, methods=["GET"]),
        Route("/login", login, methods=["POST"]),
        Route("/reset", reset, methods=["POST"]),
        Route("/notify-test", notify_test, methods=["POST"]),
        Route("/refresh", refresh, methods=["POST"]),
    ])


class _NoSignalServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def serve(config: Config) -> None:
    app = App(config)
    if app.notifier.enabled:
        app.notifier.ensure_password()
    log.info(
        "todo-mcp %s starting: authority=%s tz=%s auth=%s ntfy=%s",
        __version__, config.authority, config.timezone, app.auth.state,
        f"{config.ntfy_topic}" if app.notifier.enabled else "off",
    )
    main = uvicorn.Server(uvicorn.Config(
        build_http_app(app), host=config.host, port=config.port, log_level=config.log_level.lower(),
        proxy_headers=False, server_header=False,
    ))
    admin = _NoSignalServer(uvicorn.Config(
        build_admin_app(app), host="127.0.0.1", port=config.admin_port, log_level="warning", access_log=False,
    ))
    keepalive = asyncio.create_task(app.auth.keepalive_loop())
    admin_task = asyncio.create_task(admin.serve())
    try:
        await main.serve()
    finally:
        admin.should_exit = True
        keepalive.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await keepalive
        await admin_task
        await app.http.aclose()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level, stream=sys.stdout, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # httpx logs every request URL at INFO; Graph URLs carry no secrets, but keep the log quiet.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    config = Config.from_env()
    setup_logging(config.log_level)
    os.umask(0o077)
    securefile.ensure_private_dir(config.data_dir)
    if not config.client_id:
        log.warning("TODO_MCP_CLIENT_ID is not set; tools will report that sign-in isn't configured")
    asyncio.run(serve(config))


if __name__ == "__main__":  # pragma: no cover
    main()
