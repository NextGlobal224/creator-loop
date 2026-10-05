"""Real Qt PCM conversion under a bounded native Job, without engine/model."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path

from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.publication_media import _open_read_lock


@unittest.skipUnless(sys.platform == "win32", "Windows owned Qt decoder")
class PCMDecodeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name).resolve()
        self.repo = Path(__file__).resolve().parents[1]
        self.script = self.folder / "child.py"
        self.script.write_text(
            """import json,sys
from dataclasses import asdict
from PySide6.QtCore import QCoreApplication,QFile,QIODevice
from creator_loop.pcm_decode import decode_pcm_local
app=QCoreApplication([])
source=QFile()
assert source.open(0,QIODevice.OpenModeFlag.ReadOnly,QFile.FileHandleFlag.DontCloseHandle)
source.seek(0)
settings=json.loads(sys.argv[2])
cancel_after=settings.pop('cancel_after',None)
timeline_shift=settings.pop('timeline_shift',None)
if timeline_shift is not None:
    import creator_loop.pcm_decode as module
    from PySide6.QtMultimedia import QAudioBuffer,QAudioDecoder
    class ShiftedDecoder(QAudioDecoder):
        def read(self):
            buffer=super().read()
            if buffer.isValid() and buffer.startTime()>0:
                return QAudioBuffer(bytes(buffer.constData()),buffer.format(),buffer.startTime()+timeline_shift)
            return buffer
    module.QAudioDecoder=ShiftedDecoder
checks=0
def cancelled():
    global checks
    checks+=1
    return cancel_after is not None and checks>=cancel_after
try:
    with open(sys.argv[1],'xb') as output:
        result=decode_pcm_local(source,output,cancelled=cancelled,**settings)
    print(json.dumps({'status':'PASS',**asdict(result)}))
except Exception as error:
    print(json.dumps({'status':'REFUSED','error_type':type(error).__name__,'error':str(error)}))
finally:
    source.close()
""",
            encoding="utf-8",
        )

    def _run(self, fixture="video-with-tone.mp4", **settings):
        original = self.repo / "tests/fixtures" / fixture
        before = hashlib.sha256(original.read_bytes()).hexdigest()
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(self.repo / "app")
        environment["QT_QPA_PLATFORM"] = "offscreen"
        with _open_read_lock(original) as held:
            with OwnedWindowsProcess(
                Path(sys.executable),
                [
                    str(self.script),
                    str(self.folder / "audio.wav"),
                    json.dumps(settings),
                ],
                self.folder / "logs",
                component_version="test/Qt-PCM",
                environment=environment,
                stdin_source=held,
                memory_limit_bytes=512 * 1024**2,
            ) as child:
                outcome = child.wait(20)
                self.assertFalse(outcome.timed_out)
                self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), before)
        rows = (self.folder / "logs/stdout.log").read_text().splitlines()
        return json.loads(rows[-1])

    def test_tone_decodes_to_bounded_pcm_with_original_unchanged(self):
        result = self._run()
        self.assertEqual(result["status"], "PASS")
        with wave.open(str(self.folder / "audio.wav"), "rb") as wav:
            self.assertEqual(
                (wav.getnchannels(), wav.getsampwidth(), wav.getframerate()),
                (1, 2, 16000),
            )
            self.assertEqual(wav.getnframes(), result["frames"])
            samples = wav.readframes(wav.getnframes())
            self.assertTrue(any(samples))
        self.assertGreater(result["buffers"], 1)
        self.assertTrue(900 <= result["duration_ms"] <= 1000)

    def test_missing_audio_refuses_without_success(self):
        result = self._run("video-red-blue.mp4")
        self.assertEqual(result["status"], "REFUSED")
        self.assertEqual(result["error_type"], "ValueError")

    def test_pcm_budget_refuses_instead_of_truncating_success(self):
        result = self._run(max_frames=1)
        self.assertEqual(result["status"], "REFUSED")
        self.assertIn("budget", result["error"])
        self.assertLessEqual((self.folder / "audio.wav").stat().st_size, 46)

    def test_cancel_refuses_and_preserves_input(self):
        result = self._run(cancel_after=3)
        self.assertEqual(result["status"], "REFUSED")
        self.assertEqual(result["error_type"], "InterruptedError")

    def test_decoder_deadline_refuses_and_owned_tree_exits(self):
        result = self._run(timeout_seconds=0.000001)
        self.assertEqual(result["status"], "REFUSED")
        self.assertEqual(result["error_type"], "TimeoutError")

    def test_invalid_boolean_budget_refuses_before_decode(self):
        result = self._run(max_frames=True)
        self.assertEqual(result["status"], "REFUSED")
        self.assertEqual(result["error_type"], "ValueError")

    def test_native_buffer_gap_is_not_silently_stitched(self):
        result = self._run(timeline_shift=5000)
        self.assertEqual(result["status"], "REFUSED")
        self.assertIn("timeline", result["error"])

    def test_native_buffer_overlap_is_not_silently_clipped(self):
        result = self._run(timeline_shift=-5000)
        self.assertEqual(result["status"], "REFUSED")
        self.assertIn("timeline", result["error"])
