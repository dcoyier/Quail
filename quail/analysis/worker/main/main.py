"""Worker subprocess entry: one execute request over NDJSON stdin/stdout."""

from __future__ import annotations

import resource
import signal
import sys
from contextlib import contextmanager
from typing import Any, Iterator, TextIO

from quail.analysis.bindings import (
    RESERVED_NAMES,
    BindingEncodingError,
    bindings_from_payload,
    bindings_to_payload,
    decode_namespace,
    encode_binding_value,
)
from quail.analysis.errors import QuailError, QuailRuntimeError, QuailSyntaxError
from quail.analysis.worker.protocol import dumps_message, loads_message
from quail.analysis.worker.runtime import (
    HostEndpoint,
    PrintBuffer,
    build_namespace,
    host_call_from_endpoint,
    reset_host_call,
    set_host_call,
)
from quail.analysis.worker.sandbox import validate_quail_code


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
    """Read one execute message, run code, write result or error."""

    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    line = input_stream.readline()
    if not line:
        _write(output_stream, {"type": "result", "ok": False, "message": "empty input"})
        return
    try:
        message = loads_message(line)
    except Exception as error:  # noqa: BLE001 - protocol boundary
        _write(
            output_stream,
            {"type": "result", "ok": False, "message": f"invalid protocol: {error}"},
        )
        return
    if message.get("type") != "execute":
        _write(
            output_stream,
            {"type": "result", "ok": False, "message": "expected execute message"},
        )
        return
    code = message.get("code")
    if not isinstance(code, str):
        _write(
            output_stream,
            {"type": "result", "ok": False, "message": "execute.code must be a string"},
        )
        return
    try:
        initial_bindings = bindings_from_payload(message.get("bindings") or {})
    except Exception as error:  # noqa: BLE001 - protocol boundary
        _write(
            output_stream,
            {"type": "result", "ok": False, "message": f"invalid bindings: {error}"},
        )
        return
    try:
        cpu_seconds, max_memory_bytes = _parse_limits(message.get("limits"))
    except QuailRuntimeError as error:
        _write(
            output_stream,
            {"type": "result", "ok": False, "message": str(error)},
        )
        return

    pending: dict[str, Any] | None = None

    def send_and_wait(payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal pending
        _write(output_stream, payload)
        while True:
            response_line = input_stream.readline()
            if not response_line:
                raise QuailRuntimeError("Host closed the protocol stream")
            response = loads_message(response_line)
            if response.get("type") == "api_result":
                return response
            if response.get("type") == "result":
                pending = response
                raise QuailRuntimeError("Host ended the exec during an api_call")

    prints = PrintBuffer()
    endpoint = HostEndpoint(send_and_wait)
    token = set_host_call(host_call_from_endpoint(endpoint))
    try:
        program = validate_quail_code(code)
        namespace = build_namespace(endpoint, prints)
        namespace.update(decode_namespace(initial_bindings))
        compiled = compile(program.tree, "<quail_exec>", "exec")
        _apply_memory_ceiling(max_memory_bytes)
        with _cpu_timeout_guard(cpu_seconds):
            exec(compiled, namespace, namespace)  # noqa: S102 - intentional worker sandbox

        changed: dict[str, Any] = {}
        for name in sorted(program.assigned_names):
            if name not in namespace or name in RESERVED_NAMES:
                continue
            binding = encode_binding_value(namespace[name])
            if initial_bindings.get(name) != binding:
                changed[name] = binding
        deleted = sorted(
            name
            for name in program.deleted_names
            if name in initial_bindings and name not in namespace
        )
        _write(
            output_stream,
            {
                "type": "result",
                "ok": True,
                "printed_output": prints.text,
                "changed_bindings": bindings_to_payload(changed),
                "deleted_bindings": deleted,
            },
        )
    except BindingEncodingError as error:
        _write(output_stream, _failure_result(error, message=str(error)))
    except (QuailError, QuailSyntaxError, QuailRuntimeError) as error:
        _write(output_stream, _failure_result(error, message=str(error)))
    except Exception as error:  # noqa: BLE001 - sandbox boundary
        _write(
            output_stream,
            _failure_result(error, message=f"{type(error).__name__}: {error}"),
        )
    finally:
        reset_host_call(token)
        if pending is not None:
            del pending


def _write(stream: TextIO, message: dict[str, Any]) -> None:
    stream.write(dumps_message(message) + "\n")
    stream.flush()


def _failure_result(error: BaseException, *, message: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "result",
        "ok": False,
        "exception_type": type(error).__name__,
        "message": message,
        "printed_output": "",
    }
    if isinstance(error, QuailRuntimeError) and error.repair_hint:
        payload["repair_hint"] = error.repair_hint
    return payload


def _parse_limits(raw: object) -> tuple[int, int]:
    if raw is None:
        return 15, 256 * 1024 * 1024
    if not isinstance(raw, dict):
        raise QuailRuntimeError("execute.limits must be an object")
    cpu_seconds = raw.get("cpu_seconds", 15)
    if isinstance(cpu_seconds, bool) or not isinstance(cpu_seconds, int) or cpu_seconds <= 0:
        raise QuailRuntimeError("execute.limits.cpu_seconds must be a positive integer")
    max_memory_bytes = raw.get("max_memory_bytes", 256 * 1024 * 1024)
    if (
        isinstance(max_memory_bytes, bool)
        or not isinstance(max_memory_bytes, int)
        or max_memory_bytes <= 0
    ):
        raise QuailRuntimeError("execute.limits.max_memory_bytes must be a positive integer")
    return cpu_seconds, max_memory_bytes


def _apply_memory_ceiling(max_memory_bytes: int) -> None:
    """Best-effort address-space ceiling (enforced on Linux; host also watches RSS)."""

    if not hasattr(resource, "RLIMIT_AS"):
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (max_memory_bytes, max_memory_bytes))
    except (ValueError, OSError):
        return


@contextmanager
def _cpu_timeout_guard(cpu_seconds: int) -> Iterator[None]:
    """Apply RLIMIT_CPU and turn SIGXCPU into a QuailRuntimeError."""

    cpu_signal = getattr(signal, "SIGXCPU", None)
    if cpu_signal is None or not hasattr(resource, "RLIMIT_CPU"):
        yield
        return

    def _on_cpu(_signum: int, _frame: object) -> None:
        raise QuailRuntimeError(f"quail_exec exceeded its {cpu_seconds:g}s CPU-time limit")

    previous = signal.signal(cpu_signal, _on_cpu)
    soft = cpu_seconds
    hard = max(cpu_seconds + 1, soft)
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (soft, hard))
    except (ValueError, OSError):
        signal.signal(cpu_signal, previous)
        yield
        return
    try:
        yield
    finally:
        signal.signal(cpu_signal, previous)


if __name__ == "__main__":
    serve()
