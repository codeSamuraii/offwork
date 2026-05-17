import json

import pytest

from offwork.core.task import Task, _TaskEncoder, _resolve, resolve_args


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

class TestBytesSerialization:
    """Bytes / bytearray must survive to_json and resolve_args round-trip."""

    @staticmethod
    def _roundtrip(task: Task) -> Task:
        restored = Task.from_json(task.to_json())
        args, kwargs = resolve_args(restored.args, restored.kwargs, {})
        return Task(
            graph_json=restored.graph_json,
            function_name=restored.function_name,
            args=args,
            kwargs=kwargs,
            task_id=restored.task_id,
        )

    def test_bytes_in_args(self) -> None:
        payload = b"\x00\x01\x02 hello \xff"
        task = Task(graph_json="{}", function_name="f", args=(payload,))
        restored = self._roundtrip(task)
        assert restored.args == (payload,)
        assert isinstance(restored.args[0], bytes)

    def test_bytes_in_kwargs(self) -> None:
        task = Task(
            graph_json="{}", function_name="f", kwargs={"blob": b"abc"},
        )
        restored = self._roundtrip(task)
        assert restored.kwargs == {"blob": b"abc"}

    def test_bytes_nested(self) -> None:
        task = Task(
            graph_json="{}",
            function_name="f",
            args=([b"a", b"b"], {"k": b"c"}),
        )
        restored = self._roundtrip(task)
        assert restored.args == ([b"a", b"b"], {"k": b"c"})

    def test_bytearray_roundtrip(self) -> None:
        ba = bytearray(b"mutable")
        task = Task(graph_json="{}", function_name="f", args=(ba,))
        restored = self._roundtrip(task)
        assert restored.args == (ba,)
        assert isinstance(restored.args[0], bytearray)

    def test_empty_bytes(self) -> None:
        task = Task(graph_json="{}", function_name="f", args=(b"",))
        restored = self._roundtrip(task)
        assert restored.args == (b"",)

    def test_large_bytes(self) -> None:
        blob = bytes(range(256)) * 1024  # 256 KiB
        task = Task(graph_json="{}", function_name="f", args=(blob,))
        restored = self._roundtrip(task)
        assert restored.args[0] == blob


class TestBuiltinTypeSerialization:
    """Common stdlib types must survive to_json + resolve_args."""

    @staticmethod
    def _roundtrip(value: object) -> object:
        task = Task(graph_json="{}", function_name="f", args=(value,))
        restored = Task.from_json(task.to_json())
        args, _ = resolve_args(restored.args, restored.kwargs, {})
        return args[0]

    def test_datetime(self) -> None:
        import datetime
        v = datetime.datetime(2026, 4, 26, 14, 30, 5, 123456)
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, datetime.datetime)

    def test_datetime_with_tz(self) -> None:
        import datetime
        v = datetime.datetime(2026, 4, 26, tzinfo=datetime.timezone.utc)
        out = self._roundtrip(v)
        assert out == v
        assert out.tzinfo is not None

    def test_date(self) -> None:
        import datetime
        v = datetime.date(2026, 4, 26)
        out = self._roundtrip(v)
        assert out == v
        # date is roundtripped as date, not datetime
        assert type(out) is datetime.date

    def test_time(self) -> None:
        import datetime
        v = datetime.time(14, 30, 5)
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, datetime.time)

    def test_timedelta(self) -> None:
        import datetime
        v = datetime.timedelta(days=2, hours=3, microseconds=42)
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, datetime.timedelta)

    def test_decimal(self) -> None:
        from decimal import Decimal
        v = Decimal("3.14159265358979323846")
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, Decimal)

    def test_uuid(self) -> None:
        import uuid
        v = uuid.uuid4()
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, uuid.UUID)

    def test_complex(self) -> None:
        v = complex(2.0, -3.5)
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, complex)

    def test_set(self) -> None:
        v = {1, 2, 3, "x"}
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, set)

    def test_frozenset(self) -> None:
        v = frozenset([1, 2, 3])
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, frozenset)

    def test_pathlib_path(self) -> None:
        import pathlib
        v = pathlib.Path("/tmp/data/file.txt")
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, pathlib.PurePath)

    def test_pure_posix_path(self) -> None:
        import pathlib
        v = pathlib.PurePosixPath("/var/log/app")
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, pathlib.PurePosixPath)

    def test_nested_in_kwargs(self) -> None:
        import datetime
        from decimal import Decimal
        task = Task(
            graph_json="{}",
            function_name="f",
            kwargs={
                "when": datetime.datetime(2026, 1, 1),
                "amount": Decimal("19.99"),
                "tags": {"a", "b"},
            },
        )
        restored = Task.from_json(task.to_json())
        _, kwargs = resolve_args(restored.args, restored.kwargs, {})
        assert kwargs["when"] == datetime.datetime(2026, 1, 1)
        assert kwargs["amount"] == Decimal("19.99")
        assert kwargs["tags"] == {"a", "b"}

    def test_set_inside_list(self) -> None:
        v = [{1, 2}, {3, 4}]
        out = self._roundtrip(v)
        assert out == v
        assert all(isinstance(s, set) for s in out)


class TestExtendedTypeSerialization:
    """Tuples, NamedTuples, enums, ranges, fractions, collections, ipaddress."""

    @staticmethod
    def _roundtrip(value: object, namespace: dict[str, object] | None = None) -> object:
        task = Task(graph_json="{}", function_name="f", args=(value,))
        restored = Task.from_json(task.to_json())
        args, _ = resolve_args(restored.args, restored.kwargs, namespace or {})
        return args[0]

    def test_tuple_preserves_type(self) -> None:
        v = (1, 2, "three")
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, tuple)

    def test_nested_tuple(self) -> None:
        v = (1, (2, 3), [4, (5, 6)])
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, tuple)
        assert isinstance(out[1], tuple)
        assert isinstance(out[2][1], tuple)

    def test_empty_tuple(self) -> None:
        out = self._roundtrip(())
        assert out == ()
        assert isinstance(out, tuple)

    def test_namedtuple_roundtrip(self) -> None:
        from collections import namedtuple
        Point = namedtuple("Point", ["x", "y"])
        p = Point(3, 4)
        out = self._roundtrip(p, {"Point": Point})
        assert isinstance(out, Point)
        assert out.x == 3 and out.y == 4

    def test_namedtuple_falls_back_to_tuple(self) -> None:
        from collections import namedtuple
        Pair = namedtuple("Pair", ["a", "b"])
        out = self._roundtrip(Pair(1, 2))  # no namespace -> plain tuple
        assert out == (1, 2)
        assert type(out) is tuple

    def test_dict_with_int_keys(self) -> None:
        v = {1: "a", 2: "b"}
        out = self._roundtrip(v)
        assert out == v
        assert all(isinstance(k, int) for k in out)

    def test_dict_with_tuple_keys(self) -> None:
        v = {(1, 2): "x", (3, 4): "y"}
        out = self._roundtrip(v)
        assert out == v

    def test_dict_string_keys_unchanged(self) -> None:
        v = {"a": 1, "b": 2}
        task = Task(graph_json="{}", function_name="f", args=(v,))
        # JSON should still use the natural object form for str-keyed dicts
        raw = task.to_json()
        assert "__offwork_dict__" not in raw
        out = self._roundtrip(v)
        assert out == v

    def test_enum(self) -> None:
        import enum

        class Color(enum.Enum):
            RED = 1
            GREEN = 2

        out = self._roundtrip(Color.RED, {"Color": Color})
        assert out is Color.RED

    def test_int_enum(self) -> None:
        import enum

        class Mode(enum.IntEnum):
            ON = 1
            OFF = 0

        out = self._roundtrip(Mode.ON, {"Mode": Mode})
        assert out is Mode.ON
        assert isinstance(out, Mode)

    def test_enum_unknown_class_falls_back_to_value(self) -> None:
        import enum

        class Color(enum.Enum):
            RED = 1

        out = self._roundtrip(Color.RED)
        assert out == 1

    def test_range(self) -> None:
        v = range(0, 10, 2)
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, range)

    def test_fraction(self) -> None:
        from fractions import Fraction
        v = Fraction(22, 7)
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, Fraction)

    def test_counter(self) -> None:
        from collections import Counter
        v = Counter({"a": 3, "b": 2, "c": 1})
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, Counter)

    def test_ordered_dict(self) -> None:
        from collections import OrderedDict
        v = OrderedDict([("a", 1), ("b", 2), ("c", 3)])
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, OrderedDict)
        assert list(out.keys()) == ["a", "b", "c"]

    def test_defaultdict(self) -> None:
        from collections import defaultdict
        v = defaultdict(list)
        v["a"].append(1)
        v["b"].append(2)
        out = self._roundtrip(v)
        assert isinstance(out, defaultdict)
        assert dict(out) == {"a": [1], "b": [2]}
        assert out.default_factory is list

    def test_deque(self) -> None:
        from collections import deque
        v = deque([1, 2, 3], maxlen=5)
        out = self._roundtrip(v)
        assert isinstance(out, deque)
        assert list(out) == [1, 2, 3]
        assert out.maxlen == 5

    def test_ipv4_address(self) -> None:
        import ipaddress
        v = ipaddress.IPv4Address("192.168.1.1")
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, ipaddress.IPv4Address)

    def test_ipv6_network(self) -> None:
        import ipaddress
        v = ipaddress.IPv6Network("2001:db8::/32")
        out = self._roundtrip(v)
        assert out == v
        assert isinstance(out, ipaddress.IPv6Network)

    def test_memoryview(self) -> None:
        v = memoryview(b"hello world")
        out = self._roundtrip(v)
        assert isinstance(out, memoryview)
        assert bytes(out) == b"hello world"

    def test_pickle_fallback(self) -> None:
        import re
        # re.Pattern has neither __dict__ nor __slots__, but pickles fine.
        v = re.compile(r"\d+", re.IGNORECASE)
        out = self._roundtrip(v)
        assert isinstance(out, re.Pattern)
        assert out.pattern == r"\d+"
        assert out.flags == v.flags

    def test_unserializable_raises(self) -> None:
        # generators are not picklable and have no state extractable
        gen = (x for x in range(3))
        with pytest.raises(TypeError):
            self._roundtrip(gen)


class TestRoundTripCombined:
    """End-to-end tests combining many types in one envelope."""

    def test_kitchen_sink(self) -> None:
        import datetime
        from collections import Counter, OrderedDict, deque
        from decimal import Decimal
        from fractions import Fraction

        payload: dict[str, object] = {
            "tup": (1, (2, 3)),
            "set": {1, 2, 3},
            "bytes": b"\x00\xff",
            "dt": datetime.datetime(2026, 4, 27, 12),
            "amount": Decimal("9.99"),
            "frac": Fraction(1, 3),
            "rng": range(5),
            "counter": Counter("aabbbc"),
            "od": OrderedDict([("a", 1)]),
            "dq": deque([10, 20]),
            "intkeys": {1: "x", 2: "y"},
        }
        task = Task(graph_json="{}", function_name="f", kwargs=payload)
        restored = Task.from_json(task.to_json())
        _, kwargs = resolve_args(restored.args, restored.kwargs, {})

        assert kwargs["tup"] == (1, (2, 3))
        assert isinstance(kwargs["tup"], tuple)
        assert kwargs["set"] == {1, 2, 3}
        assert kwargs["bytes"] == b"\x00\xff"
        assert kwargs["dt"] == datetime.datetime(2026, 4, 27, 12)
        assert kwargs["amount"] == Decimal("9.99")
        assert kwargs["frac"] == Fraction(1, 3)
        assert kwargs["rng"] == range(5)
        assert kwargs["counter"] == Counter("aabbbc")
        assert kwargs["od"] == OrderedDict([("a", 1)])
        assert list(kwargs["dq"]) == [10, 20]
        assert kwargs["intkeys"] == {1: "x", 2: "y"}
