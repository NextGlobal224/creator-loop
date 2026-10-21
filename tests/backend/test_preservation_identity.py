"""Preservation-only creation proof must not authorize unknown/live owners."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from creator_loop.backend.runtime import preservation_identity as proof
from creator_loop.backend.runtime.owned_process import OwnedWindowsProcess

BASE = 134000000000000000


class PreservationIdentityTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"pid": 202, "creation_identity": str(BASE)}
        self.rows = [
            {"pid": 202, "creation": str(BASE + 100000000)},
            {"pid": 101, "creation": str(BASE + 50000000)},
        ]

    def read(self, rows=None, *, text=None, stderr="", failure=None):
        completed = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(self.rows if rows is None else rows) if text is None else text,
            stderr,
        )
        with (
            patch.object(proof.os, "getpid", return_value=101),
            patch.object(proof, "_api", return_value=Mock()),
            patch.object(
                proof,
                "process_identity",
                return_value={"creation_identity": str(BASE + 50000000)},
            ),
            patch.object(
                proof,
                "_powershell",
                return_value=Path(
                    "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
                ),
            ),
            patch.object(
                proof.time,
                "time_ns",
                return_value=(BASE + 200000000 - 116444736000000000) * 100,
            ),
            patch.object(
                proof.subprocess, "run", return_value=completed, side_effect=failure
            ) as run,
        ):
            result = proof._later_local_creation(self.identity)
            args = run.call_args
            self.assertEqual(args.kwargs["timeout"], 12)
            self.assertTrue(args.kwargs["check"])
            self.assertTrue(args.kwargs["env"]["PSModulePath"].endswith("Modules"))
            self.assertEqual(
                sum(key.casefold() == "psmodulepath" for key in args.kwargs["env"]), 1
            )
            command = args.args[0][-1]
            self.assertIn("-Namespace root/cimv2", command)
            self.assertIn("ProcessId=202 OR ProcessId=101", command)
            self.assertIn("-Property ProcessId,CreationDate", command)
            self.assertNotIn("-ComputerName", command)
            self.assertIn("$PSModuleAutoLoadingPreference='None'", command)
            self.assertIn("CimCmdlets/CimCmdlets.psd1", command.replace("\\", "/"))
            self.assertIn("CimCmdlets\\Get-CimInstance", command)
            self.assertIn("Microsoft.PowerShell.Utility\\ConvertTo-Json", command)
            return result

    def test_later_creation_requires_calibrated_witness_and_one_second_separation(self):
        self.assertTrue(self.read())
        for offset in (-100000000, 0, 9, 10000000):
            rows = [{"pid": 202, "creation": str(BASE + offset)}, self.rows[1]]
            with self.subTest(offset=offset):
                self.assertFalse(self.read(rows))

    def test_missing_ambiguous_duplicate_or_implausible_evidence_refuses(self):
        for rows in (
            [],
            self.rows[:1],
            self.rows + self.rows[:1],
            [self.rows[0], self.rows[0]],
            [self.rows[0], {"pid": 101, "creation": str(BASE)}],
            [{"pid": True, "creation": str(BASE + 100000000)}, self.rows[1]],
            [{"pid": 202, "creation": None}, self.rows[1]],
            [{"pid": 202, "creation": str(BASE + 300000000)}, self.rows[1]],
            [{"pid": 202, "creation": "0"}, self.rows[1]],
            [{**self.rows[0], "name": "different.exe"}, self.rows[1]],
        ):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.read(rows)
        for text in (
            '[{"pid":202,"pid":202,"creation":"1"}]',
            "null",
            "broken",
            " " * 4097,
        ):
            with self.subTest(text=text[:50]), self.assertRaises(ValueError):
                self.read(text=text)
        with self.assertRaises(ValueError):
            self.read(stderr="provider warning")

    def test_timeout_and_command_failure_are_not_positive_evidence(self):
        for error in (
            subprocess.TimeoutExpired("powershell", 12),
            subprocess.CalledProcessError(1, "powershell"),
        ):
            with (
                self.subTest(error=error),
                self.assertRaises(subprocess.SubprocessError),
            ):
                self.read(failure=error)

    def test_wrapper_keeps_live_and_other_native_errors_without_fallback(self):
        with patch.object(proof, "_later_local_creation") as extra:
            for value in (False, True):
                with patch.object(proof, "_identity_dead", return_value=value):
                    self.assertEqual(
                        proof.preservation_identity_dead(self.identity), value
                    )
            with patch.object(
                proof, "_identity_dead", side_effect=ValueError("invalid")
            ):
                with self.assertRaises(ValueError):
                    proof.preservation_identity_dead(self.identity)
            error = OSError("other native error")
            error.winerror = 6
            with (
                patch.object(proof, "_identity_dead", side_effect=error),
                self.assertRaises(OSError),
            ):
                proof.preservation_identity_dead(self.identity)
            extra.assert_not_called()

    def test_access_denial_is_preserved_unless_fresh_later_creation_proves_reuse(self):
        error = PermissionError("native access denied")
        error.winerror = 5
        with patch.object(proof, "_identity_dead", side_effect=error):
            with patch.object(proof, "_later_local_creation", return_value=True):
                self.assertTrue(proof.preservation_identity_dead(self.identity))
            for value in (
                False,
                ValueError("ambiguous"),
                OSError("denied"),
                subprocess.TimeoutExpired("powershell", 12),
            ):
                with patch.object(
                    proof,
                    "_later_local_creation",
                    return_value=value if not isinstance(value, Exception) else None,
                    side_effect=value if isinstance(value, Exception) else None,
                ):
                    with (
                        self.subTest(value=value),
                        self.assertRaises(PermissionError) as caught,
                    ):
                        proof.preservation_identity_dead(self.identity)
                    self.assertIs(caught.exception, error)

    @unittest.skipUnless(
        sys.platform == "win32", "real local CIM/native creation witness"
    )
    def test_real_provider_agrees_with_native_child_creation_without_killing_other_processes(
        self,
    ):
        with tempfile.TemporaryDirectory(prefix="preservation-cim-") as folder:
            shadow = Path(folder) / "shadow"
            for module in ("CimCmdlets", "Microsoft.PowerShell.Utility"):
                directory = shadow / module
                directory.mkdir(parents=True)
                (directory / (module + ".psm1")).write_text(
                    "throw 'Untrusted module must never load'", encoding="utf-8"
                )
            with (
                patch.dict(proof.os.environ, {"PSModulePath": str(shadow)}),
                OwnedWindowsProcess(
                    Path(sys.executable).resolve(),
                    ["-c", "import time;time.sleep(30)"],
                    Path(folder) / "child",
                    component_version="preservation-cim-test",
                ) as child,
            ):
                identity = child.record
                self.assertFalse(proof._later_local_creation(identity))
                # Synthetic older record only tests the provider comparison;
                # it does not claim this fixture actually reused a Windows PID.
                older = {
                    **identity,
                    "creation_identity": str(
                        int(identity["creation_identity"]) - 864000000000
                    ),
                }
                self.assertTrue(proof._later_local_creation(older))
