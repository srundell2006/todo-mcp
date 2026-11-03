from __future__ import annotations

from datetime import datetime, timezone

import pytest
from conftest import sign_in

from todo_mcp.recurrence import RecurrenceSpec
from todo_mcp.todo import TodoError, TodoService

NOW = datetime(2026, 11, 3, 20, 0, tzinfo=timezone.utc)  # Tue 2026-11-03 14:00 Central


@pytest.fixture
async def todo(stack, config):
    await sign_in(stack)
    return TodoService(stack.graph, config.tz, now=lambda: NOW)


def due(d: str) -> dict:
    return {"dateTime": f"{d}T06:00:00.0000000", "timeZone": "UTC"}  # Central midnight, as To Do stores it


async def test_list_resolution(todo, fake):
    groceries = fake.add_list("Groceries")
    assert (await todo.resolve_list("groceries"))["id"] == groceries["id"]
    assert (await todo.resolve_list(groceries["id"]))["id"] == groceries["id"]
    assert (await todo.resolve_list("default"))["wellknownListName"] == "defaultList"
    with pytest.raises(TodoError, match="No list named 'Nope'"):
        await todo.resolve_list("Nope")
    fake.add_list("groceries")
    todo._invalidate_lists()
    with pytest.raises(TodoError, match="More than one list"):
        await todo.resolve_list("Groceries")


async def test_list_crud_and_built_in_guard(todo, fake):
    created = (await todo.create_list("Temp"))["created"]
    assert created["name"] == "Temp" and created["built_in"] is None
    renamed = await todo.rename_list("Temp", "Temp 2")
    assert renamed["list"]["name"] == "Temp 2"
    with pytest.raises(TodoError, match="built-in"):
        await todo.rename_list("Tasks", "Other")
    with pytest.raises(TodoError, match="built-in"):
        await todo.delete_list("Flagged emails")
    assert (await todo.delete_list("Temp 2"))["deleted_list"] == "Temp 2"
    assert created["id"] not in fake.lists


async def test_create_task_with_every_field(todo, fake):
    lst = fake.add_list("Home")
    out = await todo.create_task(
        "Home", "Pay water bill", notes="Account 1234", due_date="2026-11-10", reminder="2026-11-09T18:00",
        importance="high", recurrence=RecurrenceSpec(frequency="monthly"), categories=["Bills", "Bills", " "],
        checklist=["Find bill", "Pay online"],
    )
    task = out["created"]
    stored = fake.tasks[lst["id"]][task["id"]]
    assert stored["dueDateTime"] == {"dateTime": "2026-11-10T00:00:00.0000000", "timeZone": "Central Standard Time"}
    assert stored["reminderDateTime"] == {"dateTime": "2026-11-09T18:00:00.0000000",
                                          "timeZone": "Central Standard Time"}
    assert stored["isReminderOn"] is True
    assert stored["recurrence"]["pattern"] == {"type": "absoluteMonthly", "interval": 1, "dayOfMonth": 10}
    assert stored["categories"] == ["Bills"]
    assert stored["body"] == {"content": "Account 1234", "contentType": "text"}
    assert task["due_date"] == "2026-11-10" and task["reminder"] == "2026-11-09T18:00-06:00"
    assert task["recurrence"]["summary"] == "every month on the 10th"
    assert [i["text"] for i in task["checklist"]] == ["Find bill", "Pay online"]


async def test_recurrence_without_due_date_uses_today(todo, fake):
    fake.add_list("Home")
    out = await todo.create_task("Home", "Water plants", recurrence=RecurrenceSpec(frequency="weekly"))
    assert out["created"]["due_date"] == "2026-11-03"
    assert out["created"]["recurrence"]["summary"] == "every week on Tuesday"
    assert "today" in out["notes"][0]


async def test_list_tasks_filters_across_lists(todo, fake):
    a, b = fake.add_list("A"), fake.add_list("B")
    fake.add_task(a["id"], title="overdue", dueDateTime=due("2026-11-01"))
    fake.add_task(a["id"], title="today", dueDateTime=due("2026-11-03"), importance="high")
    fake.add_task(a["id"], title="done", status="completed", dueDateTime=due("2026-11-02"),
                  completedDateTime={"dateTime": "2026-11-02T18:00:00.0000000", "timeZone": "UTC"})
    fake.add_task(b["id"], title="later", dueDateTime=due("2026-11-20"), categories=["Work"])
    fake.add_task(b["id"], title="someday")
    fake.add_task(b["id"], title="next week", dueDateTime=due("2026-11-09"))

    open_all = await todo.list_tasks()
    assert [t["title"] for t in open_all["tasks"]] == ["overdue", "today", "next week", "later", "someday"]
    assert open_all["tasks"][0]["overdue"] is True and open_all["tasks"][1]["overdue"] is False
    assert {t["list"] for t in open_all["tasks"]} == {"A", "B"}

    assert [t["title"] for t in (await todo.list_tasks(overdue=True))["tasks"]] == ["overdue"]
    assert [t["title"] for t in (await todo.list_tasks(due_on="today"))["tasks"]] == ["today"]
    week = await todo.list_tasks(due_from="2026-11-03", due_to="2026-11-09")
    assert [t["title"] for t in week["tasks"]] == ["today", "next week"]
    assert [t["title"] for t in (await todo.list_tasks(no_due_date=True))["tasks"]] == ["someday"]
    assert [t["title"] for t in (await todo.list_tasks(category="work"))["tasks"]] == ["later"]
    assert [t["title"] for t in (await todo.list_tasks(importance="high"))["tasks"]] == ["today"]
    done = await todo.list_tasks(status="completed")
    assert [t["title"] for t in done["tasks"]] == ["done"]
    assert done["tasks"][0]["completed_at"] == "2026-11-02T12:00-06:00"
    assert len((await todo.list_tasks("A", status="all"))["tasks"]) == 3
    limited = await todo.list_tasks(limit=2)
    assert limited["returned"] == 2 and limited["truncated"] and limited["total_matched"] == 5
    # Server-side status filter is used, and pages are followed (page size 2).
    assert any("%24filter=status+ne" in str(r.url) or "$filter=status ne" in str(r.url) for r in fake.requests)


async def test_status_filter_falls_back_when_graph_rejects_it(todo, fake):
    a = fake.add_list("A")
    fake.add_task(a["id"], title="open one")
    fake.add_task(a["id"], title="closed one", status="completed")
    fake.reject_filters = True
    assert [t["title"] for t in (await todo.list_tasks("A"))["tasks"]] == ["open one"]


async def test_date_filter_errors(todo):
    with pytest.raises(TodoError, match="due_from is after due_to"):
        await todo.list_tasks(due_from="2026-11-10", due_to="2026-11-01")
    with pytest.raises(TodoError, match="Couldn't read the date"):
        await todo.list_tasks(due_on="next blursday")


async def test_search_across_lists_including_checklist(todo, fake):
    a, b = fake.add_list("A"), fake.add_list("B")
    fake.add_task(a["id"], title="Buy milk")
    t = fake.add_task(b["id"], title="Party", body={"content": "Remember the cake", "contentType": "text"})
    fake.checklist[t["id"]]["C1"] = {"id": "C1", "displayName": "order MILK shake", "isChecked": False}
    fake.add_task(b["id"], title="Old milk run", status="completed")
    hits = await todo.search_tasks("milk")
    assert [h["title"] for h in hits["tasks"]] == ["Buy milk", "Party"]
    assert hits["tasks"][1]["matched_in"] == ["checklist"]
    assert len((await todo.search_tasks("milk", include_completed=True))["tasks"]) == 3
    assert [h["title"] for h in (await todo.search_tasks("cake party"))["tasks"]] == ["Party"]
    assert (await todo.search_tasks("milk", list_ref="A"))["total_matched"] == 1


async def test_update_complete_reopen_delete(todo, fake):
    lst = fake.add_list("Home")
    task = (await todo.create_task("Home", "Fix fence", due_date="2026-11-04", reminder="2026-11-04T08:00",
                                   recurrence=RecurrenceSpec(frequency="weekly")))["created"]
    tid = task["id"]
    upd = await todo.update_task("Home", tid, title="Fix the fence", importance="low", categories=["Yard"],
                                 notes="Need nails")
    assert upd["updated"]["title"] == "Fix the fence" and upd["updated"]["categories"] == ["Yard"]
    with pytest.raises(TodoError, match="repeats"):
        await todo.update_task("Home", tid, clear_due_date=True)
    cleared = await todo.update_task("Home", tid, clear_due_date=True, clear_recurrence=True, clear_reminder=True,
                                     clear_notes=True)
    assert cleared["updated"]["due_date"] is None and cleared["updated"]["recurrence"] is None
    assert cleared["updated"]["reminder"] is None and cleared["updated"]["notes"] is None
    with pytest.raises(TodoError, match="Nothing to change"):
        await todo.update_task("Home", tid)
    with pytest.raises(TodoError, match="not both"):
        await todo.update_task("Home", tid, due_date="today", clear_due_date=True)

    done = await todo.set_status("Home", tid, "completed")
    assert done["task"]["status"] == "completed" and done["task"]["completed_at"]
    reopened = await todo.set_status("Home", tid, "notStarted")
    assert reopened["task"]["status"] == "notStarted" and reopened["task"]["completed_at"] is None
    assert reopened["previous_status"] == "completed"
    gone = await todo.delete_task("Home", tid)
    assert gone["deleted_task"] == "Fix the fence" and tid not in fake.tasks[lst["id"]]


async def test_checklist_items(todo, fake):
    fake.add_list("Home")
    tid = (await todo.create_task("Home", "Pack"))["created"]["id"]
    item = (await todo.add_checklist_item("Home", tid, "Socks"))["added"]
    assert item == {"id": item["id"], "text": "Socks", "checked": False}
    upd = (await todo.update_checklist_item("Home", tid, item["id"], text="Wool socks", checked=True))["updated"]
    assert upd["text"] == "Wool socks" and upd["checked"] is True
    full = await todo.get_task("Home", tid)
    assert full["checklist"] == [upd]
    assert (await todo.remove_checklist_item("Home", tid, item["id"]))["removed"] == "Wool socks"
    assert (await todo.get_task("Home", tid))["checklist"] == []


async def test_full_path_on_temporary_list(todo, fake):
    """The same sequence the live acceptance test runs."""
    lst = (await todo.create_list("zz-todo-mcp-test"))["created"]
    tid = (await todo.create_task(lst["id"], "probe", due_date="tomorrow"))["created"]["id"]
    await todo.update_task(lst["id"], tid, notes="updated")
    assert (await todo.set_status(lst["id"], tid, "completed"))["task"]["status"] == "completed"
    assert (await todo.set_status(lst["id"], tid, "notStarted"))["task"]["status"] == "notStarted"
    await todo.delete_task(lst["id"], tid)
    await todo.delete_list(lst["id"])
    assert lst["id"] not in fake.lists
