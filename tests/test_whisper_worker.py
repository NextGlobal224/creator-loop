"""Native CLI double for lifecycle/leases; no real model downloads in CI."""

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
import wave
from ctypes import wintypes as w
from pathlib import Path

from creator_loop.app_lock import AppDataLock
from creator_loop.local_components import ComponentSpec, component_manifest_bytes
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.paths import ensure_data_root
from creator_loop.runtime_ownership import process_identity
from creator_loop.runtime_recovery import _identity_dead
from creator_loop.whisper_adapter import (
    RUNTIME_FILES,
    WhisperRunError,
    WhisperSettings,
    transcribe_whisper_wav,
)

FAKE_CLI = r"""
using System;
using System.IO;
using System.Threading;
using System.Web.Script.Serialization;
using D = System.Collections.Generic.Dictionary<string, object>;
public class FixtureWhisper {
    public static int Main(string[] args) {
        string input="", output="", model="", language="";
        for(int i=0;i<args.Length-1;i++) {
            if(args[i]=="-f") input=args[i+1];
            if(args[i]=="-of") output=args[i+1];
            if(args[i]=="-m") model=args[i+1];
            if(args[i]=="-l") language=args[i+1];
        }
        if(Path.GetFileName(input)=="hang.wav") Thread.Sleep(30000);
        if(Path.GetFileName(input)=="crash.wav") return 17;
        var body = new D {
            {"model",new D{{"type","base"},{"multilingual",true}}},
            {"params",new D{{"language",language},{"translate",false},{"model",model}}},
            {"result",new D{{"language",language}}},
            {"transcription",new object[]{new D{
                {"offsets",new D{{"from",0},{"to",500}}},{"text"," fixture RAW"}}}}
        };
        File.WriteAllText(output+".json",new JavaScriptSerializer().Serialize(body));
        return 0;
    }
}
"""


@unittest.skipUnless(sys.platform == "win32", "native Windows Job/leases")
class WhisperWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(
            prefix="CreatorLoop whisper CLI double "
        )
        cls.addClassCleanup(temporary.cleanup)
        cls.base = Path(temporary.name).resolve(strict=True)
        cls.runtime = cls.base / "cpu-runtime"
        cls.runtime.mkdir()
        source = cls.base / "fixture.cs"
        source.write_text(FAKE_CLI, encoding="utf-8")
        compile_script = cls.base / "compile.ps1"

        def quote(path):
            return "'" + str(path).replace("'", "''") + "'"

        compile_script.write_text(
            "$ErrorActionPreference='Stop'\nAdd-Type -TypeDefinition ([IO.File]::ReadAllText("
            + quote(source)
            + ")) -ReferencedAssemblies System.Web.Extensions -OutputType ConsoleApplication -OutputAssembly "
            + quote(cls.runtime / "whisper-cli.exe")
            + "\n",
            encoding="utf-8",
        )
        powershell = (
            Path(os.environ["SystemRoot"])
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        with OwnedWindowsProcess(
            powershell,
            [
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(compile_script),
            ],
            cls.base / "compiler-log",
            component_version="test/native-cli-double",
        ) as compiler:
            outcome = compiler.wait(45)
        if outcome.exit_code != 0 or outcome.timed_out:
            raise RuntimeError(
                (cls.base / "compiler-log/stderr.log").read_text(errors="replace")
            )
        for name in RUNTIME_FILES - {"whisper-cli.exe"}:
            (cls.runtime / name).write_bytes(
                b"synthetic library never loaded by CLI double"
            )
        cls.model = cls.base / "fake-model.bin"
        cls.model.write_bytes(b"synthetic model, not real Whisper acceptance")
        cls.budget = 128 * 1024**2
        cls.specs = tuple(
            ComponentSpec(
                name.replace(".", "-"),
                "ENGINE",
                cls.runtime / name,
                "1.8.7",
                hashlib.sha256((cls.runtime / name).read_bytes()).hexdigest(),
                (cls.runtime / name).stat().st_size,
                "https://example.test/fake-engine",
                "MIT",
                "https://example.test/fake-license",
                cls.budget,
                True,
                False,
            )
            for name in sorted(RUNTIME_FILES)
        ) + (
            ComponentSpec(
                "fake-model",
                "MODEL",
                cls.model,
                "5359861c739e955e79d9a303bcbc70fb988958b1",
                hashlib.sha256(cls.model.read_bytes()).hexdigest(),
                cls.model.stat().st_size,
                "https://example.test/fake-model",
                "MIT",
                "https://example.test/fake-license",
                cls.budget,
                True,
                False,
            ),
        )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=self.base)
        self.addCleanup(temporary.cleanup)
        self.case = Path(temporary.name).resolve(strict=True)
        self.root = self.case / "data"
        ensure_data_root(self.root)
        self.input = self.case / "source.wav"
        with wave.open(str(self.input), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 16000)
        self.digest = hashlib.sha256(self.input.read_bytes()).hexdigest()

    def run_task(self, **kwargs):
        with AppDataLock(self.root) as lock:
            return transcribe_whisper_wav(
                self.specs,
                self.input,
                self.root,
                lock,
                settings=kwargs.pop("settings", WhisperSettings(self.budget)),
                expected_sha256=kwargs.pop("digest", self.digest),
                expected_size=self.input.stat().st_size,
                **kwargs,
            )

    def workspace(self):
        return next(
            entry
            for entry in (self.root / "runtime").iterdir()
            if entry.name.startswith("whisper-")
        )

    def assert_preserved_and_dead(self):
        child = json.loads((self.workspace() / "child-ownership.json").read_text())
        self.assertTrue(_identity_dead(child))
        self.assertEqual(
            hashlib.sha256(self.input.read_bytes()).hexdigest(), self.digest
        )
        for item in self.specs:
            self.assertEqual(
                hashlib.sha256(item.path.read_bytes()).hexdigest(), item.sha256
            )

    def test_success_preserves_raw_and_component_provenance(self):
        result = self.run_task()
        self.assertEqual(result.segments[0].text, " fixture RAW")
        self.assertEqual(result.raw_json, (result.workspace / "raw.json").read_bytes())
        self.assertEqual(len(result.component_ids_and_hashes), 6)
        self.assert_preserved_and_dead()

    def test_cancel_stops_owned_tree_and_preserves_external_bytes(self):
        self.input.rename(self.case / "hang.wav")
        self.input = self.case / "hang.wav"
        with self.assertRaises(InterruptedError):
            self.run_task(
                cancelled=lambda: any(
                    (entry / "child-ownership.json").is_file()
                    for entry in (self.root / "runtime").iterdir()
                )
            )
        self.assert_preserved_and_dead()

    def test_timeout_stops_owned_tree_and_retains_recovery_binding(self):
        self.input.rename(self.case / "hang.wav")
        self.input = self.case / "hang.wav"
        with self.assertRaises(TimeoutError):
            self.run_task(settings=WhisperSettings(self.budget, timeout_seconds=0.15))
        self.assert_preserved_and_dead()

    def test_native_failure_retains_workspace_and_does_not_create_success(self):
        self.input.rename(self.case / "crash.wav")
        self.input = self.case / "crash.wav"
        with self.assertRaises(WhisperRunError) as caught:
            self.run_task()
        self.assertEqual(caught.exception.exit_code, 17)
        self.assertEqual(caught.exception.workspace, self.workspace())
        self.assertFalse((self.workspace() / "raw.json").exists())
        self.assert_preserved_and_dead()

    def test_changed_input_digest_refuses_before_native_launch(self):
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.run_task(digest="0" * 64)
        self.assertFalse(
            any(
                entry.name.startswith("whisper-")
                for entry in (self.root / "runtime").iterdir()
            )
        )
        self.assertEqual(
            hashlib.sha256(self.input.read_bytes()).hexdigest(), self.digest
        )

    def test_parent_crash_closes_inner_job_before_outer_cleanup(self):
        self.input.rename(self.case / "hang.wav")
        self.input = self.case / "hang.wav"
        request = self.case / "request.json"
        request.write_text(
            json.dumps(
                {
                    "manifest": json.loads(component_manifest_bytes(self.specs)),
                    "root": str(self.root),
                    "wav": str(self.input),
                    "sha256": self.digest,
                    "size": self.input.stat().st_size,
                    "budget": self.budget,
                }
            ),
            encoding="utf-8",
        )
        child_script = self.case / "parent.py"
        child_script.write_text(
            """import json, sys
from pathlib import Path
from creator_loop.app_lock import AppDataLock
from creator_loop.local_components import parse_component_manifest
from creator_loop.whisper_adapter import WhisperSettings, transcribe_whisper_wav
body = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
with AppDataLock(Path(body["root"])) as lock:
    transcribe_whisper_wav(parse_component_manifest(json.dumps(body["manifest"]).encode()),
        Path(body["wav"]), Path(body["root"]), lock, settings=WhisperSettings(body["budget"]),
        expected_sha256=body["sha256"], expected_size=body["size"])
""",
            encoding="utf-8",
        )
        with OwnedWindowsProcess(
            Path(sys._base_executable),
            [str(child_script), str(request)],
            self.case / "parent-log",
            component_version="test/whisper-parent",
        ) as parent:
            deadline = time.perf_counter() + 10
            child = None
            while time.perf_counter() < deadline:
                files = list(
                    (self.root / "runtime").glob("whisper-*/child-ownership.json")
                )
                if files:
                    try:
                        child = json.loads(files[0].read_text())
                    except (ValueError, OSError):
                        pass  # Wait for the before-resume fsynced binding to complete.
                    else:
                        break
                self.assertEqual(
                    parent.kernel.WaitForSingleObject(parent.process, 0), 0x102
                )
                time.sleep(0.01)
            self.assertIsNotNone(child)
            self.assertEqual(child["parent_pid"], parent.record["pid"])
            handle = parent.kernel.OpenProcess(0x00101000, False, child["pid"])
            self.assertTrue(handle)
            try:
                identity = process_identity(parent.kernel, handle)
                self.assertEqual(
                    identity["creation_identity"], child["creation_identity"]
                )
                self.assertEqual(
                    identity["executable"], str(self.runtime / "whisper-cli.exe")
                )
                parent.kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
                parent.kernel.TerminateProcess.restype = w.BOOL
                self.assertTrue(parent.kernel.TerminateProcess(parent.process, 17))
                self.assertEqual(
                    parent.kernel.WaitForSingleObject(parent.process, 5000), 0
                )
                self.assertEqual(parent.kernel.WaitForSingleObject(handle, 5000), 0)
                # Read-only settling: no outer wait/poll/stop to help child cleanup.
                deadline = time.perf_counter() + 5
                while not parent.tree_finished() and time.perf_counter() < deadline:
                    time.sleep(0.01)
                self.assertTrue(parent.tree_finished())
                with AppDataLock(self.root):
                    self.assert_preserved_and_dead()
            finally:
                parent.kernel.CloseHandle(handle)
