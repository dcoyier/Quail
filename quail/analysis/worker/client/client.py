"""Host-side worker client: spawn subprocess and answer ApiCalls."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quail.analysis.errors import QuailRuntimeError, QuailSyntaxError
from quail.analysis.worker.protocol import (
    ApiCall,
    decode_api_call,
    dumps_message,
    encode_value,
    loads_message,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class WorkerResult:
    printed_output: str


def run_worker_script(
    code: str,
    *,
    on_api_call: Callable[[ApiCall], Any],
) -> WorkerResult:
    """Spawn the worker, feed execute, handle api_call, return printed_output."""

    if not isinstance(code, str):
        raise QuailSyntaxError("code must be a string")

    bootstrap = (
        "import sys;"
        f"sys.path.insert(0, {_REPO_ROOT.as_posix()!r});"
        "from quail.analysis.worker.main.main import serve;"
        "serve()"
    )
    env = {
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONHASHSEED": "0",
        "PYTHONUTF8": "1",
        "TZ": "UTC",
    }
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "-u", "-c", bootstrap],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    try:
        process.stdin.write(dumps_message({"type": "execute", "code": code}) + "\n")
        process.stdin.flush()

        while True:
            line = process.stdout.readline()
            if not line:
                stderr = process.stderr.read()
                raise QuailRuntimeError(
                    f"Worker exited without a result: {stderr.strip() or 'no stderr'}"
                )
            message = loads_message(line)
            message_type = message.get("type")
            if message_type == "api_call":
                call = decode_api_call(message)
                try:
                    result = on_api_call(call)
                    response = {
                        "type": "api_result",
                        "id": call.id,
                        "ok": True,
                        "result": encode_value(result),
                    }
                except Exception as error:  # noqa: BLE001 - RPC boundary
                    response = {
                        "type": "api_result",
                        "id": call.id,
                        "ok": False,
                        "message": f"{type(error).__name__}: {error}",
                        "result": encode_value(None),
                    }
                process.stdin.write(dumps_message(response) + "\n")
                process.stdin.flush()
                continue
            if message_type == "result":
                if not message.get("ok"):
                    raise QuailRuntimeError(str(message.get("message") or "worker failed"))
                printed = message.get("printed_output", "")
                if not isinstance(printed, str):
                    raise QuailRuntimeError("worker printed_output must be a string")
                return WorkerResult(printed_output=printed)
            raise QuailRuntimeError(f"Unexpected worker message type: {message_type!r}")
    finally:
        try:
            process.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass
        try:
            process.wait(timeout=2)
        except Exception:  # noqa: BLE001
            pass
