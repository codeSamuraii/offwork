from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Self

_OBJECT_SENTINEL = "__pyfuse_obj__"


class _TaskEncoder(json.JSONEncoder):
    """JSON encoder that serializes arbitrary objects via class name + __dict__."""

    def default(self, o: object) -> Any:
        if hasattr(o, "__dict__"):
            state = o.__dict__
        elif hasattr(type(o), "__slots__"):
            all_slots: set[str] = set()
            for klass in type(o).__mro__:
                all_slots.update(getattr(klass, "__slots__", ()))
            all_slots -= {"__weakref__", "__dict__"}
            state = {
                slot: getattr(o, slot)
                for slot in sorted(all_slots)
                if hasattr(o, slot)
            }
        else:
            raise TypeError(
                f"Object of type {type(o).__name__} is not JSON serializable"
            )
        return {
            _OBJECT_SENTINEL: {
                "class": type(o).__name__,
                "state": state,
            }
        }


def _reconstruct_object(info: dict[str, Any], namespace: dict[str, Any]) -> Any:
    """Rebuild a single serialized object from its sentinel payload."""
    cls = namespace.get(info["class"])
    if cls is None:
        return {_OBJECT_SENTINEL: info}
    obj = cls.__new__(cls)
    state = {k: _resolve(v, namespace) for k, v in info.get("state", {}).items()}
    if hasattr(obj, "__dict__"):
        obj.__dict__.update(state)
    else:
        for key, val in state.items():
            object.__setattr__(obj, key, val)
    return obj


def _resolve(value: Any, namespace: dict[str, Any]) -> Any:
    """Recursively resolve serialized object sentinels using *namespace*."""
    if isinstance(value, list):
        return [_resolve(v, namespace) for v in value]
    if not isinstance(value, dict):
        return value
    if len(value) == 1 and _OBJECT_SENTINEL in value:
        return _reconstruct_object(value[_OBJECT_SENTINEL], namespace)
    return {k: _resolve(v, namespace) for k, v in value.items()}


def resolve_args(
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    namespace: dict[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Resolve serialized object sentinels in task arguments.

    Called by the worker after reconstructing the function's namespace,
    so that class instances passed as arguments can be rebuilt.
    """
    return (
        tuple(_resolve(a, namespace) for a in args),
        {k: _resolve(v, namespace) for k, v in kwargs.items()},
    )


@dataclass(frozen=True)
class Task:
    """A serializable envelope for remote function execution.

    Bundles the serialized dependency graph with the target function
    name and its arguments, so the consumer side needs zero knowledge
    of pyfuse internals to dispatch work.
    """

    graph_json: str
    function_name: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    timeout: float | None = None
    retries: int = 0
    retry_delay: float = 1.0

    def to_json(self) -> str:
        """Serialize the task envelope to a JSON string."""
        d: dict[str, Any] = {
            "id": self.task_id,
            "graph": self.graph_json,
            "function": self.function_name,
            "args": list(self.args),
            "kwargs": self.kwargs,
        }
        if self.timeout is not None:
            d["timeout"] = self.timeout
        if self.retries:
            d["retries"] = self.retries
        if self.retry_delay != 1.0:
            d["retry_delay"] = self.retry_delay
        return json.dumps(d, cls=_TaskEncoder)

    @classmethod
    def from_json(cls, json_str: str | bytes) -> Self:
        """Deserialize a task envelope from a JSON string."""
        data = json.loads(json_str)
        return cls(
            graph_json=data["graph"],
            function_name=data["function"],
            args=tuple(data.get("args", ())),
            kwargs=data.get("kwargs", {}),
            task_id=data.get("id", uuid.uuid4().hex[:12]),
            timeout=data.get("timeout"),
            retries=data.get("retries", 0),
            retry_delay=data.get("retry_delay", 1.0),
        )
