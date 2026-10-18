"""Pinned installer refuses tampered bytes and preserves existing components."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from creator_loop.backend.facebook.facebook_install import install_browser


@unittest.skipUnless(os.name == "nt", "Native Windows namespace leases")
class FacebookInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "dữ liệu beta"
        self.payload = b"pinned browser fixture"
        self.pin = {
            "browser_version": "fixture-1",
            "download_url": "https://storage.googleapis.com/chrome-for-testing-public/fixture.zip",
            "files": {
                "chrome.exe": {
                    "size": len(self.payload),
                    "sha256": hashlib.sha256(self.payload).hexdigest(),
                }
            },
        }

    def response(self, payload=None, path="chrome-win64/chrome.exe"):
        body = io.BytesIO()
        with zipfile.ZipFile(body, "w") as archive:
            archive.writestr(path, self.payload if payload is None else payload)
        body.seek(0)
        body.url = self.pin["download_url"]
        return body

    def install(self, response):
        with (
            patch(
                "creator_loop.backend.facebook.facebook_install.browser_pin",
                return_value=self.pin,
            ),
            patch(
                "creator_loop.backend.facebook.facebook_install.urllib.request.urlopen",
                return_value=response,
            ) as download,
        ):
            result = install_browser(self.root)
            return result, download.call_count

    def test_install_then_reuse_without_download_and_keep_existing_bytes(self):
        target, count = self.install(self.response())
        self.assertEqual(count, 1)
        self.assertEqual((target / "chrome.exe").read_bytes(), self.payload)
        target_again, count = self.install(None)
        self.assertEqual((target_again, count), (target, 0))
        (target / "chrome.exe").write_bytes(b"tampered browser bytes")
        with self.assertRaises(ValueError):
            self.install(None)
        self.assertEqual(
            (target / "chrome.exe").read_bytes(), b"tampered browser bytes"
        )

    def test_archive_traversal_and_digest_mismatch_leave_no_selected_runtime(self):
        for response in (
            self.response(path="chrome-win64/../chrome.exe"),
            self.response(payload=b"X" * len(self.payload)),
        ):
            with self.assertRaises(ValueError):
                self.install(response)
            self.assertFalse(
                (self.root / "manifests/facebook-browser-runtime.json").exists()
            )
            self.assertFalse(
                (self.root / "components/facebook-browser-fixture-1").exists()
            )
            self.assertFalse((self.root / "chrome.exe").exists())
