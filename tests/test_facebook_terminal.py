import json
import tempfile
import unittest
from pathlib import Path

from creator_loop.facebook_ui import read_terminal


class FacebookTerminalTests(unittest.TestCase):
    def test_stale_success_nonzero_and_wrong_identity_never_imported(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "result.json"
            body = {
                "format": 1,
                "run_id": "a" * 32,
                "status": "SUCCEEDED",
                "count": 1,
                "links": [
                    {
                        "url": "https://www.facebook.com/reel/12",
                        "excerpt": None,
                        "publisher": None,
                    }
                ],
            }
            path.write_text(json.dumps(body), encoding="utf-8")
            self.assertEqual(read_terminal(path, "a" * 32, 0, 20), body)
            for code, rid in ((5, "a" * 32), (0, "b" * 32)):
                with self.assertRaises(ValueError):
                    read_terminal(path, rid, code, 20)
            for change in (
                {"count": 2},
                {"format": True},
                {"links": body["links"] * 2, "count": 2},
                {
                    "links": [
                        {
                            "url": "https://www.facebook.com/profile.php?id=12",
                            "excerpt": None,
                            "publisher": None,
                        }
                    ]
                },
                {"status": "SCANNING"},
            ):
                path.write_text(json.dumps({**body, **change}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_terminal(path, "a" * 32, 0, 20)
