"""Owned launch and health evidence remain complete in deep Windows logs."""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from creator_loop.database import SCHEMA_VERSION
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.update_health import run_health_check


@unittest.skipUnless(sys.platform == "win32", "actual Windows owned process logs")
class OwnedLogPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve(strict=True)
        parent = self.base / ("d" * (235 - len(str(self.base)) - 1))
        parent.mkdir()
        self.logs = parent / ("logs-" + "x" * 19)
        self.assertEqual(len(str(self.logs)), 260)
        self.addCleanup(self._clean_logs)

    def io(self, path):
        return Path("\\\\?\\" + str(path))

    def _clean_logs(self):
        if self.io(self.logs).exists():
            # Only this generated fixture, after owned process contexts close.
            base = self.io(self.base).resolve(strict=True)
            target = self.io(self.logs).resolve(strict=True)
            self.assertTrue(target.is_relative_to(base))
            self.assertEqual(target.relative_to(base), self.logs.relative_to(self.base))
            shutil.rmtree(target)

    def test_deep_ownership_is_complete_before_resume_and_channels_are_preserved(self):
        observed = []

        def before_resume(record):
            parsed = json.loads(
                self.io(self.logs / "ownership.json").read_text(encoding="utf-8")
            )
            self.assertEqual(parsed, record)
            observed.append(parsed)

        with OwnedWindowsProcess(
            Path(sys.executable),
            [
                "-c",
                "import sys; print('owned stdout'); print('owned stderr', file=sys.stderr)",
            ],
            self.logs,
            component_version="private-deep-log-test",
            before_resume=before_resume,
        ) as process:
            self.assertEqual(process.stdout_path, self.logs / "stdout.log")
            outcome = process.wait(15)
            self.assertEqual(outcome.exit_code, 0)
            self.assertFalse(outcome.timed_out)
        self.assertEqual(len(observed), 1)
        self.assertIn(b"owned stdout", self.io(process.stdout_path).read_bytes())
        self.assertIn(b"owned stderr", self.io(process.stderr_path).read_bytes())

    def test_failed_deep_before_resume_retains_evidence_and_child_does_not_run(self):
        marker = self.base / "child-must-not-run.keep"

        def refuse(record):
            self.assertEqual(
                json.loads(self.io(self.logs / "ownership.json").read_text()), record
            )
            raise RuntimeError("Refuse before child execution")

        with self.assertRaises(RuntimeError):
            OwnedWindowsProcess(
                Path(sys.executable),
                [
                    "-c",
                    "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'child ran')",
                    str(marker),
                ],
                self.logs,
                component_version="private-deep-refusal-test",
                before_resume=refuse,
            )
        self.assertFalse(marker.exists())
        self.assertTrue(self.io(self.logs / "ownership.json").is_file())

    def test_deep_health_receipt_and_output_metadata_are_written_and_read(self):
        payload = {
            "health_format": 1,
            "schema_version": SCHEMA_VERSION,
            "query_ok": True,
            "storage_reference": "not_applicable",
        }
        # Synthetic response tests log I/O; existing tests validate real SQLite.
        code = "print(" + repr(json.dumps(payload)) + ")"
        result = run_health_check(
            Path(sys.executable),
            self.base,
            self.logs,
            component_version="private-deep-health-test",
            arguments_prefix=["-c", code],
        )
        self.assertEqual(result, payload)
        receipt = json.loads(
            self.io(self.logs / "result.json").read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["exit_code"], 0)
        self.assertFalse(receipt["timed_out"])
        self.assertEqual(receipt["health_run_format"], 1)
