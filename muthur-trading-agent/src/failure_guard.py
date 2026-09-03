from __future__ import annotations

import logging
import os
import sys
import time
from typing import Callable


class RapidFailureGuard(logging.Handler):
    """Watches for a burst of ERROR-level log records and forces a hard process exit
    if too many happen too quickly.

    An unbounded in-process retry loop (an MCP subprocess crash-looping, an
    Anthropic API outage retried too eagerly, etc.) is a worse failure mode than a
    hard exit that hands recovery to Kubernetes' own crash-loop backoff.
    """

    def __init__(
        self,
        max_errors: int = 15,
        window_seconds: float = 10.0,
        exit_fn: Callable[[int], None] | None = None,
    ):
        super().__init__(level=logging.ERROR)
        self.max_errors = max_errors
        self.window_seconds = window_seconds
        self._exit_fn = exit_fn or (lambda code: os._exit(code))
        self._timestamps: list[float] = []
        self._tripped = False

    def emit(self, record: logging.LogRecord) -> None:
        if self._tripped:
            return

        now = time.time()
        self._timestamps.append(now)
        self._timestamps = [t for t in self._timestamps if now - t <= self.window_seconds]

        if len(self._timestamps) >= self.max_errors:
            self._tripped = True
            # Bypasses the logging framework deliberately: logging this would
            # recurse back into this same handler (attached to the root logger).
            print(
                f"detected {len(self._timestamps)} error logs within {self.window_seconds}s "
                f"(most recent: {record.getMessage()}) - exiting so Kubernetes applies crash-loop backoff",
                file=sys.stderr,
                flush=True,
            )
            self._exit_fn(1)
