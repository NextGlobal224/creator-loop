import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from creator_loop.backend.analysis.workers_transport import (
    NoRedirect,
    post_owned,
    save_quality_rejection,
)
from creator_loop.backend.runtime.owned_process import OwnedWindowsProcess
from creator_loop.backend.runtime.runtime_ownership import create_workspace_marker


class WorkersBoundaryTests(unittest.TestCase):
    def test_known_stage_failure_retains_bounded_generated_text_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = "a" * 64
            packet = {
                "error": "WORKERS_AI_FAILED",
                "reason": "STAGE_OUTPUT_INVALID",
                "stage": "editing",
                "stage_output": "generated text " + key,
                "retryable": False,
                "private_exception": "do not keep",
            }
            save_quality_rejection(root, 502, json.dumps(packet).encode(), key)
            value = json.loads(
                (root / "quality-rejection.json").read_text(encoding="utf-8")
            )
            self.assertEqual(value["stage"], "editing")
            self.assertEqual(value["stage_output"], "generated text [REDACTED]")
            self.assertNotIn("private_exception", value)

    def test_only_bounded_known_quality_diagnostic_is_retained_without_credential(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            key = "a" * 64
            packet = {
                "error": "QUALITY_REJECTED",
                "reason": "SOURCE_AUDIT_FAILED",
                "retryable": False,
                "candidate_body": "Unsupported output " + key,
                "audit_issues": ["Missing source " + key],
                "arbitrary_private_error": key,
            }
            save_quality_rejection(root, 422, json.dumps(packet).encode(), key)
            raw = (root / "quality-rejection.json").read_text(encoding="utf-8")
            self.assertNotIn(key, raw)
            self.assertNotIn("arbitrary_private_error", raw)
            report = json.loads(raw)
            self.assertEqual(report["reason"], "SOURCE_AUDIT_FAILED")
            self.assertIn("[REDACTED]", report["candidate_body"])
            original = raw
            for status, payload in [
                (403, packet),
                (422, {**packet, "reason": "untrusted"}),
                (422, {**packet, "reason": {}}),
                (422, {"error": key}),
            ]:
                save_quality_rejection(root, status, json.dumps(payload).encode(), key)
                self.assertEqual(
                    (root / "quality-rejection.json").read_text(encoding="utf-8"),
                    original,
                )
            save_quality_rejection(root, 422, b"x" * (64 * 1024 + 1), key)
            self.assertEqual(
                (root / "quality-rejection.json").read_text(encoding="utf-8"), original
            )

    def test_redirect_never_forwards_credential(self):
        with self.assertRaisesRegex(ValueError, "redirect refused"):
            NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://other.example"
            )

    @unittest.skipUnless(sys.platform == "win32", "Owned Windows network worker")
    def test_deadline_and_cancel_terminate_only_owned_blocked_child(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                workspace = root / "runtime/creator-workers-fixture"
                workspace.mkdir(parents=True)
                marker = create_workspace_marker(
                    root, workspace, "fixture", kind="CREATOR_LLM_WORKSPACE"
                )
                processes = []

                def launch(executable, arguments, log_directory, **kwargs):
                    process = OwnedWindowsProcess(
                        Path(sys.executable),
                        ["-c", "import time;time.sleep(10)"],
                        log_directory,
                        **kwargs,
                    )
                    processes.append(process)
                    return process

                start = time.monotonic()

                def cancelled():
                    return cancel and time.monotonic() - start > 0.2

                error = InterruptedError if cancel else TimeoutError
                with patch(
                    "creator_loop.backend.analysis.workers_transport.OwnedWindowsProcess",
                    side_effect=launch,
                ):
                    with self.assertRaises(error):
                        post_owned(
                            workspace,
                            marker,
                            "https://fixture.workers.dev/b/generate",
                            b"{}",
                            "private-key",
                            cancelled,
                            timeout_seconds=0.4,
                        )
                self.assertLess(time.monotonic() - start, 3)
                self.assertTrue(processes[0].tree_finished())
                for path in workspace.rglob("*"):
                    if path.is_file():
                        self.assertNotIn(b"private-key", path.read_bytes())
                child = json.loads((workspace / "child-ownership.json").read_text())
                self.assertEqual(child["component_version"], "fixture")
