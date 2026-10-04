"""Refuse opt-in probe misconfiguration before touching selected/default data."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class ProductFlowCLITests(unittest.TestCase):
    def _refused(self, extra, expected):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            foreign = base / "default data"
            foreign.mkdir()
            marker = foreign / "keep.bin"
            marker.write_bytes(b"unselected user data stays unchanged")
            work = base / "missing probe work"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "creator_loop",
                    "--product-flow-smoke",
                    str(work),
                    *extra,
                ],
                env={
                    **os.environ,
                    "CREATOR_LOOP_DATA_ROOT": str(foreign),
                    "QT_QPA_PLATFORM": "offscreen",
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "app"),
                },
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, expected, result.stderr)
            self.assertFalse(work.exists())
            self.assertEqual(list(foreign.iterdir()), [marker])
            self.assertEqual(
                marker.read_bytes(), b"unselected user data stays unchanged"
            )
            return result

    def test_mixed_flags_refused_before_root_initialization(self):
        self._refused(["--backup"], 2)

    def test_missing_work_root_or_unsupported_platform_refused(self):
        result = self._refused([], 4)
        # Qt plugin warnings may precede the final refusal JSON on Windows.
        receipt = json.loads(result.stderr.splitlines()[-1])
        self.assertEqual(receipt["product_flow_probe"], "refused")


if __name__ == "__main__":
    unittest.main()
