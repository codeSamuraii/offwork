import json

import pytest

from pyfuse.core.task import Task, _TaskEncoder, _resolve


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


class TestSlotsObjectSerialization:
    """Test serialization of objects with __slots__."""

    def test_slots_object_roundtrip(self) -> None:
        class Point:
            __slots__ = ("x", "y")

            def __init__(self, x: int, y: int) -> None:
                self.x = x
                self.y = y

        p = Point(3, 4)
        encoded = json.loads(json.dumps(p, cls=_TaskEncoder))
        restored = _resolve(encoded, {"Point": Point})
        assert isinstance(restored, Point)
        assert restored.x == 3
        assert restored.y == 4

    def test_slots_inherited(self) -> None:
        class Base:
            __slots__ = ("a",)

        class Child(Base):
            __slots__ = ("b",)

            def __init__(self, a: int, b: int) -> None:
                self.a = a
                self.b = b

        c = Child(1, 2)
        encoded = json.loads(json.dumps(c, cls=_TaskEncoder))
        restored = _resolve(encoded, {"Child": Child})
        assert isinstance(restored, Child)
        assert restored.a == 1
        assert restored.b == 2

    def test_slots_uninitialized(self) -> None:
        class Partial:
            __slots__ = ("x", "y")

            def __init__(self, x: int) -> None:
                self.x = x
                # y intentionally not set

        p = Partial(5)
        encoded = json.loads(json.dumps(p, cls=_TaskEncoder))
        restored = _resolve(encoded, {"Partial": Partial})
        assert isinstance(restored, Partial)
        assert restored.x == 5
        assert not hasattr(restored, "y")

    def test_slots_with_dict(self) -> None:
        """Class with __slots__ including __dict__ uses __dict__ path."""
        class Mixed:
            __slots__ = ("__dict__", "x")

            def __init__(self, x: int, y: int) -> None:
                self.x = x
                self.y = y  # stored in __dict__

        m = Mixed(1, 2)
        encoded = json.loads(json.dumps(m, cls=_TaskEncoder))
        restored = _resolve(encoded, {"Mixed": Mixed})
        assert isinstance(restored, Mixed)
        # __dict__-based objects use the __dict__ path
        assert restored.y == 2
