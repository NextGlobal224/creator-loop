"""Managed schema/lock gap refuses migration; native lifecycle tested separately."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_update_preparation as preparation_fixture
from creator_loop.app_lock import AppDataLock
from creator_loop.database import SCHEMA_VERSION
from creator_loop.managed_launcher import launch_managed
from creator_loop.owned_process import ProcessOutcome
from creator_loop.update_activation import activate_prepared_update


class ManagedLauncherTests(unittest.TestCase):
    def setUp(self):
        self.fixture = preparation_fixture.UpdatePreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.installation
        journal = self.fixture._prepare()
        with patch("creator_loop.update_activation.run_health_check"):
            self.candidate = activate_prepared_update(
                self.root, journal, self.installation
            )
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        self.env["CREATOR_LOOP_DATA_ROOT"] = str(self.root)

    def _source(self, root=None):
        env = self.env.copy()
        env["CREATOR_LOOP_DATA_ROOT"] = str(root or self.root)
        return subprocess.run(
            [sys.executable, "-m", "creator_loop", "--compatible-only", "--smoke"],
            env=env,
            capture_output=True,
            timeout=10,
        )

    def test_compatible_child_never_initializes_missing_or_migrates_old_database(self):
        missing = self.root.parent / "missing"
        result = self._source(missing)
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertFalse(missing.exists())
        self.fixture._legacy(SCHEMA_VERSION - 1)
        before = self.fixture.path.read_bytes()
        result = self._source()
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertEqual(self.fixture.path.read_bytes(), before)
        self.assertEqual(list(self.fixture.path.parent.glob("*.backup-*")), [])

    def test_compatible_child_obeys_app_lock_then_validates_existing_schema(self):
        with AppDataLock(self.root):
            result = self._source()
        self.assertEqual(result.returncode, 3, result.stderr)
        before = self.fixture.path.read_bytes()
        result = self._source()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fixture.path.read_bytes(), before)

    def _process_fixture(self, *, change_schema=False):
        test = self

        class SourceProcessFixture:
            def __init__(self, executable, arguments, directory, **kwargs):
                test.assertEqual(
                    executable, test.candidate / "CreatorLoop/CreatorLoop.exe"
                )
                test.assertEqual(arguments, ["--compatible-only", "--smoke"])
                test.assertFalse(kwargs["capture_output"])
                with AppDataLock(test.root):
                    pass  # parent releases lock before child may acquire it
                if change_schema:
                    test.fixture._legacy(SCHEMA_VERSION - 1)
                self.before = test.fixture.path.read_bytes()
                directory.mkdir()
                self.record = {"fixture": "source launcher; native tested separately"}

            def __enter__(self):
                return self

            def wait(self, timeout):
                result = test._source()
                test.assertEqual(test.fixture.path.read_bytes(), self.before)
                return ProcessOutcome(result.returncode, False, 0)

            def __exit__(self, *args):
                pass

        return SourceProcessFixture

    def test_managed_pointer_starts_compatible_child_after_releasing_parent_locks(self):
        with patch(
            "creator_loop.managed_launcher.OwnedWindowsProcess", self._process_fixture()
        ):
            self.assertEqual(
                launch_managed(self.root, self.installation, smoke=True), 0
            )
        records = list((self.root / "logs").glob("managed-launch-*/result.json"))
        self.assertEqual(len(records), 1)
        self.assertFalse(json.loads(records[0].read_text())["output_capture"])

    def test_schema_change_in_launch_gap_is_refused_by_child_without_auto_migration(
        self,
    ):
        with patch(
            "creator_loop.managed_launcher.OwnedWindowsProcess",
            self._process_fixture(change_schema=True),
        ):
            self.assertEqual(
                launch_managed(self.root, self.installation, smoke=True), 4
            )
        self.fixture._assert_source(SCHEMA_VERSION - 1)

    def test_blocked_pointer_and_current_schema_outside_range_never_start_child(self):
        pointer_path = self.installation / "active-installation.json"
        pointer = json.loads(pointer_path.read_text())
        pointer["status"] = "HEALTH_FAILED"
        pointer_path.write_text(json.dumps(pointer))
        with patch("creator_loop.managed_launcher.OwnedWindowsProcess") as process:
            with self.assertRaisesRegex(RuntimeError, "health-validated"):
                launch_managed(self.root, self.installation, smoke=True)
            process.assert_not_called()
        pointer["status"] = "ACTIVE"
        pointer_path.write_text(json.dumps(pointer))
        self.fixture._legacy(SCHEMA_VERSION - 1)
        before = self.fixture.path.read_bytes()
        with patch("creator_loop.managed_launcher.OwnedWindowsProcess") as process:
            with self.assertRaisesRegex(RuntimeError, "current schema"):
                launch_managed(self.root, self.installation, smoke=True)
            process.assert_not_called()
        self.assertEqual(self.fixture.path.read_bytes(), before)
