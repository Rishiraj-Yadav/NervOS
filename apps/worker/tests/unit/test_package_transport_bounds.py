"""Deterministic bounds for the Worker's package-host transport (ADR 0038).

A cap violation is not cosmetic: the aggregate output budget kills the contained process,
and the Worker's exception path then terminates the whole containment group. These tests
pin the transport half of that contract without needing a platform kernel.
"""

# pyright: basic

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from nervos_core.application.sandbox import MAX_TOTAL_OUTPUT_BYTES
from nervos_package_host.wire import MAX_FRAME_BYTES, HostProtocolError
from nervos_worker.package_execution import _discard_stderr, _OutputBudget, _read_frame


def _stream_reader(*chunks: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


def _frame(payload: bytes) -> bytes:
    return str(len(payload)).encode() + b"\n" + payload


def test_the_aggregate_output_budget_refuses_anything_beyond_its_bound() -> None:
    budget = _OutputBudget()
    budget.consume(MAX_TOTAL_OUTPUT_BYTES)
    with pytest.raises(HostProtocolError, match="aggregate"):
        budget.consume(1)


@pytest.mark.anyio
async def test_a_frame_longer_than_the_frame_bound_is_refused() -> None:
    declared = str(MAX_FRAME_BYTES + 1).encode() + b"\n"
    with pytest.raises(HostProtocolError, match="frame length is invalid"):
        await _read_frame(_stream_reader(declared), _OutputBudget())


@pytest.mark.anyio
async def test_the_frame_reader_accounts_header_and_payload_against_the_budget() -> None:
    budget = _OutputBudget()
    budget.consume(MAX_TOTAL_OUTPUT_BYTES - 1)
    with pytest.raises(HostProtocolError, match="aggregate"):
        await _read_frame(_stream_reader(_frame(b"hello")), budget)


@pytest.mark.anyio
async def test_a_stderr_cap_violation_kills_the_contained_process() -> None:
    killed: list[bool] = []
    process = SimpleNamespace(
        stderr=_stream_reader(b"s" * (MAX_TOTAL_OUTPUT_BYTES + 16)),
        kill=lambda: killed.append(True),
    )
    budget = _OutputBudget()
    await _discard_stderr(process, budget)  # type: ignore[arg-type]
    assert killed == [True]
    assert budget.used > MAX_TOTAL_OUTPUT_BYTES


@pytest.mark.anyio
async def test_stderr_within_the_bound_leaves_the_process_running() -> None:
    killed: list[bool] = []
    process = SimpleNamespace(
        stderr=_stream_reader(b"s" * (MAX_TOTAL_OUTPUT_BYTES // 2)),
        kill=lambda: killed.append(True),
    )
    budget = _OutputBudget()
    await _discard_stderr(process, budget)  # type: ignore[arg-type]
    assert killed == []
    assert budget.used == MAX_TOTAL_OUTPUT_BYTES // 2
