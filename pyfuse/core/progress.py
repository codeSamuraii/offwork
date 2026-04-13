import contextvars
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self, overload


@dataclass(frozen=True)
class ProgressInfo:
    """Progress information reported by a running task.

    Returned by :meth:`Result.progress`.
    """

    current: float = 0
    total: float | None = None
    message: str | None = None

    @property
    def percent(self) -> float | None:
        """Return completion percentage, or ``None`` if *total* is unknown."""
        if self.total is not None and self.total > 0:
            return self.current / self.total * 100
        return None

    def to_json(self) -> str:
        d: dict[str, Any] = {"current": self.current}
        if self.total is not None:
            d["total"] = self.total
        if self.message is not None:
            d["message"] = self.message
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str | bytes) -> Self:
        data = json.loads(raw)
        return cls(
            current=data["current"],
            total=data.get("total"),
            message=data.get("message"),
        )


_progress_callback: contextvars.ContextVar[
    Callable[[float, float | None, str | None], None] | None
] = contextvars.ContextVar("pyfuse_progress", default=None)


@overload
def progress(percent: float, /, *, message: str | None = None) -> None: ...


@overload
def progress(current: int, total: int, /, *, message: str | None = None) -> None: ...


def progress(
    _value: float,
    _total: int | None = None,
    /,
    *,
    message: str | None = None,
) -> None:
    """Report task progress from within a running function.

    Call this inside a ``@trace``-decorated function to report progress
    to the client.  When called outside a worker, this is a silent no-op.

    Accepts either a percentage or a current/total pair::

        progress(75.0)                          # 75 % complete
        progress(75.0, message="loading model") # 75 % with message
        progress(3, 10)                         # 3 of 10 (30 %)
        progress(3, 10, message="step 3")       # 3 of 10 with message
    """
    cb = _progress_callback.get(None)
    if cb is None:
        return

    if _total is not None:
        # current / total form
        cb(_value, _total, message)
    else:
        # percent form — normalise to current/100
        cb(_value, 100, message)
