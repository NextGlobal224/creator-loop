"""Actual owned private review/SQL, GUI heartbeat, budgets and cancellation."""

import json
import os
import sqlite3
import sys
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

if sys.platform == "win32":
    import test_transcript_evidence as fixture
    from creator_loop.app_lock import AppDataLock
    from creator_loop.owned_process import OwnedWindowsProcess
    from creator_loop.private_pipe import PrivatePipe
    from creator_loop.runtime_ownership import bind_workspace_child
    from creator_loop.transcript_review_command import TranscriptReviewCommand
    from creator_loop.transcript_review_task import MAX_RESPONSE
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication


@unittest.skipUnless(sys.platform == "win32", "actual Windows owned private review")
class TranscriptReviewTaskTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        self.app = QApplication.instance() or QApplication([])
        self.fixture = fixture.TranscriptEvidenceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.command = TranscriptReviewCommand(self.root)
        self.addCleanup(self.command.shutdown)
        self.results, self.failures, self.pulses = [], [], []
        self.command.finished.connect(
            lambda code, text: self.results.append((code, json.loads(text)))
        )
        self.command.failed.connect(self.failures.append)
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: self.pulses.append(time.perf_counter()))
        timer.start()
        self.timer = timer
        self.addCleanup(timer.stop)

    def until(self, predicate, timeout=10):
        deadline = time.perf_counter() + timeout
        while not predicate() and time.perf_counter() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertTrue(predicate(), str(self.failures))

    def run_review(self, **kwargs):
        self.command.start_review(self.fixture.raw_id, **kwargs)
        self.until(lambda: not self.command.busy)
        self.assertFalse(self.failures, self.failures)
        self.assertEqual(self.command.received, b"")
        return self.results[-1]

    def query(self, sql):
        with closing(sqlite3.connect(self.fixture.db_path)) as db:
            return db.execute(sql).fetchall()

    def private_child(self, script):
        self.command.request_id, self.command.action = "a" * 32, "segment"
        self.command.received.clear()
        self.command.pipe = pipe = PrivatePipe(eager_reads=True)
        self.command._launch(
            Path(getattr(sys, "_base_executable", sys.executable)),
            ["-u", "-c", script],
            {**os.environ, "PYTHONUTF8": "1"},
            timeout_seconds=2,
            memory_limit_bytes=128 * 1024**2,
            capture_output=False,
            stdout_sink=pipe.writer,
        )
        self.command.process.streams[1].close()
        pipe.writer.close()

    def test_page_segment_and_explicit_create_remain_private_model_pending(self):
        code, page = self.run_review()
        self.assertEqual(code, 0)
        self.assertEqual(page["data"]["segments"][0]["preview"], " Lời máy chưa duyệt ")
        self.fixture._assert_no_evidence()
        code, segment = self.run_review(
            action="segment", raw_sha256=self.fixture.raw_digest
        )
        self.assertEqual(code, 0)
        self.assertEqual(segment["data"]["text"], " Lời máy chưa duyệt ")
        self.fixture._assert_no_evidence()
        code, receipt = self.run_review(
            action="create", raw_sha256=self.fixture.raw_digest
        )
        self.assertEqual(code, 0)
        self.assertEqual(receipt["data"]["producer_type"], "MODEL")
        rows = self.query(
            "SELECT producer_type,anchor_file_id,processing_run_id,content FROM evidence_versions"
        )
        self.assertEqual(
            rows,
            [
                (
                    "MODEL",
                    self.fixture.original_id,
                    self.fixture.run_id,
                    " Lời máy chưa duyệt ",
                )
            ],
        )
        self.assertEqual(self.query("SELECT COUNT(*) FROM review_events"), [(0,)])
        self.assertGreater(len(self.pulses), 5)
        for logs in (self.root / "logs").glob("transcript-review-*"):
            self.assertEqual({p.name for p in logs.iterdir()}, {"ownership.json"})
            self.assertNotIn("Lời máy", (logs / "ownership.json").read_text())
        for workspace in (self.root / "runtime").glob("transcript-review-*"):
            self.assertEqual(
                {p.name for p in workspace.iterdir()},
                {"request.json", "ownership.json", "child-ownership.json"},
            )
        with AppDataLock(self.root):
            pass  # fresh root lock is available only after native cleanup

    def test_changed_raw_digest_refuses_create_without_evidence(self):
        code, result = self.run_review(action="create", raw_sha256="0" * 64)
        self.assertEqual(code, 4)
        self.assertEqual(result["status"], "FAILED")
        self.fixture._assert_no_evidence()

    def test_preview_decodes_actual_original_range_without_evidence_or_text_logs(self):
        code, body = self.run_review(
            action="preview", raw_sha256=self.fixture.raw_digest
        )
        self.assertEqual(code, 0)
        data = body["data"]
        self.assertEqual((data["start_ms"], data["end_ms"]), (0, 90))
        decoded = data["decoded"]
        self.assertEqual(
            decoded["anchor_path"], str(self.fixture.paths[self.fixture.original_id])
        )
        self.assertEqual(decoded["data_root"], str(self.root))
        self.assertGreater(decoded["duration_ms"], 90)
        self.assertLess(decoded["buffer_start_ms"], 90)
        self.assertGreater(decoded["buffer_end_ms"], 0)
        self.fixture._assert_no_evidence()

    def test_root_lock_busy_is_a_refusal_not_a_second_writer(self):
        with AppDataLock(self.root):
            code, result = self.run_review()
            self.assertEqual(code, 3)
            self.assertEqual(result["error"], "DATA_ROOT_BUSY")
        self.fixture._assert_no_evidence()

    def test_native_child_identity_mismatch_refuses_before_read_or_create(self):
        def wrong_binding(workspace, marker, child):
            bind_workspace_child(workspace, marker, {**child, "pid": child["pid"] + 1})

        with patch(
            "creator_loop.transcript_review_command.bind_workspace_child",
            side_effect=wrong_binding,
        ):
            code, result = self.run_review(
                action="create", raw_sha256=self.fixture.raw_digest
            )
        self.assertEqual(code, 4)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"], "ValueError")
        self.fixture._assert_no_evidence()

    def test_partial_unicode_frame_keeps_gui_alive_and_never_copies_text_to_log(self):
        body = {
            "format": 1,
            "request_id": "a" * 32,
            "action": "segment",
            "status": "SUCCEEDED",
            "data": {"text": "PRIVATE_á中🙂"},
        }
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.private_child(
            f"import sys,time; p={payload!r}; sys.stdout.buffer.write(p[:11]); sys.stdout.buffer.flush(); time.sleep(.3);sys.stdout.buffer.write(p[11:]);sys.stdout.buffer.flush();sys.stderr.write('PRIVATE_STDERR')"
        )
        self.until(lambda: not self.command.busy)
        self.assertFalse(self.failures)
        self.assertEqual(self.results[-1], (0, body))
        self.assertGreater(len(self.pulses), 10)
        self.assertEqual(
            {p.name for p in self.command.log_directory.iterdir()}, {"ownership.json"}
        )

    def test_over_budget_output_is_refused_with_owned_tree_and_pipe_settled(self):
        self.private_child(
            f"import sys;sys.stdout.buffer.write(b'x'*{MAX_RESPONSE + 1});sys.stdout.buffer.flush()"
        )
        self.until(lambda: not self.command.busy)
        self.assertTrue(self.failures)
        self.assertFalse(self.results)
        self.assertIsNone(self.command.pipe)
        self.assertEqual(self.command.received, b"")

    def test_cancel_exact_child_keeps_unrelated_process_and_native_cleanup_proven(self):
        with OwnedWindowsProcess(
            Path(sys.executable),
            ["-c", "import time;time.sleep(30)"],
            self.root / "logs" / "sentinel",
            component_version="test/unrelated",
        ) as sentinel:
            self.private_child("import time;time.sleep(30)")
            self.until(lambda: self.command.pipe.read_pending)
            self.command.cancel()
            self.until(lambda: not self.command.busy)
            self.assertTrue(self.failures)
            self.assertIsNone(self.command.pipe)
            self.assertEqual(
                sentinel.kernel.WaitForSingleObject(sentinel.process, 0), 258
            )

    def test_pipe_pending_retains_native_process_and_root_until_completion(self):
        self.private_child("import time;time.sleep(.1)")
        self.until(lambda: self.command.process.tree_finished())
        pipe, process = self.command.pipe, self.command.process
        with patch.object(pipe, "try_close", return_value=False):
            for _ in range(3):
                self.command._poll()
            self.assertIs(self.command.pipe, pipe)
            self.assertIs(self.command.process, process)
            self.assertTrue(self.command.busy)
        self.until(lambda: not self.command.busy)
        self.assertIsNone(self.command.pipe)
        self.assertTrue(self.failures)  # empty frame is never accepted as a receipt

    def test_worker_deadline_retires_idle_tree_without_blocking_gui(self):
        self.private_child("import time;time.sleep(30)")
        self.command.deadline = time.perf_counter() + 0.15
        self.until(lambda: not self.command.busy)
        self.assertTrue(self.failures)
        self.assertFalse(self.results)
        self.assertIsNone(self.command.pipe)
        self.assertGreater(len(self.pulses), 5)

    def test_duplicate_or_wrong_request_frame_is_not_a_creation_receipt(self):
        for payload in (
            b'{"format":1,"format":1,"request_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","action":"segment","status":"SUCCEEDED"}',
            b'{"format":1,"request_id":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","action":"segment","status":"SUCCEEDED"}',
        ):
            self.private_child(
                f"import sys;sys.stdout.buffer.write({payload!r});sys.stdout.buffer.flush()"
            )
            self.until(lambda: not self.command.busy)
            self.assertTrue(self.failures)
            self.assertFalse(self.results)
            self.assertEqual(self.command.received, b"")


if __name__ == "__main__":
    unittest.main()
