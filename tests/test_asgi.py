"""Tests for pyfuse.integrations.asgi — lifespan and middleware."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from pyfuse.integrations.asgi import PyfuseLifespan, PyfuseMiddleware, pyfuse_lifespan


# ---------------------------------------------------------------------------
# PyfuseLifespan
# ---------------------------------------------------------------------------


class TestPyfuseLifespan:
    async def test_connects_and_disconnects(self) -> None:
        """PyfuseLifespan calls connect() on enter and disconnect() on exit."""
        lifespan = PyfuseLifespan(url="local://localhost:9999")

        with patch("pyfuse.connect") as mock_connect, \
             patch("pyfuse.disconnect", new_callable=AsyncMock) as mock_disconnect:
            async with lifespan(app=MagicMock()):
                mock_connect.assert_called_once_with("local://localhost:9999")
                mock_disconnect.assert_not_called()

            mock_disconnect.assert_awaited_once()

    async def test_forwards_backend_kwargs(self) -> None:
        """Extra kwargs are forwarded to connect()."""
        lifespan = PyfuseLifespan(url="redis://host:6379", db=2)

        with patch("pyfuse.connect") as mock_connect, \
             patch("pyfuse.disconnect", new_callable=AsyncMock):
            async with lifespan(app=MagicMock()):
                mock_connect.assert_called_once_with("redis://host:6379", db=2)

    async def test_disconnects_on_exception(self) -> None:
        """Backend is disconnected even if the app raises during lifespan."""
        lifespan = PyfuseLifespan(url="local://localhost:9999")

        with patch("pyfuse.connect"), \
             patch("pyfuse.disconnect", new_callable=AsyncMock) as mock_disconnect:
            with pytest.raises(RuntimeError, match="boom"):
                async with lifespan(app=MagicMock()):
                    raise RuntimeError("boom")

            mock_disconnect.assert_awaited_once()

    async def test_url_from_env(self) -> None:
        """When url is None, connect() is called with None (uses env var)."""
        lifespan = PyfuseLifespan()

        with patch("pyfuse.connect") as mock_connect, \
             patch("pyfuse.disconnect", new_callable=AsyncMock):
            async with lifespan(app=MagicMock()):
                mock_connect.assert_called_once_with(None)


# ---------------------------------------------------------------------------
# pyfuse_lifespan() factory
# ---------------------------------------------------------------------------


class TestPyfuseLifespanFactory:
    def test_returns_lifespan_instance(self) -> None:
        result = pyfuse_lifespan("redis://localhost:6379")
        assert isinstance(result, PyfuseLifespan)

    def test_stores_url_and_kwargs(self) -> None:
        result = pyfuse_lifespan("redis://host:6379", db=3)
        assert result._url == "redis://host:6379"
        assert result._backend_kwargs == {"db": 3}

    async def test_factory_works_as_lifespan(self) -> None:
        """Ensure the factory result works when used as lifespan."""
        lifespan = pyfuse_lifespan("local://localhost:9999")

        with patch("pyfuse.connect") as mock_connect, \
             patch("pyfuse.disconnect", new_callable=AsyncMock):
            async with lifespan(app=MagicMock()):
                mock_connect.assert_called_once_with("local://localhost:9999")


# ---------------------------------------------------------------------------
# PyfuseMiddleware
# ---------------------------------------------------------------------------


class TestPyfuseMiddleware:
    async def test_forwards_non_lifespan_events(self) -> None:
        """HTTP/WS scopes are forwarded to the wrapped app without interception."""
        inner_app = AsyncMock()
        mw = PyfuseMiddleware(inner_app, url="redis://localhost:6379")

        scope = {"type": "http", "path": "/test"}
        receive = AsyncMock()
        send = AsyncMock()

        await mw(scope, receive, send)

        inner_app.assert_awaited_once_with(scope, receive, send)

    async def test_lifespan_startup_shutdown(self) -> None:
        """Middleware connects on startup and disconnects on shutdown."""
        inner_app = AsyncMock()
        mw = PyfuseMiddleware(inner_app, url="local://localhost:9999")

        # Build a mock receive that returns startup then shutdown
        messages = iter([
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ])
        receive = AsyncMock(side_effect=lambda: next(messages))
        sent: list[dict[str, str]] = []
        send = AsyncMock(side_effect=lambda msg: sent.append(msg))

        # Inner app simulates sending startup.complete then shutdown.complete
        async def fake_inner_app(scope: dict[str, object], recv: object, snd: object) -> None:
            await snd({"type": "lifespan.startup.complete"})  # type: ignore[misc]
            await recv()  # type: ignore[misc]  # consume shutdown
            await snd({"type": "lifespan.shutdown.complete"})  # type: ignore[misc]

        mw.app = fake_inner_app  # type: ignore[assignment]

        with patch("pyfuse.connect") as mock_connect, \
             patch("pyfuse.disconnect", new_callable=AsyncMock) as mock_disconnect:
            await mw({"type": "lifespan"}, receive, send)
            mock_connect.assert_called_once_with("local://localhost:9999")
            mock_disconnect.assert_awaited_once()

        # Verify the lifecycle messages were forwarded
        assert {"type": "lifespan.startup.complete"} in sent
        assert {"type": "lifespan.shutdown.complete"} in sent

    async def test_stores_url_and_kwargs(self) -> None:
        inner_app = AsyncMock()
        mw = PyfuseMiddleware(inner_app, url="redis://host:6379", db=5)
        assert mw._url == "redis://host:6379"
        assert mw._backend_kwargs == {"db": 5}
