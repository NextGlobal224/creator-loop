"""Unittest suite with durable per-test timing and hang stacks, without retries."""

from __future__ import annotations

import argparse
import faulthandler
import json
import sys
import time
import unittest
from pathlib import Path
from typing import TextIO
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
    ):
        faulthandler.dump_traceback_later(60, repeat=True, file=traces)
        try:
            suite = unittest.defaultTestLoader.discover("tests", pattern=args.pattern)
            runner = unittest.TextTestRunner(
                verbosity=1,
                resultclass=lambda *values: TimedResult(*values, progress=progress),
            )
            result = runner.run(suite)
            return 0 if result.wasSuccessful() else 1
        finally:
            faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    raise SystemExit(main())
