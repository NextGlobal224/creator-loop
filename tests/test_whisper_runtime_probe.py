"""Version preflight lifecycle/privacy doubles; not real model acceptance."""

import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from creator_loop.app_lock import AppDataLock, DataRootBusy
from creator_loop.component_selection import save_component_selection
from creator_loop.database import initialize
from creator_loop.local_components import (
    ComponentSpec,
    VerifiedComponent,
    hold_verified_components,
)
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.paths import ensure_data_root
from creator_loop.runtime_recovery import _identity_dead
from creator_loop.whisper_adapter import RUNTIME_FILES
from creator_loop.whisper_runtime_probe import (
    inspect_amd64_image,
    parse_engine_version,
    probe_selected_whisper_cli,
)


def pe_header(*, dll=True):
    # Synthetic headers only; never loaded as DLLs by the native CLI double.
    raw = bytearray(200)
    raw[:2] = b"MZ"
    raw[60:64] = (64).to_bytes(4, "little")
    raw[64:68] = b"PE\x00\x00"
    raw[68:70] = (0x8664).to_bytes(2, "little")
    raw[84:86] = (112).to_bytes(2, "little")
    raw[86:88] = (2 | (0x2000 if dll else 0)).to_bytes(2, "little")
    raw[88:90] = (0x20B).to_bytes(2, "little")
    return bytes(raw)


def spec(path, raw, *, model=False):
    return ComponentSpec(
        path.name.replace(".", "-"),
        "MODEL" if model else "ENGINE",
        path,
        "5359861c739e955e79d9a303bcbc70fb988958b1" if model else "1.8.7",
        hashlib.sha256(raw).hexdigest(),
        len(raw),
        "https://example.invalid/native-version-fixture",
        "SYNTHETIC TEST ONLY",
        "https://example.invalid/fixture-license",
        256 * 1024**2,
        True,
        False,
    )


class VersionMetadataTests(unittest.TestCase):
    def test_cli_conflicts_refuse_before_any_data_root_access(self):
        from creator_loop import __main__ as launcher

        for arguments in (
            ["--smoke"],
            ["--inspect-components"],
            ["--health"],
            ["--component-worker-memory-mib", "768"],
        ):
            with self.subTest(arguments=arguments):
                with (
                    patch.object(
                        sys,
                        "argv",
                        ["CreatorLoop", "--check-whisper-runtime", *arguments],
                    ),
                    patch.object(launcher, "data_root") as get_root,
                    redirect_stderr(io.StringIO()),
                ):
                    with self.assertRaises(SystemExit) as refused:
                        launcher.main()
                    self.assertEqual(refused.exception.code, 2)
                    get_root.assert_not_called()

    def test_strict_receipt_rejects_extra_private_output_and_unbounded_fields(self):
        for raw in (
            b"whisper.cpp version: 1.8.7",
            b"whisper.cpp version: 1.8.7\n",
            b"whisper.cpp version: 1.8.7\r\n",
        ):
            self.assertEqual(parse_engine_version(raw), "1.8.7")
        for raw in (
            b"",
            b"1.8.7",
            b"whisper.cpp version: 1.8.7\nPRIVATE_TRANSCRIPT",
            b"whisper.cpp version: 1000.8.7\n",
            b"\xff",
            b"x" * 32769,
        ):
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                parse_engine_version(raw)

    def inspect(self, raw, *, dll=True):
        stream = io.BytesIO(raw)
        try:
            inspect_amd64_image(
                VerifiedComponent(
                    spec(Path("runtime.dll" if dll else "cli.exe"), raw), stream
                )
            )
        finally:
            self.assertEqual(stream.tell(), 0)

    def test_architecture_type_and_header_offsets_must_match_held_file(self):
        self.inspect(pe_header())
        self.inspect(pe_header(dll=False), dll=False)
        for offset, value in (
            (0, b"NO"),
            (60, (1).to_bytes(4, "little")),
            (60, (2**32 - 1).to_bytes(4, "little")),
            (64, b"PX\x00\x00"),
            (68, (0x14C).to_bytes(2, "little")),
            (84, (3).to_bytes(2, "little")),
            (86, (0x2000).to_bytes(2, "little")),
            (88, (0x10B).to_bytes(2, "little")),
        ):
            raw = bytearray(pe_header())
            raw[offset : offset + len(value)] = value
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                self.inspect(bytes(raw))
        with self.assertRaises(ValueError):
            self.inspect(pe_header(dll=False))
        with self.assertRaises(ValueError):
            self.inspect(pe_header()[:-1])


FAKE_CLI = r"""
using System;
using System.Threading;
public class VersionFixture {
    public static int Main(string[] args) {
        if(args.Length!=1 || args[0]!="--version") return 19;
        string mode=Environment.GetEnvironmentVariable("CREATOR_LOOP_VERSION_FIXTURE");
        if(mode=="hang") Thread.Sleep(30000);
        if(mode=="crash") return 17;
        if(mode=="flood") { Console.Error.Write(new string('x',40000)); return 0; }
        if(mode=="private") Console.Out.WriteLine("PRIVATE_TRANSCRIPT_MUST_NOT_BE_LOGGED");
        Console.Error.WriteLine(mode=="mismatch" ? "whisper.cpp version: 1.8.6" : "whisper.cpp version: 1.8.7");
        return 0;
    }
}
"""


@unittest.skipUnless(sys.platform == "win32", "native engine version Job/leases")
class NativeVersionProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="CreatorLoop version double ")
        cls.addClassCleanup(temporary.cleanup)
        cls.base = Path(temporary.name).resolve(strict=True)
        cls.runtime = cls.base / "cpu-runtime"
        cls.runtime.mkdir()
        source = cls.base / "fixture.cs"
        source.write_text(FAKE_CLI, encoding="utf-8")
        compiler_exe = (
            Path(os.environ["SystemRoot"])
            / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
        )
        with OwnedWindowsProcess(
            compiler_exe,
            [
                "/nologo",
                "/platform:x64",
                "/target:exe",
                "/out:" + str(cls.runtime / "whisper-cli.exe"),
                str(source),
            ],
            cls.base / "compiler-log",
            component_version="test/native-version-double",
        ) as compiler:
            outcome = compiler.wait(45)
        if outcome.exit_code != 0 or outcome.timed_out:
            raise RuntimeError(
                (cls.base / "compiler-log/stdout.log").read_text(errors="replace")
            )
        for name in RUNTIME_FILES - {"whisper-cli.exe"}:
            (cls.runtime / name).write_bytes(pe_header())
        cls.model = cls.base / "synthetic-model.bin"
        cls.model.write_bytes(b"FAKE MODEL MUST NEVER EXECUTE")
        cls.specs = tuple(
            spec(cls.runtime / name, (cls.runtime / name).read_bytes())
            for name in sorted(RUNTIME_FILES)
        ) + (spec(cls.model, cls.model.read_bytes(), model=True),)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.base)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True) / "data"
        ensure_data_root(self.root)
        self.db = self.root / "creator_loop.sqlite3"
        initialize(self.db)
        with AppDataLock(self.root) as lock:
            save_component_selection(
                self.root, self.specs, lock=lock, worker_memory_limit=256 * 1024**2
            )
        self.manifest = self.root / "manifests/storage-roots.json"
        self.before = (self.db.read_bytes(), self.manifest.read_bytes())

    def probe(self, mode="normal", **kwargs):
        with patch.dict(os.environ, {"CREATOR_LOOP_VERSION_FIXTURE": mode}):
            return probe_selected_whisper_cli(self.root, **kwargs)

    def assert_preserved(self):
        self.assertEqual(
            (self.db.read_bytes(), self.manifest.read_bytes()), self.before
        )
        self.assertFalse(
            json.loads(self.manifest.read_bytes())["component_selection"][
                "runtime_compatibility_verified"
            ]
        )
        for item in self.specs:
            with item.path.open("r+b") as stream:
                self.assertEqual(hashlib.sha256(stream.read()).hexdigest(), item.sha256)
        with AppDataLock(self.root):
            pass
        self.assertFalse(list((self.root / "logs").rglob("*.log")))
        for record in (self.root / "logs").rglob("ownership.json"):
            self.assertNotIn(b"PRIVATE_TRANSCRIPT", record.read_bytes())
            self.assertTrue(_identity_dead(json.loads(record.read_bytes())))
        from creator_loop.whisper_runtime_probe import _retained

        self.assertEqual(_retained, [])

    def test_actual_stderr_version_job_closes_leases_without_compatibility_or_mutation(
        self,
    ):
        result = self.probe()
        self.assertEqual(result["check"], "ENGINE_CLI_VERSION_CONFIRMED")
        self.assertEqual(result["engine_version"], "1.8.7")
        for field in (
            "model_executed",
            "model_compatibility_verified",
            "runtime_compatibility_verified",
        ):
            self.assertIs(result[field], False)
        self.assert_preserved()

    def test_mismatch_private_output_flood_and_crash_refuse_without_default_logs(self):
        for mode in ("mismatch", "private", "flood", "crash"):
            with self.subTest(mode=mode), self.assertRaises((ValueError, RuntimeError)):
                self.probe(mode)
            self.assert_preserved()

    def test_timeout_and_post_launch_cancel_stop_only_owned_tree_and_release_leases(
        self,
    ):
        with OwnedWindowsProcess(
            Path(sys.executable),
            ["-c", "import time; time.sleep(60)"],
            self.root.parent / "separate-sentinel-log",
            component_version="test/separate-sentinel",
        ) as sentinel:
            with self.assertRaises(TimeoutError):
                self.probe("hang", timeout_seconds=0.15)
            self.assertEqual(len(list((self.root / "logs").iterdir())), 1)
            self.assert_preserved()
            self.assertIsNone(sentinel.poll())
            started = time.monotonic()
            with self.assertRaises(InterruptedError):
                self.probe("hang", cancelled=lambda: time.monotonic() - started > 0.3)
            self.assertEqual(len(list((self.root / "logs").iterdir())), 2)
            self.assert_preserved()
            self.assertIsNone(sentinel.poll())

    def test_guard_created_during_hash_refuses_before_native_child_start(self):
        guard = self.root / "runtime/restore-in-progress.json"

        @contextmanager
        def after_hash(*args, **kwargs):
            with hold_verified_components(*args, **kwargs) as items:
                guard.write_bytes(b"retain guarded history")
                yield items

        with patch(
            "creator_loop.whisper_runtime_probe.hold_verified_components", after_hash
        ):
            with self.assertRaisesRegex(RuntimeError, "Restore guard"):
                self.probe()
        self.assertEqual(guard.read_bytes(), b"retain guarded history")
        self.assertFalse(list((self.root / "logs").iterdir()))
        self.assert_preserved()

    def test_busy_root_or_existing_guard_never_launches(self):
        with AppDataLock(self.root), self.assertRaises(DataRootBusy):
            self.probe()
        guard = self.root / "runtime/restore-in-progress.json"
        guard.mkdir()
        with self.assertRaisesRegex(RuntimeError, "Restore guard"):
            self.probe()
        self.assertFalse(list((self.root / "logs").iterdir()))
        self.assert_preserved()

    def test_missing_db_is_not_initialized(self):
        self.db.unlink()
        with self.assertRaises(OSError):
            self.probe()
        self.assertFalse(self.db.exists())
        self.assertEqual(self.manifest.read_bytes(), self.before[1])
        self.assertFalse(list((self.root / "logs").iterdir()))

    def test_changed_component_metadata_or_extra_runtime_file_never_launches(self):
        selection = json.loads(self.manifest.read_bytes())
        selection["component_selection"]["manifest"]["components"][0]["sha256"] = (
            "0" * 64
        )
        self.manifest.write_text(json.dumps(selection), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.probe()
        self.manifest.write_bytes(self.before[1])
        unexpected = self.runtime / "unselected.dll"
        unexpected.write_bytes(b"must not load")
        try:
            with self.assertRaisesRegex(ValueError, "Unexpected file"):
                self.probe()
            self.assertFalse(list((self.root / "logs").iterdir()))
        finally:
            unexpected.unlink()
        self.assert_preserved()
