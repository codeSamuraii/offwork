"""Tests for pyfuse._loop — fast event-loop helper."""

import asyncio

from pyfuse._loop import run, _has_uvloop


async def _identity(x: int) -> int:
    return x


async def _gather() -> list[int]:
    return list(await asyncio.gather(_identity(1), _identity(2), _identity(3)))


def test_run_returns_coroutine_result() -> None:
    assert run(_identity(42)) == 42


def test_run_supports_gather() -> None:
    assert run(_gather()) == [1, 2, 3]


def test_has_uvloop_flag_is_bool() -> None:
    assert isinstance(_has_uvloop, bool)
