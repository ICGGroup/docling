"""Verbose logging control for Docling pipeline instrumentation.

Set DOCLING_VERBOSE=true (default) to enable detailed timing and diagnostic
output.  Set DOCLING_VERBOSE=false to silence it (e.g. in production).
"""

import os
import time

VERBOSE: bool = os.environ.get("DOCLING_VERBOSE", "true").lower() not in (
    "false",
    "0",
    "no",
)


def vprint(msg: str) -> None:
    """Print *msg* only when verbose mode is enabled."""
    if VERBOSE:
        print(msg)


class vtimer:
    """Context manager that prints elapsed time when verbose mode is enabled.

    Usage::

        with vtimer("layout_model load"):
            model = load_layout_model()
        # prints: === layout_model load: 1234.5ms ===
    """

    __slots__ = ("_label", "_start")

    def __init__(self, label: str) -> None:
        self._label = label
        self._start = 0.0

    def __enter__(self) -> "vtimer":
        self._start = time.time()
        if VERBOSE:
            print(f"=== {self._label}: BEGIN ===")
        return self

    def __exit__(self, *exc_info: object) -> None:
        elapsed_ms = (time.time() - self._start) * 1000
        if VERBOSE:
            print(f"=== {self._label}: END {elapsed_ms:.1f}ms ===")
