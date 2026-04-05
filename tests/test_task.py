from __future__ import annotations

import json

import pytest

from pyfuse._task import Task


class TestTaskCreation:
    def test_auto_generated_id(self) -> None:
        task = Task(graph_json="{}", function_name="f")
        assert len(task.task_id) == 12

    def test_explicit_id(self) -> None:
        task = Task(graph_json="{}", function_name="f", task_id="abc")
        assert task.task_id == "abc"

    def test_default_args_kwargs(self) -> None:
        task = Task(graph_json="{}", function_name="f")
        assert task.args == ()
        assert task.kwargs == {}

    def test_frozen(self) -> None:
        task = Task(graph_json="{}", function_name="f")
        with pytest.raises(AttributeError):
            task.function_name = "g"  # type: ignore[misc]

    def test_unique_ids(self) -> None:
        t1 = Task(graph_json="{}", function_name="f")
        t2 = Task(graph_json="{}", function_name="f")
        assert t1.task_id != t2.task_id


class TestTaskSerialization:
    def test_roundtrip(self) -> None:
        original = Task(
            graph_json='{"objects": {}}',
            function_name="m.func",
            args=(1, "two", 3.0),
            kwargs={"key": "value"},
            task_id="test123",
        )
        restored = Task.from_json(original.to_json())
        assert restored.graph_json == original.graph_json
        assert restored.function_name == original.function_name
        assert restored.args == original.args
        assert restored.kwargs == original.kwargs
        assert restored.task_id == original.task_id

    def test_to_json_structure(self) -> None:
        task = Task(
            graph_json='{"g": 1}',
            function_name="f",
            args=(1, 2),
            kwargs={"k": "v"},
            task_id="tid",
        )
        data = json.loads(task.to_json())
        assert data["id"] == "tid"
        assert data["graph"] == '{"g": 1}'
        assert data["function"] == "f"
        assert data["args"] == [1, 2]
        assert data["kwargs"] == {"k": "v"}

    def test_from_json_bytes(self) -> None:
        task = Task(graph_json="{}", function_name="f", task_id="x")
        raw = task.to_json().encode()
        restored = Task.from_json(raw)
        assert restored.task_id == "x"

    def test_from_json_missing_optional_fields(self) -> None:
        data = json.dumps({"graph": "{}", "function": "f"})
        task = Task.from_json(data)
        assert task.args == ()
        assert task.kwargs == {}
        assert len(task.task_id) == 12  # auto-generated

    def test_empty_args(self) -> None:
        task = Task(graph_json="{}", function_name="f")
        restored = Task.from_json(task.to_json())
        assert restored.args == ()
        assert restored.kwargs == {}
