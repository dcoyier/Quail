"""Worker subprocess package for quail_exec (no DB access)."""

from quail.analysis.worker.client import WorkerResult, run_worker_script

__all__ = [
    "WorkerResult",
    "run_worker_script",
]
