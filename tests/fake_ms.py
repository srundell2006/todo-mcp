"""In-memory stand-in for the Microsoft identity platform and Graph /me/todo, for tests."""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

LOGIN = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
GRAPH = "https://graph.microsoft.com/v1.0"


@dataclass
class FakeMicrosoft:
    page_size: int = 2
    account_id: str = "acct-1"
    username: str = "stephen@example.com"
    lists: dict[str, dict] = field(default_factory=dict)
    tasks: dict[str, dict[str, dict]] = field(default_factory=dict)
    checklist: dict[str, dict[str, dict]] = field(default_factory=dict)
    access_tokens: set[str] = field(default_factory=set)
    refresh_tokens: set[str] = field(default_factory=set)
    requests: list[httpx.Request] = field(default_factory=list)
    device_polls_before_success: int = 1
    device_error: str | None = None
    refresh_error: tuple[int, dict] | None = None
    throttle_next: list[httpx.Response] = field(default_factory=list)
    reject_filters: bool = False
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _polls: int = 0

    def __post_init__(self) -> None:
        self.add_list("Tasks", wellknown="defaultList")
        self.add_list("Flagged emails", wellknown="flaggedEmails")

    # ------------------------------------------------------------- fixtures

    def new_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}=="

    def add_list(self, name: str, wellknown: str = "none") -> dict:
        lid = self.new_id("L")
        self.lists[lid] = {"id": lid, "displayName": name, "wellknownListName": wellknown,
                           "isOwner": True, "isShared": False}
        self.tasks[lid] = {}
        return self.lists[lid]

    def add_task(self, list_id: str, **fields) -> dict:
        tid = self.new_id("T")
        task = {"id": tid, "title": "untitled", "status": "notStarted", "importance": "normal",
                "isReminderOn": False, "categories": [], "body": {"content": "", "contentType": "text"},
                "createdDateTime": "2026-11-01T15:00:00.0000000Z",
                "lastModifiedDateTime": "2026-11-01T15:00:00.0000000Z", **fields}
        self.tasks[list_id][tid] = task
        self.checklist[tid] = {}
        return task

    def issue_tokens(self) -> dict:
        n = next(self._ids)
        at, rt = f"at-{n}", f"rt-{n}"
        self.access_tokens.add(at)
        self.refresh_tokens.add(rt)
        return {"token_type": "Bearer", "access_token": at, "refresh_token": rt, "expires_in": 3600,
                "scope": "https://graph.microsoft.com/Tasks.ReadWrite https://graph.microsoft.com/User.Read"}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -------------------------------------------------------------- routing

    @staticmethod
    def _json(status: int, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers) if body is not None else httpx.Response(
            status, headers=headers)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith(LOGIN):
            return self._login(request)
        if url.startswith("http://ntfy.test"):
            return httpx.Response(200, json={"id": "x"})
        if url.startswith(GRAPH):
            auth = request.headers.get("Authorization", "")
            if auth.removeprefix("Bearer ") not in self.access_tokens:
                return self._json(401, {"error": {"code": "InvalidAuthenticationToken", "message": "expired"}})
            if self.throttle_next:
                return self.throttle_next.pop(0)
            return self._graph(request)
        return httpx.Response(404)

    def _login(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        path = request.url.path
        if path.endswith("/devicecode"):
            return self._json(200, {"device_code": "DEVICE-SECRET", "user_code": "ABCD-EFGH",
                                    "verification_uri": "https://microsoft.com/devicelogin",
                                    "expires_in": 900, "interval": 5})
        if form.get("grant_type", "").endswith("device_code"):
            if self.device_error:
                return self._json(400, {"error": self.device_error})
            self._polls += 1
            if self._polls <= self.device_polls_before_success:
                return self._json(400, {"error": "authorization_pending"})
            return self._json(200, self.issue_tokens())
        if form.get("grant_type") == "refresh_token":
            if self.refresh_error:
                status, body = self.refresh_error
                return self._json(status, body)
            if form.get("refresh_token") not in self.refresh_tokens:
                return self._json(400, {"error": "invalid_grant",
                                        "error_description": "AADSTS70000: The provided grant is invalid."})
            self.refresh_tokens.discard(form["refresh_token"])
            return self._json(200, self.issue_tokens())
        return self._json(400, {"error": "unsupported_grant_type"})

    def _page(self, request: httpx.Request, items: list[dict]) -> httpx.Response:
        q = parse_qs(urlsplit(str(request.url)).query)
        skip = int(q.get("$skip", ["0"])[0])
        page = items[skip: skip + self.page_size]
        body: dict = {"value": page}
        if skip + self.page_size < len(items):
            base = str(request.url).split("?")[0]
            rest = {k: v[0] for k, v in q.items() if k != "$skip"}
            qs = "&".join(f"{k}={v}" for k, v in rest.items())
            body["@odata.nextLink"] = f"{base}?{qs + '&' if qs else ''}$skip={skip + self.page_size}"
        return self._json(200, body)

    def _graph(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path.removeprefix("/v1.0"))
        method = request.method
        body = json.loads(request.content) if request.content else None
        q = parse_qs(urlsplit(str(request.url)).query)

        if path == "/me":
            return self._json(200, {"id": self.account_id, "userPrincipalName": self.username,
                                    "displayName": "Stephen"})
        if path == "/me/todo/lists":
            if method == "GET":
                return self._page(request, list(self.lists.values()))
            lst = self.add_list(body["displayName"])
            return self._json(201, lst)
        m = re.fullmatch(r"/me/todo/lists/([^/]+)(/tasks(?:/([^/]+)(/checklistItems(?:/([^/]+))?)?)?)?", path)
        if not m:
            return self._json(404, {"error": {"code": "NotFound", "message": "no route"}})
        lid, tasks_part, tid, cl_part, cid = m.groups()
        if lid not in self.lists:
            return self._json(404, {"error": {"code": "ErrorItemNotFound", "message": "list not found"}})
        if not tasks_part:
            if method == "PATCH":
                self.lists[lid].update(body)
                return self._json(200, self.lists[lid])
            if method == "DELETE":
                del self.lists[lid]
                return self._json(204)
            return self._json(200, self.lists[lid])
        tasks = self.tasks[lid]
        if tid is None:
            if method == "POST":
                task = self.add_task(lid, **body)
                return self._json(201, task)
            items = list(tasks.values())
            flt = q.get("$filter", [None])[0]
            if flt:
                if self.reject_filters:
                    return self._json(400, {"error": {"code": "BadRequest", "message": "filter not supported"}})
                fm = re.fullmatch(r"status (eq|ne) '(\w+)'", flt)
                op, val = fm.groups()
                items = [t for t in items if (t["status"] == val) == (op == "eq")]
            if q.get("$expand", [None])[0] == "checklistItems":
                items = [{**t, "checklistItems": list(self.checklist.get(t["id"], {}).values())} for t in items]
            return self._page(request, items)
        if tid not in tasks:
            return self._json(404, {"error": {"code": "ErrorItemNotFound", "message": "task not found"}})
        if not cl_part:
            if method == "PATCH" and body.get("status") == "completed" and tasks[tid].get("recurrence"):
                # Like To Do: the completed occurrence becomes a separate completed task and the
                # series (same ID) moves on to its next occurrence (weekly here: +7 days).
                from datetime import datetime, timedelta
                cur = tasks[tid]
                copy = {k: v for k, v in cur.items() if k not in ("id", "recurrence", "status")}
                self.add_task(lid, **copy, status="completed",
                              completedDateTime={"dateTime": "2026-11-03T16:00:00.0000000", "timeZone": "UTC"})
                due = datetime.fromisoformat(cur["dueDateTime"]["dateTime"][:19]) + timedelta(days=7)
                cur["dueDateTime"] = {"dateTime": due.strftime("%Y-%m-%dT%H:%M:%S.0000000"),
                                      "timeZone": cur["dueDateTime"]["timeZone"]}
                return self._json(200, cur)
            if method == "PATCH":
                for k, v in body.items():
                    if v is None:
                        tasks[tid].pop(k, None)
                    else:
                        tasks[tid][k] = v
                if body.get("status") == "completed":
                    tasks[tid]["completedDateTime"] = {"dateTime": "2026-11-03T16:00:00.0000000", "timeZone": "UTC"}
                elif "status" in body:
                    tasks[tid].pop("completedDateTime", None)
                return self._json(200, tasks[tid])
            if method == "DELETE":
                del tasks[tid]
                return self._json(204)
            return self._json(200, tasks[tid])
        items = self.checklist[tid]
        if cid is None:
            if method == "POST":
                iid = self.new_id("C")
                items[iid] = {"id": iid, "displayName": body["displayName"], "isChecked": body.get("isChecked", False)}
                return self._json(201, items[iid])
            return self._page(request, list(items.values()))
        if cid not in items:
            return self._json(404, {"error": {"code": "ErrorItemNotFound", "message": "item not found"}})
        if method == "PATCH":
            items[cid].update(body)
            return self._json(200, items[cid])
        if method == "DELETE":
            del items[cid]
            return self._json(204)
        return self._json(200, items[cid])
