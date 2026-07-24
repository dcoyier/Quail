"""Worker subprocess entry: one execute request over NDJSON stdin/stdout."""

from __future__ import annotations

import sys
from typing import Any, TextIO

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
        _write(
            output_stream,
            {
                "type": "result",
                "ok": False,
                "message": str(error),
                "printed_output": "",
            },
        )
    except (QuailError, QuailSyntaxError, QuailRuntimeError) as error:
        _write(
            output_stream,
            {
                "type": "result",
                "ok": False,
                "message": str(error),
                "printed_output": "",
            },
        )
    except Exception as error:  # noqa: BLE001 - sandbox boundary
        _write(
            output_stream,
            {
                "type": "result",
                "ok": False,
                "message": f"{type(error).__name__}: {error}",
                "printed_output": "",
            },
        )
    finally:
        reset_host_call(token)
        if pending is not None:
            del pending


def _write(stream: TextIO, message: dict[str, Any]) -> None:
    stream.write(dumps_message(message) + "\n")
    stream.flush()


if __name__ == "__main__":
    serve()
