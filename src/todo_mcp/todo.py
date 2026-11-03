"""Microsoft To Do operations on top of Graph /me/todo, with local-time formatting."""

from __future__ import annotations

import asyncio
import html
import re
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from . import recurrence as rec
from . import timeutil as tu
from .graph import GraphClient, GraphError, seg

LIST_CACHE_SECONDS = 30
MAX_TASKS_PER_LIST = 5000
IMPORTANCE_RANK = {"high": 0, "normal": 1, "low": 2}
STATUS_FILTERS = {"open", "completed", "all", "notStarted", "inProgress", "waitingOnOthers", "deferred"}


class TodoError(Exception):
    """A request problem with a message meant for the user."""


def _plain_text(body: dict | None) -> str | None:
    if not body:
        return None
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        content = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", content)
        content = html.unescape(re.sub(r"<[^>]+>", "", content))
    content = content.strip()
    return content or None


class TodoService:
    def __init__(self, graph: GraphClient, tz: ZoneInfo, now: Callable[[], datetime] | None = None):
        self.graph = graph
        self.tz = tz
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lists_cache: tuple[float, list[dict]] | None = None

    # ------------------------------------------------------------------ helpers

    def today(self) -> date:
        return tu.today(self.tz, self._now())

    def _date(self, value: str) -> date:
        try:
            return tu.parse_date_input(value, self.tz, self._now())
        except tu.DateInputError as exc:
            raise TodoError(str(exc)) from exc

    def _datetime(self, value: str) -> datetime:
        try:
            return tu.parse_datetime_input(value, self.tz, self._now())
        except tu.DateInputError as exc:
            raise TodoError(str(exc)) from exc

    @staticmethod
    def _tasks_path(list_id: str, task_id: str | None = None) -> str:
        path = f"/me/todo/lists/{seg(list_id)}/tasks"
        return f"{path}/{seg(task_id)}" if task_id else path

    def format_list(self, raw: dict) -> dict:
        wk = raw.get("wellknownListName")
        return {
            "id": raw.get("id"),
            "name": raw.get("displayName"),
            "built_in": None if wk in (None, "none") else wk,
            "is_owner": raw.get("isOwner"),
            "is_shared": raw.get("isShared"),
        }

    def format_item(self, raw: dict) -> dict:
        return {"id": raw.get("id"), "text": raw.get("displayName"), "checked": bool(raw.get("isChecked"))}

    def format_task(self, raw: dict, lst: dict | None, checklist: list[dict] | None = None) -> dict:
        due = tu.parse_graph_due_date(raw.get("dueDateTime"), self.tz)
        status = raw.get("status")
        reminder = tu.parse_graph_datetime(raw.get("reminderDateTime"), self.tz)
        # To Do stores the completion as a date (usually midnight UTC), like due dates.
        completed = tu.parse_graph_due_date(raw.get("completedDateTime"), self.tz)
        recurrence = raw.get("recurrence")
        out: dict[str, Any] = {
            "id": raw.get("id"),
            "list": (lst or {}).get("displayName"),
            "list_id": (lst or {}).get("id"),
            "title": raw.get("title"),
            "status": status,
            "importance": raw.get("importance"),
            "due_date": due.isoformat() if due else None,
            "overdue": bool(due and status != "completed" and due < self.today()),
            "reminder": tu.iso_local(reminder) if raw.get("isReminderOn") else None,
            "recurrence": (
                {"summary": rec.describe(recurrence), "pattern": recurrence.get("pattern"),
                 "range": recurrence.get("range")}
                if recurrence else None
            ),
            "categories": raw.get("categories") or [],
            "notes": _plain_text(raw.get("body")),
            "completed_on": completed.isoformat() if completed and status == "completed" else None,
            "created": tu.iso_local(tu.parse_offset_timestamp(raw.get("createdDateTime"), self.tz)),
            "modified": tu.iso_local(tu.parse_offset_timestamp(raw.get("lastModifiedDateTime"), self.tz)),
        }
        items = checklist if checklist is not None else raw.get("checklistItems")
        if items is not None:
            out["checklist"] = [self.format_item(i) for i in items]
        return out

    # -------------------------------------------------------------------- lists

    async def _lists(self, fresh: bool = False) -> list[dict]:
        if not fresh and self._lists_cache and time.monotonic() - self._lists_cache[0] < LIST_CACHE_SECONDS:
            return self._lists_cache[1]
        lists = await self.graph.get_all("/me/todo/lists")
        self._lists_cache = (time.monotonic(), lists)
        return lists

    def _invalidate_lists(self) -> None:
        self._lists_cache = None

    async def resolve_list(self, ref: str) -> dict:
        ref = (ref or "").strip()
        if not ref:
            raise TodoError("Say which list (its name or ID). Use list_task_lists to see them.")
        for fresh in (False, True):
            lists = await self._lists(fresh=fresh)
            for lst in lists:
                if lst.get("id") == ref:
                    return lst
            if ref.casefold() in ("default", "tasks (default)"):
                for lst in lists:
                    if lst.get("wellknownListName") == "defaultList":
                        return lst
            matches = [lst for lst in lists if (lst.get("displayName") or "").casefold() == ref.casefold()]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                ids = ", ".join(m["id"] for m in matches)
                raise TodoError(f"More than one list is named {ref!r}; pass the ID instead. IDs: {ids}")
        names = ", ".join(sorted(repr(lst.get("displayName")) for lst in lists))
        raise TodoError(f"No list named {ref!r}. Lists: {names}")

    async def list_lists(self) -> dict:
        lists = await self._lists(fresh=True)
        return {"count": len(lists), "lists": [self.format_list(x) for x in lists]}

    async def create_list(self, name: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise TodoError("A list needs a name.")
        existing = [x for x in await self._lists() if (x.get("displayName") or "").casefold() == name.casefold()]
        raw = await self.graph.request("POST", "/me/todo/lists", json={"displayName": name})
        self._invalidate_lists()
        out = {"created": self.format_list(raw or {})}
        if existing:
            out["note"] = f"A list named {name!r} already existed; To Do allows duplicates, so there are now two."
        return out

    @staticmethod
    def _guard_built_in(lst: dict, action: str) -> None:
        wk = lst.get("wellknownListName")
        if wk not in (None, "none"):
            raise TodoError(f"{lst.get('displayName')!r} is a built-in To Do list ({wk}) and can't be {action}.")

    async def rename_list(self, ref: str, new_name: str) -> dict:
        new_name = (new_name or "").strip()
        if not new_name:
            raise TodoError("Give the new name.")
        lst = await self.resolve_list(ref)
        self._guard_built_in(lst, "renamed")
        raw = await self.graph.request("PATCH", f"/me/todo/lists/{seg(lst['id'])}", json={"displayName": new_name})
        self._invalidate_lists()
        return {"renamed_from": lst.get("displayName"), "list": self.format_list(raw or {**lst, "displayName": new_name})}

    async def delete_list(self, ref: str) -> dict:
        lst = await self.resolve_list(ref)
        self._guard_built_in(lst, "deleted")
        await self.graph.request("DELETE", f"/me/todo/lists/{seg(lst['id'])}")
        self._invalidate_lists()
        return {"deleted_list": lst.get("displayName"), "id": lst.get("id")}

    # -------------------------------------------------------------------- tasks

    @staticmethod
    def _status_filter(status: str) -> str | None:
        if status == "open":
            return "status ne 'completed'"
        if status == "all":
            return None
        return f"status eq '{status}'"

    @staticmethod
    def _status_ok(task: dict, status: str) -> bool:
        s = task.get("status")
        if status == "all":
            return True
        if status == "open":
            return s != "completed"
        return s == status

    async def _fetch_tasks(self, lst: dict, status: str, expand_checklist: bool = False) -> list[dict]:
        params: dict[str, str] = {}
        flt = self._status_filter(status)
        if flt:
            params["$filter"] = flt
        if expand_checklist:
            params["$expand"] = "checklistItems"
        path = self._tasks_path(lst["id"])
        try:
            raw = await self.graph.get_all(path, params or None, max_items=MAX_TASKS_PER_LIST)
        except GraphError as exc:
            if exc.status != 400 or not params:
                raise
            # Fall back to filtering locally if Graph rejects the query options.
            raw = await self.graph.get_all(path, None, max_items=MAX_TASKS_PER_LIST)
        return [t for t in raw if self._status_ok(t, status)]

    async def _fetch_many(self, lists: Iterable[dict], status: str, expand_checklist: bool = False):
        sem = asyncio.Semaphore(4)

        async def one(lst: dict):
            async with sem:
                return lst, await self._fetch_tasks(lst, status, expand_checklist)

        return await asyncio.gather(*(one(lst) for lst in lists))

    def _sort_key(self, t: dict):
        return (
            t["due_date"] is None,
            t["due_date"] or "",
            IMPORTANCE_RANK.get(t.get("importance") or "normal", 1),
            t.get("created") or "",
        )

    async def list_tasks(
        self,
        list_ref: str | None = None,
        status: str = "open",
        due_on: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
        overdue: bool = False,
        no_due_date: bool = False,
        importance: str | None = None,
        category: str | None = None,
        limit: int = 50,
    ) -> dict:
        if status not in STATUS_FILTERS:
            raise TodoError(f"Unknown status {status!r}. Use one of: {', '.join(sorted(STATUS_FILTERS))}.")
        if no_due_date and (due_on or due_from or due_to or overdue):
            raise TodoError("no_due_date can't be combined with due-date filters.")
        on = self._date(due_on) if due_on else None
        lo = self._date(due_from) if due_from else None
        hi = self._date(due_to) if due_to else None
        if lo and hi and lo > hi:
            raise TodoError("due_from is after due_to.")
        today = self.today()
        lists = [await self.resolve_list(list_ref)] if list_ref else await self._lists()
        results = await self._fetch_many(lists, status)

        matched: list[dict] = []
        for lst, tasks in results:
            for raw in tasks:
                t = self.format_task(raw, lst)
                d = date.fromisoformat(t["due_date"]) if t["due_date"] else None
                if no_due_date and d is not None:
                    continue
                if (on or lo or hi or overdue) and d is None:
                    continue
                if on and d != on:
                    continue
                if lo and d < lo:
                    continue
                if hi and d > hi:
                    continue
                if overdue and not (d < today and t["status"] != "completed"):
                    continue
                if importance and t["importance"] != importance:
                    continue
                if category and category.casefold() not in [c.casefold() for c in t["categories"]]:
                    continue
                matched.append(t)
        if status == "completed":
            matched.sort(key=lambda t: t.get("completed_on") or "", reverse=True)
        else:
            matched.sort(key=self._sort_key)
        limit = max(1, min(int(limit), 500))
        return {
            "timezone": self.tz.key,
            "today": today.isoformat(),
            "lists_searched": [lst.get("displayName") for lst in lists],
            "total_matched": len(matched),
            "returned": min(limit, len(matched)),
            "truncated": len(matched) > limit,
            "tasks": matched[:limit],
        }

    async def search_tasks(
        self, query: str, list_ref: str | None = None, include_completed: bool = False, limit: int = 25
    ) -> dict:
        terms = [t for t in (query or "").casefold().split() if t]
        if not terms:
            raise TodoError("Give some words to search for.")
        lists = [await self.resolve_list(list_ref)] if list_ref else await self._lists()
        status = "all" if include_completed else "open"
        try:
            results = await self._fetch_many(lists, status, expand_checklist=True)
        except GraphError as exc:
            if exc.status != 400:
                raise
            results = await self._fetch_many(lists, status)
        hits: list[dict] = []
        for lst, tasks in results:
            for raw in tasks:
                t = self.format_task(raw, lst)
                fields = {
                    "title": t["title"] or "",
                    "notes": t["notes"] or "",
                    "checklist": " ".join(i["text"] or "" for i in t.get("checklist", [])),
                    "categories": " ".join(t["categories"]),
                }
                haystack = " ".join(fields.values()).casefold()
                if all(term in haystack for term in terms):
                    t["matched_in"] = [k for k, v in fields.items() if any(term in v.casefold() for term in terms)]
                    hits.append(t)
        hits.sort(key=lambda t: (t["status"] == "completed", "title" not in t["matched_in"], *self._sort_key(t)))
        limit = max(1, min(int(limit), 200))
        return {
            "query": query,
            "lists_searched": len(lists),
            "total_matched": len(hits),
            "returned": min(limit, len(hits)),
            "truncated": len(hits) > limit,
            "tasks": hits[:limit],
        }

    async def _get_raw(self, lst: dict, task_id: str) -> dict:
        return await self.graph.get(self._tasks_path(lst["id"], task_id))

    async def _checklist(self, lst: dict, task_id: str) -> list[dict]:
        return await self.graph.get_all(f"{self._tasks_path(lst['id'], task_id)}/checklistItems")

    async def get_task(self, list_ref: str, task_id: str) -> dict:
        lst = await self.resolve_list(list_ref)
        raw = await self._get_raw(lst, task_id)
        return self.format_task(raw, lst, await self._checklist(lst, task_id))

    def _validate_categories(self, categories: list[str] | None) -> list[str] | None:
        if categories is None:
            return None
        cleaned = [c.strip() for c in categories if c and c.strip()]
        return list(dict.fromkeys(cleaned))

    async def create_task(
        self,
        list_ref: str,
        title: str,
        notes: str | None = None,
        due_date: str | None = None,
        reminder: str | None = None,
        importance: str | None = None,
        recurrence: rec.RecurrenceSpec | None = None,
        categories: list[str] | None = None,
        checklist: list[str] | None = None,
    ) -> dict:
        title = (title or "").strip()
        if not title:
            raise TodoError("A task needs a title.")
        lst = await self.resolve_list(list_ref)
        notes_out: list[str] = []
        body: dict[str, Any] = {"title": title}
        if notes:
            body["body"] = {"content": notes, "contentType": "text"}
        due = self._date(due_date) if due_date else None
        if recurrence and not due:
            due = self.today()
            notes_out.append("Due date set to today, because a repeating task needs one.")
        if due:
            body["dueDateTime"] = tu.due_to_graph(due, self.tz)
        if reminder:
            when = self._datetime(reminder)
            body["reminderDateTime"] = tu.to_graph(when, self.tz)
            body["isReminderOn"] = True
            if when < self._now():
                notes_out.append("The reminder time is in the past.")
        if importance:
            body["importance"] = importance
        if recurrence:
            try:
                body["recurrence"] = rec.build(recurrence, due)
            except ValueError as exc:
                raise TodoError(str(exc)) from exc
        cats = self._validate_categories(categories)
        if cats:
            body["categories"] = cats
        raw = await self.graph.request("POST", self._tasks_path(lst["id"]), json=body) or {}
        items: list[dict] = []
        for text in checklist or []:
            if text and text.strip():
                item = await self.graph.request(
                    "POST", f"{self._tasks_path(lst['id'], raw['id'])}/checklistItems",
                    json={"displayName": text.strip()},
                )
                items.append(item or {})
        out = {"created": self.format_task(raw, lst, items if checklist else None)}
        if notes_out:
            out["notes"] = notes_out
        return out

    async def update_task(
        self,
        list_ref: str,
        task_id: str,
        title: str | None = None,
        notes: str | None = None,
        clear_notes: bool = False,
        due_date: str | None = None,
        clear_due_date: bool = False,
        reminder: str | None = None,
        clear_reminder: bool = False,
        importance: str | None = None,
        status: str | None = None,
        recurrence: rec.RecurrenceSpec | None = None,
        clear_recurrence: bool = False,
        categories: list[str] | None = None,
    ) -> dict:
        for value, flag, what in (
            (notes, clear_notes, "notes"), (due_date, clear_due_date, "due_date"),
            (reminder, clear_reminder, "reminder"), (recurrence, clear_recurrence, "recurrence"),
        ):
            if value is not None and flag:
                raise TodoError(f"Either set {what} or clear it, not both.")
        lst = await self.resolve_list(list_ref)
        current = await self._get_raw(lst, task_id)
        patch: dict[str, Any] = {}
        notes_out: list[str] = []
        if title is not None:
            if not title.strip():
                raise TodoError("The title can't be empty.")
            patch["title"] = title.strip()
        if notes is not None:
            patch["body"] = {"content": notes, "contentType": "text"}
        if clear_notes:
            patch["body"] = {"content": "", "contentType": "text"}
        new_due = self._date(due_date) if due_date else None
        if new_due:
            patch["dueDateTime"] = tu.due_to_graph(new_due, self.tz)
        if clear_due_date:
            if current.get("recurrence") and not clear_recurrence:
                raise TodoError("This task repeats, and a repeating task needs a due date. "
                                "Clear the recurrence too (clear_recurrence) or keep the due date.")
            patch["dueDateTime"] = None
        if reminder is not None:
            when = self._datetime(reminder)
            patch["reminderDateTime"] = tu.to_graph(when, self.tz)
            patch["isReminderOn"] = True
            if when < self._now():
                notes_out.append("The reminder time is in the past.")
        if clear_reminder:
            patch["isReminderOn"] = False
            patch["reminderDateTime"] = None
        if importance is not None:
            patch["importance"] = importance
        if status is not None:
            patch["status"] = status
        if recurrence is not None:
            anchor = new_due or tu.parse_graph_due_date(current.get("dueDateTime"), self.tz)
            if anchor is None:
                anchor = self.today()
                patch["dueDateTime"] = tu.due_to_graph(anchor, self.tz)
                notes_out.append("Due date set to today, because a repeating task needs one.")
            try:
                patch["recurrence"] = rec.build(recurrence, anchor)
            except ValueError as exc:
                raise TodoError(str(exc)) from exc
        if clear_recurrence:
            patch["recurrence"] = None
        if categories is not None:
            patch["categories"] = self._validate_categories(categories) or []
        if not patch:
            raise TodoError("Nothing to change: give at least one field to update.")
        raw = await self.graph.request("PATCH", self._tasks_path(lst["id"], task_id), json=patch)
        updated = self.format_task(self._after_patch(current, patch, raw), lst)
        repeating = (current.get("recurrence") and not clear_recurrence) or recurrence is not None
        if new_due and repeating and updated["due_date"] != new_due.isoformat():
            # To Do keeps a series on its pattern. Moving the current occurrence to an
            # off-pattern date splits it off as a one-off task with the requested date.
            notes_out.append(
                f"This task repeats ({updated['recurrence']['summary'] if updated['recurrence'] else 'series'}). "
                f"To Do kept the series on its pattern (next due {updated['due_date']}) and may have split this "
                f"occurrence off as a separate one-off task due {new_due.isoformat()}. Check with list_tasks."
            )
        out: dict[str, Any] = {"updated": updated, "changed": sorted(patch)}
        if notes_out:
            out["notes"] = notes_out
        return out

    @staticmethod
    def _after_patch(current: dict, patch: dict, raw: dict | None) -> dict:
        """The task as it is after a PATCH: Graph's response when it sent the whole task,
        otherwise the previous state with the patch applied (None = removed)."""
        if raw and raw.get("id"):
            return raw
        merged = {**current, **patch}
        return {k: v for k, v in merged.items() if v is not None}

    async def set_status(self, list_ref: str, task_id: str, status: str) -> dict:
        lst = await self.resolve_list(list_ref)
        current = await self._get_raw(lst, task_id)
        patch = {"status": status}
        raw = await self.graph.request("PATCH", self._tasks_path(lst["id"], task_id), json=patch)
        task = self.format_task(self._after_patch(current, patch, raw), lst)
        out: dict[str, Any] = {"task": task, "previous_status": current.get("status")}
        if status == "completed" and current.get("recurrence") and task["status"] != "completed":
            was_due = tu.parse_graph_due_date(current.get("dueDateTime"), self.tz)
            out["note"] = (
                "This task repeats. To Do saved this occurrence"
                f"{f' (due {was_due.isoformat()})' if was_due else ''} as a separate completed task, and this "
                f"task ID now holds the series, moved to its next occurrence (due {task['due_date']}). "
                "To undo, reopen the completed copy (list_tasks with status 'completed')."
            )
            out["series_next_due"] = task["due_date"]
        return out

    async def delete_task(self, list_ref: str, task_id: str) -> dict:
        lst = await self.resolve_list(list_ref)
        current = await self._get_raw(lst, task_id)
        await self.graph.request("DELETE", self._tasks_path(lst["id"], task_id))
        return {"deleted_task": current.get("title"), "id": task_id, "list": lst.get("displayName")}

    # ---------------------------------------------------------------- checklist

    async def add_checklist_item(self, list_ref: str, task_id: str, text: str, checked: bool = False) -> dict:
        text = (text or "").strip()
        if not text:
            raise TodoError("A checklist item needs text.")
        lst = await self.resolve_list(list_ref)
        raw = await self.graph.request(
            "POST", f"{self._tasks_path(lst['id'], task_id)}/checklistItems",
            json={"displayName": text, "isChecked": bool(checked)},
        )
        return {"added": self.format_item(raw or {}), "task_id": task_id}

    async def update_checklist_item(
        self, list_ref: str, task_id: str, item_id: str, text: str | None = None, checked: bool | None = None
    ) -> dict:
        patch: dict[str, Any] = {}
        if text is not None:
            if not text.strip():
                raise TodoError("Checklist text can't be empty.")
            patch["displayName"] = text.strip()
        if checked is not None:
            patch["isChecked"] = bool(checked)
        if not patch:
            raise TodoError("Nothing to change: give text and/or checked.")
        lst = await self.resolve_list(list_ref)
        raw = await self.graph.request(
            "PATCH", f"{self._tasks_path(lst['id'], task_id)}/checklistItems/{seg(item_id)}", json=patch
        )
        return {"updated": self.format_item(raw or {}), "task_id": task_id}

    async def remove_checklist_item(self, list_ref: str, task_id: str, item_id: str) -> dict:
        lst = await self.resolve_list(list_ref)
        path = f"{self._tasks_path(lst['id'], task_id)}/checklistItems/{seg(item_id)}"
        current = await self.graph.get(path)
        await self.graph.request("DELETE", path)
        return {"removed": current.get("displayName"), "id": item_id, "task_id": task_id}
