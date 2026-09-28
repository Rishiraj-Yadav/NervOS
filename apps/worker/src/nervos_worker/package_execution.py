"""Worker-owned package-host launch, health check, and package Run execution adapter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast

from nervos_core.application.model_completion import (
    INTERNAL_EXECUTION_ERROR,
    ModelCompletion,
    ModelProviderError,
)
from nervos_core.application.model_completion import (
    ModelRequest as CoreModelRequest,
)
from nervos_core.application.tool_invocations import ClaimHandle
from nervos_core.application.trusted_chat import ChatOutcome
from nervos_core.domain.context import ContextSnapshotData
from nervos_core.domain.runs import ModelUsage, Run
from nervos_package_host.wire import (
    HOST_PROTOCOL_VERSION,
    MAX_FRAME_BYTES,
    HostProtocolError,
    read_frame,
    write_frame,
)

_HOST_START_TIMEOUT = 10.0
_HOST_CANCEL_GRACE = 1.0


class PackageExecutionAdapter:
    """Execute one already-authoritative package Attempt; owns no durable lifecycle state."""

    def __init__(self, package_store: Path) -> None:
        self._store = package_store.expanduser().resolve(strict=False)

    async def run(
        self,
        completion: ModelCompletion,
        run: Run,
        claim: ClaimHandle,
        elapsed_ms: int,
        snapshot: ContextSnapshotData | None = None,
    ) -> ChatOutcome:
        del claim, elapsed_ms
        executable = run.executable
        if (
            executable.package_environment_digest is None
            or executable.package_entrypoint is None
            or executable.host_protocol_version != HOST_PROTOCOL_VERSION
        ):
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR)
        environment = self._store / "environments" / executable.package_environment_digest
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not python.is_file():
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR)
        process = await asyncio.create_subprocess_exec(
            str(python),
            "-m",
            "nervos_package_host",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=environment,
            env=package_host_environment(),
        )
        assert process.stdin is not None and process.stdout is not None
        try:
            hello = await asyncio.wait_for(_read_frame(process.stdout), _HOST_START_TIMEOUT)
            if hello["type"] != "host_hello":
                raise HostProtocolError("package host did not send hello")
            await _write_frame(
                process.stdin,
                _message(
                    "initialize",
                    "initialize",
                    {
                        "entrypoint": executable.package_entrypoint,
                    },
                ),
            )
            ready = await asyncio.wait_for(_read_frame(process.stdout), _HOST_START_TIMEOUT)
            if ready["type"] != "ready":
                raise HostProtocolError("package host did not become ready")
            await _write_frame(
                process.stdin,
                _message(
                    "run_request",
                    "run",
                    {
                        "run_id": str(run.id),
                        "agent_instance_id": str(run.agent_instance_id),
                        "configuration": json.loads(executable.effective_config_json),
                        "context": snapshot.rendered_context if snapshot is not None else None,
                        "input_text": run.input_text,
                    },
                ),
            )
            while True:
                message = await _read_frame(process.stdout)
                message_type = message["type"]
                if message_type == "model_request":
                    model_response = await self._complete_model(completion, message)
                    await _write_frame(
                        process.stdin,
                        _message("model_response", str(message["request_id"]), model_response),
                    )
                    continue
                if message_type == "tool_request":
                    # Tool authorization remains the existing Stage-D loop authority. This adapter
                    # has no independent grant or invocation path, so unintegrated package tool
                    # calls fail closed rather than bypassing that authority.
                    await _write_frame(
                        process.stdin,
                        _message(
                            "tool_response",
                            str(message["request_id"]),
                            {"content": {}, "is_error": True},
                        ),
                    )
                    continue
                if message_type != "run_result":
                    raise HostProtocolError("package host returned an unexpected message")
                payload = cast("dict[str, object]", message["payload"])
                final_message = payload.get("final_message")
                if not isinstance(final_message, str) or not final_message.strip():
                    raise HostProtocolError("package host result has no final message")
                await asyncio.wait_for(process.wait(), _HOST_START_TIMEOUT)
                return ChatOutcome(final_message, "stop", ModelUsage())
        except asyncio.CancelledError:
            await self._cancel(process, process.stdin)
            raise
        except (HostProtocolError, TimeoutError, OSError, json.JSONDecodeError) as error:
            await self._cancel(process, process.stdin)
            raise ModelProviderError(INTERNAL_EXECUTION_ERROR) from error
        finally:
            await _drain_stderr(process)

    async def _complete_model(
        self, completion: ModelCompletion, message: Mapping[str, object]
    ) -> dict[str, object]:
        payload = cast("dict[str, object]", message["payload"])
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, list):
            raise HostProtocolError("model request messages are malformed")
        message_items = cast("list[object]", raw_messages)
        rendered = "\n".join(
            f"{item.get('role', '')}: {item.get('content', '')}"
            for raw in message_items
            if isinstance(raw, dict)
            for item in (cast("dict[str, object]", raw),)
        )
        model = payload.get("model")
        response = await completion.complete(
            CoreModelRequest(
                system_instruction="NervOS package model mediation.",
                user_text=rendered,
                model_name=model if isinstance(model, str) else "default",
                max_output_tokens=run_max_output_tokens(payload),
                timeout_ms=60_000,
            )
        )
        usage = response.usage or ModelUsage()
        return {
            "output_text": response.text,
            "stop_reason": (
                response.finish_reason.value if response.finish_reason is not None else None
            ),
            "usage": {
                key: value
                for key, value in {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "total_tokens": usage.total_tokens,
                }.items()
                if value is not None
            },
        }

    async def _cancel(
        self, process: asyncio.subprocess.Process, stdin: asyncio.StreamWriter
    ) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(Exception):
            await _write_frame(stdin, _message("cancel", "cancel", {}))
            await asyncio.wait_for(process.wait(), _HOST_CANCEL_GRACE)
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), _HOST_CANCEL_GRACE)
        except TimeoutError:
            process.kill()
            await process.wait()


class PackageHostHealthChecker:
    def check(
        self,
        *,
        environment: Path,
        entrypoint: str,
        expected_sdk_api_version: str,
    ) -> None:
        import subprocess

        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        process = subprocess.Popen(
            [
                str(python),
                "-m",
                "nervos_package_host",
                "--health-check",
                "--entrypoint",
                entrypoint,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=environment,
            env=package_host_environment(),
        )
        try:
            assert process.stdout is not None
            hello = read_frame(cast(BinaryIO, process.stdout))
            ready = read_frame(cast(BinaryIO, process.stdout))
            if hello["type"] != "host_hello" or ready["type"] != "ready":
                raise HostProtocolError("health check handshake failed")
            payload_object = ready["payload"]
            if not isinstance(payload_object, dict):
                raise HostProtocolError("health check payload is malformed")
            payload = cast("dict[str, object]", payload_object)
            if payload.get("sdk_api_version") != expected_sdk_api_version:
                raise HostProtocolError("health check SDK version mismatch")
            if payload.get("host_protocol_version") != HOST_PROTOCOL_VERSION:
                raise HostProtocolError("health check protocol mismatch")
            if process.wait(timeout=_HOST_START_TIMEOUT) != 0:
                raise HostProtocolError("health check host exited unsuccessfully")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def package_host_environment() -> dict[str, str]:
    allowed = (
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "HOME",
        "PATH",
        "LANG",
        "LC_ALL",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.update({"PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"})
    return environment


def run_max_output_tokens(payload: Mapping[str, object]) -> int:
    value = payload.get("max_output_tokens")
    if isinstance(value, int) and value > 0:
        return value
    return 1024


def _message(
    message_type: str, request_id: str, payload: Mapping[str, object]
) -> dict[str, object]:
    return {
        "protocol_version": HOST_PROTOCOL_VERSION,
        "type": message_type,
        "request_id": request_id,
        "payload": dict(payload),
    }


async def _read_frame(stream: asyncio.StreamReader) -> dict[str, object]:
    header = await stream.readline()
    if (
        not header.endswith(b"\n")
        or len(header) > 9
        or not header[:-1].isdigit()
        or header[:-1].startswith(b"0")
    ):
        raise HostProtocolError("invalid frame header")
    length = int(header[:-1])
    if length <= 0 or length > MAX_FRAME_BYTES:
        raise HostProtocolError("frame length is invalid")
    payload = await stream.readexactly(length)
    reader = BytesIO(header + payload)
    return read_frame(reader)


async def _write_frame(stream: asyncio.StreamWriter, message: Mapping[str, object]) -> None:
    buffer = BytesIO()
    write_frame(buffer, message)
    stream.write(buffer.getvalue())
    await stream.drain()


async def _drain_stderr(process: asyncio.subprocess.Process) -> None:
    if process.stderr is None:
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.stderr.read(16 * 1024), 0.2)
