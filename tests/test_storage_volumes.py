"""Probe the actual volume identity used by registered-root preflight."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from creator_loop.storage_volumes import volume_identity


class StorageVolumeTests(unittest.TestCase):
    def test_unicode_root_and_file_share_actual_volume_identity(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            root = base / "Kho Đà Nẵng có khoảng trắng"
            root.mkdir()
            original = root / "Tư liệu.txt"
            original.write_bytes(b"original")
            identity = volume_identity(root)
            self.assertEqual(identity, volume_identity(base))
            self.assertEqual(identity, volume_identity(original))
            if os.name == "nt":
                self.assertRegex(identity, r"^\\\\\?\\volume\{[0-9a-f-]{36}\}\\$")
                self.assertNotEqual(identity, root.drive.casefold())
            else:
                self.assertEqual(identity, f"posix-device:{root.stat().st_dev}")

    def test_missing_root_is_unavailable_without_creating_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            missing = Path(folder) / "Volume không sẵn"
            with self.assertRaises(OSError):
                volume_identity(missing)
            self.assertFalse(missing.exists())
