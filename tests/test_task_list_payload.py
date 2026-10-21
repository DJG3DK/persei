"""The task list the dashboard polls every few seconds stays small: goal
previews, no plan (agent/routers/tasks.py, 2026-10-21: it had reached ~900KB
and never finished loading on a slow connection)."""

from agent.routers import tasks


def test_a_long_goal_is_a_preview_and_says_so():
    goal = "fix the alert\n" + "x" * 5000
    entry = tasks._list_entry({"task_id": "t1", "goal": goal, "status": "completed"})
    assert entry["goal"] == goal[:tasks.GOAL_PREVIEW_CHARS]
    assert entry["goal_truncated"] is True
    assert entry["task_id"] == "t1" and entry["status"] == "completed"


def test_a_short_goal_is_whole_and_unmarked():
    entry = tasks._list_entry({"task_id": "t1", "goal": "short"})
    assert entry == {"task_id": "t1", "goal": "short"}


def test_the_plan_is_left_to_the_task_page():
    entry = tasks._list_entry({"task_id": "t1", "goal": "g", "latest_todos": [{"content": "x"}] * 50})
    assert "latest_todos" not in entry


def test_the_record_itself_is_not_changed():
    record = {"task_id": "t1", "goal": "y" * 5000, "latest_todos": []}
    tasks._list_entry(record)
    assert len(record["goal"]) == 5000 and "latest_todos" in record
