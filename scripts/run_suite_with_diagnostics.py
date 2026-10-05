"""Unittest suite with durable per-test timing and hang stacks, without retries."""

from __future__ import annotations

import argparse
import faulthandler
import json
import math
import sys
import threading
import time
import traceback
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, TextIO
from uuid import uuid4


class TimedResult(unittest.TextTestResult):
    def __init__(self, stream, descriptions, verbosity, *, progress: TextIO):
        super().__init__(stream, descriptions, verbosity)
        self.progress = progress
        self.started: dict[str, float] = {}

    def _record(self, test: unittest.TestCase, event: str) -> None:
        now = time.monotonic()
        if event == "start":
            self.started[test.id()] = now
        self.progress.write(
            json.dumps(
                {
                    "test": test.id(),
                    "event": event,
                    "monotonic": now,
                    "elapsed_seconds": now - self.started[test.id()],
                }
            )
            + "\n"
        )
        self.progress.flush()

    def startTest(self, test):
        self._record(test, "start")
        super().startTest(test)

    def stopTest(self, test):
        self._record(test, "stop")
        super().stopTest(test)


@contextmanager
def periodic_python_stacks(traces: TextIO, *, interval: float = 60) -> Iterator[None]:
    """Retain Python frame/code references instead of walking raw frame pointers.

    CPython's native faulthandler watchdog can race freed code metadata:
    https://github.com/python/cpython/issues/158200
    This reporter needs the GIL; process timeout/progress and fatal diagnostics
    still cover a native stall. It never retries a test or changes its deadline.
    """
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("Positive reporting interval required")
    stopped = threading.Event()
    failures: list[BaseException] = []

    def report() -> None:
        try:
            while not stopped.wait(interval):
                frames = sys._current_frames()  # strong references retain code metadata
                frame = None
                try:
                    traces.write("Periodic Python stacks (requires GIL):\n")
                    for ident, frame in frames.items():
                        traces.write(f"Thread {ident}:\n")
                        traceback.print_stack(frame, limit=100, file=traces)
                    traces.flush()
                finally:
                    frames.clear()
                    frame = None  # release the last frame too, between reports
        except BaseException as exc:
            failures.append(exc)

    reporter = threading.Thread(target=report, name="suite-stack-reporter", daemon=True)
    reporter.start()
    try:
        yield
    finally:
        stopped.set()
        reporter.join(timeout=2)
        if reporter.is_alive():
            raise RuntimeError("Stack reporter has not stopped; preserve diagnostics")
        if failures:
            raise RuntimeError(
                "Periodic stack reporting failed; preserve diagnostics"
            ) from failures[0]


def main() -> int:
    # Match `python -m unittest`: script execution otherwise puts scripts/
    # rather than the checkout root on sys.path. Tests also import app/scripts.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    parser = argparse.ArgumentParser()
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()
    diagnostics = Path(".local-test-logs") / f"suite-diagnostics-{uuid4().hex}"
    diagnostics.mkdir(parents=True)
    print(f"Suite diagnostics: {diagnostics}", flush=True)
    with (
        (diagnostics / "progress.jsonl").open("x", encoding="utf-8") as progress,
        (diagnostics / "tracebacks.log").open("x", encoding="utf-8") as traces,
        periodic_python_stacks(traces),
    ):
        faulthandler.enable(file=traces, all_threads=True)
        try:
            suite = unittest.defaultTestLoader.discover("tests", pattern=args.pattern)
            runner = unittest.TextTestRunner(
                verbosity=1,
                resultclass=lambda *values: TimedResult(*values, progress=progress),
            )
            result = runner.run(suite)
            return 0 if result.wasSuccessful() else 1
        finally:
            faulthandler.disable()


if __name__ == "__main__":
    raise SystemExit(main())
