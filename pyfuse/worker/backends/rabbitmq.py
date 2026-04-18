"""RabbitMQ backend for multi-machine task distribution.

Uses ``aio-pika`` (async AMQP 0-9-1 client) for task dispatch, result
routing, heartbeats, cancellation, and progress.

Tasks are dispatched via a durable queue.  Per-task results use dedicated
queues with message TTL.  Heartbeats, cancellation flags, and progress
data are stored in single-message queues (``x-max-length: 1``) that
behave like key-value slots.  Result notifications use a fanout exchange.

URL scheme: ``amqp://`` or ``amqps://``  (e.g. ``amqp://guest:guest@localhost/``)
"""
import time
import asyncio
import contextlib
from typing import Any
from collections.abc import AsyncIterator

try:
    import aio_pika
except ImportError:
    raise ImportError(
        "aio-pika package is required for RabbitMQBackend. "
        "Install it with: pip install aio-pika"
    ) from None

from pyfuse.worker.backends.base import Backend


class RabbitMQBackend(Backend):
    """RabbitMQ-backed transport using ``aio-pika``.

    Parameters
    ----------
    url
        AMQP connection URL (e.g. ``amqp://guest:guest@localhost/``).
    task_queue
        Name of the durable task queue.
    result_ttl
        Seconds before result messages expire.
    """

    TASK_QUEUE = "pyfuse.tasks"
    RESULT_PREFIX = "pyfuse.result."
    HEARTBEAT_PREFIX = "pyfuse.hb."
    CANCEL_PREFIX = "pyfuse.cancel."
    PROGRESS_PREFIX = "pyfuse.progress."
    NOTIFY_EXCHANGE = "pyfuse.notify"

    DEFAULT_RESULT_TTL = 300   # seconds
    HEARTBEAT_TTL = 30
    CANCEL_TTL = 3600
    PROGRESS_TTL = 300

    def __init__(
        self,
        url: str = "amqp://localhost",
        *,
        task_queue: str | None = None,
        result_ttl: int | None = None,
    ) -> None:
        self._url = url
        self._task_queue_name = task_queue or self.TASK_QUEUE
        self._result_ttl = result_ttl or self.DEFAULT_RESULT_TTL
        self._connection: Any = None
        self._channel: Any = None
        self._lock = asyncio.Lock()

    # -- connection management --------------------------------------------------

    async def _get_channel(self) -> Any:
        """Return the shared channel, creating connection if needed."""
        async with self._lock:
            if self._connection is None or self._connection.is_closed:
                self._connection = await aio_pika.connect_robust(self._url)
                self._channel = None
            if self._channel is None or self._channel.is_closed:
                self._channel = await self._connection.channel()
            return self._channel

    async def _new_channel(self) -> Any:
        """Create a dedicated channel for long-running operations."""
        async with self._lock:
            if self._connection is None or self._connection.is_closed:
                self._connection = await aio_pika.connect_robust(self._url)
                self._channel = None
        return await self._connection.channel()

    # -- internal helpers -------------------------------------------------------

    @staticmethod
    def _kv_args(ttl_s: int) -> dict[str, int]:
        """Queue arguments for a single-message key-value slot."""
        return {
            "x-message-ttl": ttl_s * 1000,
            "x-max-length": 1,
            "x-expires": ttl_s * 2 * 1000,
        }

    def _result_args(self) -> dict[str, int]:
        """Queue arguments for a per-task result queue."""
        return {
            "x-message-ttl": self._result_ttl * 1000,
            "x-expires": self._result_ttl * 2 * 1000,
        }

    async def _kv_put(
        self, prefix: str, task_id: str, value: str, ttl_s: int,
    ) -> None:
        """Write to a per-task KV queue (``x-max-length: 1`` overwrites)."""
        channel = await self._get_channel()
        name = f"{prefix}{task_id}"
        await channel.declare_queue(name, arguments=self._kv_args(ttl_s))
        await channel.default_exchange.publish(
            aio_pika.Message(value.encode()),
            routing_key=name,
        )

    async def _kv_get(
        self, prefix: str, task_id: str, ttl_s: int, *, peek: bool = False,
    ) -> str | None:
        """Read from a per-task KV queue.

        When *peek* is ``True`` the message is nack'd back so future
        reads still see it (used for cancellation flags).  Otherwise the
        message is consumed.
        """
        channel = await self._get_channel()
        name = f"{prefix}{task_id}"
        queue = await channel.declare_queue(
            name, arguments=self._kv_args(ttl_s),
        )
        msg = await queue.get(fail=False, no_ack=not peek)
        if msg is None:
            return None
        if peek:
            await msg.nack(requeue=True)
        raw: str = msg.body.decode()
        return raw

    # -- Backend interface: tasks -----------------------------------------------

    async def submit(self, task_json: str) -> None:
        channel = await self._get_channel()
        await channel.declare_queue(self._task_queue_name, durable=True)
        await channel.default_exchange.publish(
            aio_pika.Message(
                task_json.encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=self._task_queue_name,
        )

    async def listen(self) -> AsyncIterator[str]:
        channel = await self._new_channel()
        try:
            await channel.set_qos(prefetch_count=1)
            queue = await channel.declare_queue(
                self._task_queue_name, durable=True,
            )
            async with queue.iterator() as qi:
                async for message in qi:
                    async with message.process():
                        yield message.body.decode()
        finally:
            with contextlib.suppress(Exception):
                await channel.close()

    # -- Backend interface: results ---------------------------------------------

    async def send_result(self, task_id: str, result_json: str) -> None:
        channel = await self._get_channel()
        name = f"{self.RESULT_PREFIX}{task_id}"
        await channel.declare_queue(name, arguments=self._result_args())
        await channel.default_exchange.publish(
            aio_pika.Message(result_json.encode()),
            routing_key=name,
        )

    async def get_result(self, task_id: str, timeout: float | None = None) -> str:
        channel = await self._new_channel()
        try:
            name = f"{self.RESULT_PREFIX}{task_id}"
            queue = await channel.declare_queue(
                name, arguments=self._result_args(),
            )
            future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

            async def _on_message(msg: Any) -> None:
                await msg.ack()
                if not future.done():
                    future.set_result(msg.body.decode())

            tag = await queue.consume(_on_message)
            try:
                if timeout is not None:
                    try:
                        return await asyncio.wait_for(future, timeout=timeout)
                    except asyncio.TimeoutError:
                        raise TimeoutError(
                            f"Timed out waiting for result of task {task_id}"
                        ) from None
                return await future
            finally:
                with contextlib.suppress(Exception):
                    await queue.cancel(tag)
        finally:
            with contextlib.suppress(Exception):
                await channel.close()

    async def try_get_result(self, task_id: str) -> str | None:
        channel = await self._get_channel()
        name = f"{self.RESULT_PREFIX}{task_id}"
        queue = await channel.declare_queue(
            name, arguments=self._result_args(),
        )
        msg = await queue.get(fail=False)
        if msg is None:
            return None
        await msg.ack()
        raw: str = msg.body.decode()
        return raw

    # -- Heartbeat -------------------------------------------------------------

    async def send_heartbeat(self, task_id: str) -> None:
        await self._kv_put(
            self.HEARTBEAT_PREFIX, task_id,
            str(time.time()), self.HEARTBEAT_TTL,
        )

    async def get_heartbeat(self, task_id: str) -> float | None:
        raw = await self._kv_get(
            self.HEARTBEAT_PREFIX, task_id, self.HEARTBEAT_TTL,
        )
        return float(raw) if raw is not None else None

    # -- Cancellation ----------------------------------------------------------

    async def cancel_task(self, task_id: str) -> None:
        await self._kv_put(
            self.CANCEL_PREFIX, task_id, "1", self.CANCEL_TTL,
        )

    async def is_cancelled(self, task_id: str) -> bool:
        raw = await self._kv_get(
            self.CANCEL_PREFIX, task_id, self.CANCEL_TTL, peek=True,
        )
        return raw is not None

    # -- Progress --------------------------------------------------------------

    async def send_progress(self, task_id: str, progress_json: str) -> None:
        await self._kv_put(
            self.PROGRESS_PREFIX, task_id, progress_json, self.PROGRESS_TTL,
        )

    async def get_progress(self, task_id: str) -> str | None:
        return await self._kv_get(
            self.PROGRESS_PREFIX, task_id, self.PROGRESS_TTL,
        )

    # -- Result notifications --------------------------------------------------

    async def notify_result(self, task_id: str) -> None:
        channel = await self._get_channel()
        exchange = await channel.declare_exchange(
            self.NOTIFY_EXCHANGE, aio_pika.ExchangeType.FANOUT,
        )
        await exchange.publish(
            aio_pika.Message(task_id.encode()),
            routing_key="",
        )

    async def subscribe_results(self) -> AsyncIterator[str]:
        channel = await self._new_channel()
        try:
            exchange = await channel.declare_exchange(
                self.NOTIFY_EXCHANGE, aio_pika.ExchangeType.FANOUT,
            )
            queue = await channel.declare_queue(exclusive=True)
            await queue.bind(exchange)
            async with queue.iterator() as qi:
                async for message in qi:
                    async with message.process():
                        yield message.body.decode()
        finally:
            with contextlib.suppress(Exception):
                await channel.close()

    # -- Lifecycle -------------------------------------------------------------

    async def close(self) -> None:
        if self._channel is not None:
            with contextlib.suppress(Exception):
                await self._channel.close()
            self._channel = None
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.close()
            self._connection = None
