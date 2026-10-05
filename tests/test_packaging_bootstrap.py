"""The packaging-only runtime hook must leave source execution untouched."""

import os
import runpy
import sys
import tempfile
import unittest
from importlib.machinery import EXTENSION_SUFFIXES, ExtensionFileLoader, FileFinder
from pathlib import Path
from unittest.mock import patch


class PackagingBootstrapTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows extended namespace")
    def test_bundle_root_without_short_alias_keeps_native_extension_discovery(self):
        hook = Path(__file__).resolve().parents[1] / "packaging/windows_long_paths.py"
        with patch.object(sys, "frozen", False, create=True):
            alias = runpy.run_path(str(hook))["_bundle_import_path"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve(strict=True)
            (root / "_fixture.pyd").write_bytes(b"marker, never executed")
            extended = "\\\\?\\" + str(root)
            mapped = alias(str(root), str(root), extended)
            finder = FileFinder(mapped, (ExtensionFileLoader, EXTENSION_SUFFIXES))
            self.assertIsNotNone(finder.find_spec("_fixture"))
            self.assertEqual(mapped, extended)

    def test_source_execution_keeps_import_paths_environment_and_runtime_state(self):
        hook = Path(__file__).resolve().parents[1] / "packaging/windows_long_paths.py"
        paths = list(sys.path)
        environment = dict(os.environ)
        marker = object()
        with (
            patch.object(sys, "frozen", False, create=True),
            patch.object(sys, "_MEIPASS", marker, create=True),
            patch.object(sys, "_creator_loop_dll_directories", marker, create=True),
        ):
            runpy.run_path(str(hook))
            self.assertEqual(sys.path, paths)
            self.assertEqual(dict(os.environ), environment)
            self.assertIs(sys._MEIPASS, marker)
            self.assertIs(sys._creator_loop_dll_directories, marker)

    @unittest.skipUnless(sys.platform == "win32", "Windows drive/path semantics")
    def test_bundle_alias_does_not_rewrite_external_import_paths(self):
        hook = Path(__file__).resolve().parents[1] / "packaging/windows_long_paths.py"
        with patch.object(sys, "frozen", False, create=True):
            alias = runpy.run_path(str(hook))["_bundle_import_path"]
        original = r"C:\installation\_internal"
        bundle = r"C:\INSTAL~1\_INTER~1"
        for path in (r"E:\external", r"C:\installation\other", "", "relative"):
            self.assertEqual(alias(path, original, bundle), path)
        self.assertEqual(
            alias(original + r"\base_library.zip", original, bundle),
            bundle + r"\base_library.zip",
        )
        self.assertEqual(
            alias(original + r"\..names", original, bundle), bundle + r"\..names"
        )
