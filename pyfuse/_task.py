from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Self


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

    def to_json(self) -> str:
        """Serialize the task envelope to a JSON string."""
        return json.dumps({
            "id": self.task_id,
            "graph": self.graph_json,
            "function": self.function_name,
            "args": list(self.args),
            "kwargs": self.kwargs,
        })

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
        )
