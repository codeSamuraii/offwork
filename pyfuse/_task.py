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
        return json.dumps(d)

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
