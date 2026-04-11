from __future__ import annotations

import json
import traceback as tb_mod
from dataclasses import dataclass
from typing import Any, Self

from pyfuse.worker.backends.base import Backend
from pyfuse.core.errors import RemoteError


_MISSING = object()


@dataclass(frozen=True)
class ResultEnvelope:
    """Serializable envelope for a task result (success or error).

    Use the :meth:`success` and :meth:`failure` class methods to create
    instances.
    """

    task_id: str
    status: str  # "ok" or "error"
    result: Any = None
    error_type: str | None = None
    error_message: str | None = None
    error_traceback: str | None = None

    @classmethod
    def success(cls, task_id: str, result: Any) -> Self:
        return cls(task_id=task_id, status="ok", result=result)

    @classmethod
    def failure(cls, task_id: str, exc: BaseException) -> Self:
        return cls(
            task_id=task_id,
            status="error",
            error_type=type(exc).__name__,
            error_message=str(exc),
            error_traceback="".join(tb_mod.format_exception(exc)),
        )

    def to_json(self) -> str:
        d: dict[str, Any] = {
            "task_id": self.task_id,
            "status": self.status,
        }
        if self.status == "ok":
            d["result"] = self.result
        else:
            d["error_type"] = self.error_type
            d["error_message"] = self.error_message
            d["error_traceback"] = self.error_traceback
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str | bytes) -> Self:
        data = json.loads(raw)
        return cls(
            task_id=data["task_id"],
            status=data["status"],
            result=data.get("result"),
            error_type=data.get("error_type"),
            error_message=data.get("error_message"),
            error_traceback=data.get("error_traceback"),
        )


class Result:
    """Future-like handle for a remotely submitted task.

    Returned by ``traced_func.run(...)``.
    """

    def __init__(self, task_id: str, backend: Backend) -> None:
        self._task_id = task_id
        self._backend = backend
        self._envelope: ResultEnvelope | None = None

    @property
    def task_id(self) -> str:
        return self._task_id

    def result(self, timeout: float | None = None) -> Any:
        """Block until the result arrives, then return it.

        Raises :class:`RemoteError` if the remote execution failed.
        """
        if self._envelope is None:
            raw = self._backend.get_result(self._task_id, timeout=timeout)
            self._envelope = ResultEnvelope.from_json(raw)
        if self._envelope.status == "error":
            msg = f"{self._envelope.error_type}: {self._envelope.error_message}"
            if self._envelope.error_traceback:
                msg += f"\n\nRemote traceback:\n{self._envelope.error_traceback}"
            raise RemoteError(msg)
        return self._envelope.result

    @property
    def status(self) -> str:
        """Return ``"pending"``, ``"success"``, or ``"error"``."""
        if self._envelope is None:
            raw = self._backend.try_get_result(self._task_id)
            if raw is None:
                return "pending"
            self._envelope = ResultEnvelope.from_json(raw)
        return "success" if self._envelope.status == "ok" else "error"

    def done(self) -> bool:
        """Non-blocking check whether the result is available."""
        if self._envelope is not None:
            return True
        raw = self._backend.try_get_result(self._task_id)
        if raw is not None:
            self._envelope = ResultEnvelope.from_json(raw)
            return True
        return False

    def __repr__(self) -> str:
        s = "pending" if self._envelope is None else self._envelope.status
        return f"Result(task_id={self._task_id!r}, status={s!r})"
