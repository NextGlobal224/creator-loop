"""Real Win32 launches keep canonical records, including extended input paths."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.database import initialize
from creator_loop.owned_process import OwnedWindowsProcess, _api
from creator_loop.paths import ensure_data_root
from creator_loop.runtime_ownership import process_identity
from creator_loop.runtime_recovery import _identity_dead
from creator_loop.update_health import run_health_check
from creator_loop.windows_paths import file_io_path


@unittest.skipUnless(sys.platform == "win32", "real Windows image/Job identities")
class OwnedProcessIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="creator-identity-")
        self.base = Path(self.temporary.name).resolve(strict=True)
        self.addCleanup(self._cleanup)
        python = Path(sys._base_executable).resolve(strict=True)
        self.env = os.environ.copy()
        self.env["PYTHONHOME"] = str(python.parent)
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.executables = []
        for label in ("short", "Bản cài Huế", "deep"):
            folder = self.base / label
            if label == "deep":
                while len(str(folder)) <= 280:
                    folder /= "Bản cài dài có dấu " + "x" * 35
            file_io_path(folder).mkdir(parents=True)
            exe = folder / "python.exe"
            for name in (
                python.name,
                "python3.dll",
                f"python{sys.version_info.major}{sys.version_info.minor}.dll",
            ):
                shutil.copyfile(
                    file_io_path(python.parent / name), file_io_path(folder / name)
                )
            self.executables.append(exe)
        self.assertGreater(len(str(self.executables[-1])), 260)

    def _cleanup(self):
        target = file_io_path(self.base).resolve(strict=True)
        self.assertEqual(
            target, file_io_path(Path(self.temporary.name)).resolve(strict=True)
        )
        shutil.rmtree(target)  # only this fixture, after owned contexts close
        self.temporary.cleanup()

    def _assert_live_record(self, executable, directory, record):
        # Compare serialized values directly: never normalize the actual record.
        self.assertEqual(record["executable"], str(executable))
        saved = json.loads(
            file_io_path(directory / "ownership.json").read_text(encoding="utf-8")
        )
        self.assertEqual(saved, record)
        kernel = _api()
        handle = kernel.OpenProcess(0x00101000, False, record["pid"])
        self.assertTrue(handle)
        try:
            self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0x102)
            observed = process_identity(kernel, handle)
            self.assertEqual(observed["executable"], str(executable))
            self.assertEqual(observed["creation_identity"], record["creation_identity"])
            self.assertEqual(
                observed["creation_identity_format"], record["creation_identity_format"]
            )
        finally:
            kernel.CloseHandle(handle)
        self.assertFalse(_identity_dead(saved))
        wrong_image = {**saved, "executable": str(self.base / "unrelated.exe")}
        with self.assertRaisesRegex(ValueError, "executable binding mismatch"):
            _identity_dead(wrong_image)
        reused = {
            **saved,
            "creation_identity": str(int(saved["creation_identity"]) + 1),
        }
        self.assertTrue(_identity_dead(reused))

    def test_record_callback_image_and_cwd_for_normal_and_extended_executables(self):
        code = (
            "import ctypes,json,os; k=ctypes.WinDLL('kernel32'); "
            "k.GetCommandLineW.restype=ctypes.c_wchar_p; "
            "print(json.dumps({'cwd':os.getcwd(),'command':k.GetCommandLineW()}))"
        )
        for index, exe in enumerate(self.executables):
            for alias in (False, True):
                with self.subTest(executable=str(exe), extended_input=alias):
                    directory = self.base / f"process-{index}-{alias}"
                    callbacks = []

                    def before_resume(record):
                        self._assert_live_record(exe, directory, record)
                        callbacks.append(record)

                    with OwnedWindowsProcess(
                        file_io_path(exe) if alias else exe,
                        ["-c", code],
                        directory,
                        component_version="identity-regression",
                        environment=self.env,
                        cwd=file_io_path(self.executables[1].parent),
                        before_resume=before_resume,
                    ) as process:
                        self.assertEqual(process.executable, exe)
                        self.assertEqual(process.record, callbacks[0])
                        outcome = process.wait(15)
                        self.assertEqual(
                            outcome.exit_code,
                            0,
                            file_io_path(process.stderr_path).read_text(),
                        )
                        self.assertFalse(outcome.timed_out)
                        self.assertTrue(process.tree_finished())
                    child = json.loads(file_io_path(process.stdout_path).read_text())
                    self.assertTrue(
                        os.path.samefile(
                            file_io_path(Path(child["cwd"])),
                            file_io_path(self.executables[1].parent),
                        )
                    )
                    # argv[0] retains the launch alias, independent of saved identity.
                    self.assertTrue(
                        child["command"].startswith('"' + str(file_io_path(exe)) + '"')
                        or child["command"].startswith(str(file_io_path(exe)) + " ")
                    )
                    self.assertEqual(len(callbacks), 1)
                    self.assertEqual(
                        json.loads(
                            file_io_path(directory / "ownership.json").read_text()
                        ),
                        callbacks[0],
                    )

    def test_real_health_receipts_preserve_canonical_identity_at_all_three_paths(self):
        root = self.base / "data"
        ensure_data_root(root)
        initialize(root / "creator_loop.sqlite3")
        original = (root / "creator_loop.sqlite3").read_bytes()
        for index, exe in enumerate(self.executables):
            with self.subTest(executable=str(exe)):
                directory = root / "logs" / f"health-{index}"
                callbacks = []

                def observe_launch(executable, arguments, logs, **kwargs):
                    self.assertEqual(executable, exe)

                    def before_resume(record):
                        self._assert_live_record(exe, logs, record)
                        callbacks.append(record)

                    return OwnedWindowsProcess(
                        executable,
                        arguments,
                        logs,
                        before_resume=before_resume,
                        **kwargs,
                    )

                # Only insert an observer; Win32 APIs, Job and executable stay real.
                with (
                    patch.dict(os.environ, self.env),
                    patch(
                        "creator_loop.update_health.OwnedWindowsProcess",
                        side_effect=observe_launch,
                    ),
                ):
                    result = run_health_check(
                        exe,
                        root,
                        directory,
                        component_version="identity-regression",
                        arguments_prefix=["-m", "creator_loop"],
                    )
                self.assertTrue(result["query_ok"])
                receipt = json.loads(
                    file_io_path(directory / "result.json").read_text()
                )
                self.assertEqual(receipt["ownership"], callbacks[0])
                self.assertEqual(receipt["ownership"]["executable"], str(exe))
                self.assertEqual(receipt["exit_code"], 0)
                self.assertFalse(receipt["timed_out"])
                self.assertEqual((root / "creator_loop.sqlite3").read_bytes(), original)
