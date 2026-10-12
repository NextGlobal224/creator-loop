import json
import tempfile
import unittest
from pathlib import Path

from creator_loop.facebook_ui import read_terminal


class FacebookTerminalTests(unittest.TestCase):
    def test_tab_selection_rejects_invalid_or_duplicate_targets_and_exit_mismatch(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "result.json"
            body = {
                "format": 1,
                "run_id": "a" * 32,
                "status": "SELECT_TAB",
                "count": 0,
                "tabs": [
                    {"target_id": "first", "title": "Facebook"},
                    {"target_id": "second", "title": "Facebook"},
                ],
            }
            path.write_text(json.dumps(body), encoding="utf-8")
            self.assertEqual(read_terminal(path, "a" * 32, 0, 20), body)
            with self.assertRaises(ValueError):
                read_terminal(path, "a" * 32, 5, 20)
            for tabs in (
                [body["tabs"][0]] * 2,
                [{"target_id": "ws://evil", "title": "x"}] * 2,
                [{"target_id": "first", "title": "x", "url": "secret"}] * 2,
            ):
                path.write_text(json.dumps({**body, "tabs": tabs}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_terminal(path, "a" * 32, 0, 20)

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
