"""Host-side worker client: spawn subprocess and answer ApiCalls."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from quail.analysis.bindings import (
    EncodedBinding,
    bindings_from_payload,
    bindings_to_payload,
)
from quail.analysis.errors import QuailRuntimeError, QuailSyntaxError, rehydrate_quail_error
from quail.analysis.limits import (
    STANDARD_LIMITS,
    ExecLimits,
    cpu_timeout_error,
    memory_limit_error,
    wall_timeout_error,
)
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
    limits: ExecLimits | None = None,
    rss_sampler: Callable[[int], int | None] | None = None,
    cancel_event: threading.Event | None = None,
) -> WorkerResult:
    """Spawn the worker, feed execute, handle api_call, return printed_output."""

    if not isinstance(code, str):
        raise QuailSyntaxError("code must be a string")
    active = limits if limits is not None else STANDARD_LIMITS
    sample_rss = rss_sampler or _rss_bytes
    host_cancel = cancel_event

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

    done = threading.Event()
    wall_exceeded = threading.Event()
    memory_exceeded = threading.Event()

    def _signal_cancel() -> None:
        if host_cancel is not None:
            host_cancel.set()

    def _watch_wall() -> None:
        if done.wait(active.wall_seconds):
            return
        wall_exceeded.set()
        _signal_cancel()
        _kill_process_group(process)

    def _watch_memory() -> None:
        while not done.wait(0.05):
            if process.poll() is not None:
                return
            rss = sample_rss(process.pid)
            if rss is not None and rss > active.max_memory_bytes:
                memory_exceeded.set()
                _signal_cancel()
                _kill_process_group(process)
                return

    wall_thread = threading.Thread(target=_watch_wall, name="quail-wall", daemon=True)
    memory_thread = threading.Thread(target=_watch_memory, name="quail-rss", daemon=True)
    wall_thread.start()
    memory_thread.start()

    try:
        execute = {
            "type": "execute",
            "code": code,
            "bindings": bindings_to_payload(bindings or {}),
            "limits": {
                "cpu_seconds": active.cpu_seconds,
                "max_memory_bytes": active.max_memory_bytes,
            },
        }
        process.stdin.write(dumps_message(execute) + "\n")
        process.stdin.flush()

        while True:
            _raise_if_resource_exceeded(
                wall_exceeded=wall_exceeded,
                memory_exceeded=memory_exceeded,
                limits=active,
            )
            line = process.stdout.readline()
            _raise_if_resource_exceeded(
                wall_exceeded=wall_exceeded,
                memory_exceeded=memory_exceeded,
                limits=active,
            )
            if not line:
                stderr = process.stderr.read()
                _raise_if_resource_exceeded(
                    wall_exceeded=wall_exceeded,
                    memory_exceeded=memory_exceeded,
                    limits=active,
                )
                _raise_if_cpu_signal(process, stderr, active)
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
                    _raise_if_resource_exceeded(
                        wall_exceeded=wall_exceeded,
                        memory_exceeded=memory_exceeded,
                        limits=active,
                    )
                    if host_cancel is not None and host_cancel.is_set():
                        raise
                    response = {
                        "type": "api_result",
                        "id": call.id,
                        "ok": False,
                        "exception_type": type(error).__name__,
                        "message": f"{type(error).__name__}: {error}",
                        "result": encode_value(None),
                    }
                    if isinstance(error, QuailRuntimeError) and error.repair_hint:
                        response["repair_hint"] = error.repair_hint
                _raise_if_resource_exceeded(
                    wall_exceeded=wall_exceeded,
                    memory_exceeded=memory_exceeded,
                    limits=active,
                )
                process.stdin.write(dumps_message(response) + "\n")
                process.stdin.flush()
                continue
            if message_type == "result":
                if not message.get("ok"):
                    message_text = str(message.get("message") or "worker failed")
                    if _looks_like_cpu_timeout(message_text):
                        raise cpu_timeout_error(
                            active.cpu_seconds,
                            already_extended=active.already_extended,
                        )
                    raise rehydrate_quail_error(
                        message.get("exception_type"),
                        message_text,
                        message.get("repair_hint"),
                    )
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
        done.set()
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
        wall_thread.join(timeout=1)
        memory_thread.join(timeout=1)


def _raise_if_resource_exceeded(
    *,
    wall_exceeded: threading.Event,
    memory_exceeded: threading.Event,
    limits: ExecLimits,
) -> None:
    if memory_exceeded.is_set():
        raise memory_limit_error(limits.max_memory_bytes)
    if wall_exceeded.is_set():
        raise wall_timeout_error(
            limits.wall_seconds,
            already_extended=limits.already_extended,
        )


def _raise_if_cpu_signal(
    process: subprocess.Popen[str],
    stderr: str,
    limits: ExecLimits,
) -> None:
    if _looks_like_cpu_timeout(stderr):
        raise cpu_timeout_error(
            limits.cpu_seconds,
            already_extended=limits.already_extended,
        )
    returncode = process.returncode
    if returncode is not None and returncode < 0:
        sig = -returncode
        if sig in {getattr(signal, "SIGXCPU", -1), signal.SIGKILL}:
            # Soft CPU limit may surface as SIGXCPU; hard limit as SIGKILL.
            if sig == getattr(signal, "SIGXCPU", -1) or "CPU" in stderr.upper():
                raise cpu_timeout_error(
                    limits.cpu_seconds,
                    already_extended=limits.already_extended,
                )


def _looks_like_cpu_timeout(text: str) -> bool:
    lowered = text.lower()
    return "cpu-time limit" in lowered or "cpu time limit" in lowered


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    try:
        if process.pid is not None:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass


def _rss_bytes(pid: int) -> int | None:
    """Best-effort resident set size for the worker process."""

    status_path = Path(f"/proc/{pid}/status")
    if status_path.is_file():
        try:
            for line in status_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    return int(parts[1]) * 1024
        except (OSError, ValueError, IndexError):
            return None
    try:
        output = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(pid)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        value = output.strip()
        if not value:
            return None
        return int(value) * 1024
    except (OSError, ValueError, subprocess.CalledProcessError):
        return None


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
