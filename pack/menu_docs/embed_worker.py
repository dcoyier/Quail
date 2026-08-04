#!/usr/bin/env python3
"""Thin alias for the shared Ollama embedding worker."""
from __future__ import annotations

import runpy
from pathlib import Path

runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "tools" / "embed_worker.py"),
    run_name="__main__",
)
