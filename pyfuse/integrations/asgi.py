"""ASGI integration for pyfuse — works with FastAPI, Starlette, and any ASGI framework.

Provides lifecycle management so ``connect()`` / ``disconnect()`` are handled
automatically when the application starts and stops.

Lifespan (FastAPI / Starlette ≥ 0.20)
--------------------------------------

.. code-block:: python

    from fastapi import FastAPI
    from pyfuse.integrations.asgi import pyfuse_lifespan

    app = FastAPI(lifespan=pyfuse_lifespan("redis://localhost:6379"))

    @app.post("/run")
    async def run_task():
        return {"result": await my_traced_func.run(42)}

If you already have a custom lifespan, compose them::

    from contextlib import asynccontextmanager
    from pyfuse.integrations.asgi import PyfuseLifespan

    pyfuse_ctx = PyfuseLifespan(url="redis://localhost:6379")

    @asynccontextmanager
    async def lifespan(app):
        async with pyfuse_ctx(app):
            # your own startup/shutdown logic here
            yield

    app = FastAPI(lifespan=lifespan)

Middleware (any ASGI app)
-------------------------

If your framework doesn't support lifespan, use the middleware::

    from pyfuse.integrations.asgi import PyfuseMiddleware

    app = PyfuseMiddleware(app, url="redis://localhost:6379")
"""

from __future__ import annotations

import logging
from typing import Any
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator, Callable

logger = logging.getLogger(__name__)

# ASGI type aliases (to avoid depending on asgiref/starlette)
_Scope = dict[str, Any]
_Receive = Callable[..., Any]
_Send = Callable[..., Any]
_ASGIApp = Callable[[_Scope, _Receive, _Send], Any]


class PyfuseLifespan:
    """Async context manager that connects pyfuse on entry and disconnects on exit.

    Compatible with the ASGI lifespan protocol used by FastAPI and Starlette.

    Parameters
    ----------
    url
        Backend URL (e.g. ``"redis://localhost:6379"``).
        When *None*, the ``PYFUSE_BACKEND`` environment variable is used.
    **backend_kwargs
        Extra keyword arguments forwarded to :func:`pyfuse.connect`.

    Usage as a standalone lifespan::

        app = FastAPI(lifespan=PyfuseLifespan(url="redis://localhost:6379"))

    Usage composed with your own lifespan::

        pyfuse_ctx = PyfuseLifespan(url="redis://localhost:6379")

        @asynccontextmanager
        async def lifespan(app):
            async with pyfuse_ctx(app):
                yield

        app = FastAPI(lifespan=lifespan)
    """

    def __init__(
        self,
        url: str | None = None,
        **backend_kwargs: Any,
    ) -> None:
        self._url = url
        self._backend_kwargs = backend_kwargs

    @asynccontextmanager
    async def __call__(self, app: Any) -> AsyncGenerator[None, None]:
        """ASGI lifespan protocol — used as ``FastAPI(lifespan=...)``."""
        import pyfuse

        pyfuse.connect(self._url, **self._backend_kwargs)
        logger.info("pyfuse backend connected (lifespan startup)")
        try:
            yield
        finally:
            await pyfuse.disconnect()
            logger.info("pyfuse backend disconnected (lifespan shutdown)")


def pyfuse_lifespan(
    url: str | None = None,
    **backend_kwargs: Any,
) -> PyfuseLifespan:
    """Create a lifespan context manager for FastAPI / Starlette.

    Parameters
    ----------
    url
        Backend URL (e.g. ``"redis://localhost:6379"``).
        When *None*, the ``PYFUSE_BACKEND`` environment variable is used.
    **backend_kwargs
        Extra keyword arguments forwarded to :func:`pyfuse.connect`.

    Returns
    -------
    PyfuseLifespan
        An async context manager usable as ``FastAPI(lifespan=...)``.

    Example
    -------
    .. code-block:: python

        from fastapi import FastAPI
        from pyfuse.integrations.asgi import pyfuse_lifespan

        app = FastAPI(lifespan=pyfuse_lifespan("redis://localhost:6379"))
    """
    return PyfuseLifespan(url=url, **backend_kwargs)


class PyfuseMiddleware:
    """ASGI middleware that manages the pyfuse backend connection lifecycle.

    Handles ``lifespan`` events to connect on startup and disconnect on
    shutdown, forwarding all other events to the wrapped application.

    Use this when you cannot pass a ``lifespan`` parameter directly
    (e.g. plain Starlette without lifespan support, or when composing
    multiple middleware layers).

    Parameters
    ----------
    app
        The ASGI application to wrap.
    url
        Backend URL (e.g. ``"redis://localhost:6379"``).
        When *None*, the ``PYFUSE_BACKEND`` environment variable is used.
    **backend_kwargs
        Extra keyword arguments forwarded to :func:`pyfuse.connect`.

    Example
    -------
    .. code-block:: python

        from starlette.applications import Starlette
        from pyfuse.integrations.asgi import PyfuseMiddleware

        app = Starlette(routes=[...])
        app = PyfuseMiddleware(app, url="redis://localhost:6379")
    """

    def __init__(
        self,
        app: _ASGIApp,
        url: str | None = None,
        **backend_kwargs: Any,
    ) -> None:
        self.app = app
        self._url = url
        self._backend_kwargs = backend_kwargs

    async def __call__(
        self,
        scope: _Scope,
        receive: _Receive,
        send: _Send,
    ) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    async def _handle_lifespan(
        self,
        scope: _Scope,
        receive: _Receive,
        send: _Send,
    ) -> None:
        """Wrap the inner app's lifespan with pyfuse connect/disconnect."""
        import pyfuse

        async def wrapped_receive() -> dict[str, Any]:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    pyfuse.connect(self._url, **self._backend_kwargs)
                    logger.info("pyfuse backend connected (middleware startup)")
                except Exception:
                    await send({"type": "lifespan.startup.failed", "message": ""})
                    raise
            return message

        async def wrapped_send(message: dict[str, Any]) -> None:
            if message["type"] == "lifespan.shutdown.complete":
                try:
                    await pyfuse.disconnect()
                    logger.info("pyfuse backend disconnected (middleware shutdown)")
                except Exception:
                    logger.warning("pyfuse disconnect failed during shutdown", exc_info=True)
            await send(message)

        await self.app(scope, wrapped_receive, wrapped_send)
