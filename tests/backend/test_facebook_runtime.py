"""Native-compatible namespace leases never claim to prohibit child additions."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.backend.facebook.facebook_runtime import verify_browser_namespace


class FacebookRuntimeTests(unittest.TestCase):
    def test_new_file_or_empty_directory_is_rejected_by_namespace_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "locales").mkdir()
            (root / "chrome.exe").write_bytes(b"fixture")
            (root / "locales/en.pak").write_bytes(b"locale")
            pin = {"files": {"chrome.exe": {}, "locales/en.pak": {}}}
            with patch(
                "creator_loop.backend.facebook.facebook_runtime.browser_pin",
                return_value=pin,
            ):
                self.assertEqual(len(verify_browser_namespace(root)), 3)
                extra = root / "unverified.dll"
                extra.write_bytes(b"never load")
                with self.assertRaises(ValueError):
                    verify_browser_namespace(root)
                extra.unlink()
                (root / "unverified-empty-directory").mkdir()
                with self.assertRaises(ValueError):
                    verify_browser_namespace(root)
