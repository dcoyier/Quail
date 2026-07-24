"""Host-side worker client: spawn subprocess and answer ApiCalls."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quail.analysis.bindings import (
    EncodedBinding,
    bindings_from_payload,
    bindings_to_payload,
)
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
    changed_bindings: dict[str, EncodedBinding] = field(default_factory=dict)
    deleted_bindings: tuple[str, ...] = ()


def run_worker_script(
    code: str,
    *,
    on_api_call: Callable[[ApiCall], Any],
    bindings: Mapping[str, EncodedBinding] | None = None,
) -> WorkerResult:
    """Spawn the worker, feed execute, handle api_call, return printed_output."""

    if not isinstance(code, str):
        raise QuailSyntaxError("code must be a string")

    site_packages = _site_packages_path()
    bootstrap = (
        "import sys;"
        f"sys.path.insert(0, {site_packages.as_posix()!r});"
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
        execute = {
            "type": "execute",
            "code": code,
            "bindings": bindings_to_payload(bindings or {}),
        }
        process.stdin.write(dumps_message(execute) + "\n")
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
                changed = bindings_from_payload(message.get("changed_bindings") or {})
                deleted_raw = message.get("deleted_bindings") or []
                if not isinstance(deleted_raw, list):
                    raise QuailRuntimeError("worker deleted_bindings must be a list")
                deleted = tuple(str(name) for name in deleted_raw)
                return WorkerResult(
                    printed_output=printed,
                    changed_bindings=changed,
                    deleted_bindings=deleted,
                )
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


def _site_packages_path() -> Path:
    """Locate the host interpreter's site-packages so -S workers can import re2."""

    try:
        import re2

        return Path(re2.__file__).resolve().parents[1]
    except Exception:  # noqa: BLE001 - fall back before google-re2 is installed
        for entry in sys.path:
            candidate = Path(entry)
            if candidate.name == "site-packages" and candidate.is_dir():
                return candidate
        return (
            Path(sys.prefix)
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
