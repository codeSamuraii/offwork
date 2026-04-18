"""Tests for pyfuse._json — fast JSON helpers."""

from pyfuse._json import dumps, loads, _has_orjson


def test_dumps_returns_str() -> None:
    result = dumps({"a": 1, "b": [2, 3]})
    assert isinstance(result, str)


def test_dumps_is_compact() -> None:
    result = dumps({"key": "value"})
    assert " " not in result


def test_loads_parses_str() -> None:
    assert loads('{"x":1}') == {"x": 1}


def test_loads_parses_bytes() -> None:
    assert loads(b'{"x":1}') == {"x": 1}


def test_round_trip() -> None:
    obj = {"name": "pyfuse", "values": [1, 2.5, True, None], "nested": {"k": "v"}}
    assert loads(dumps(obj)) == obj


def test_has_orjson_flag_is_bool() -> None:
    assert isinstance(_has_orjson, bool)


def test_dumps_special_types() -> None:
    """Ensure basic types serialize correctly."""
    assert loads(dumps(42)) == 42
    assert loads(dumps("hello")) == "hello"
    assert loads(dumps([1, 2, 3])) == [1, 2, 3]
    assert loads(dumps(True)) is True
    assert loads(dumps(None)) is None
