"""Periodic reporter captures blocked frames without native frame-pointer races."""

import io
import threading
import time
import types
import unittest

from scripts.run_suite_with_diagnostics import periodic_python_stacks


class SuiteDiagnosticsTests(unittest.TestCase):
    def test_real_output_failure_is_reported_instead_of_silent_loss_of_diagnostics(
        self,
    ):
        output = io.StringIO()
        output.close()
        with self.assertRaisesRegex(RuntimeError, "Periodic stack reporting failed"):
            with periodic_python_stacks(output, interval=0.005):
                time.sleep(0.05)
        self.assertFalse(
            any(
                thread.name == "suite-stack-reporter"
                for thread in threading.enumerate()
            )
        )

    def test_reports_blocked_worker_and_stops_before_output_closes(self):
        stopped = threading.Event()
        entered = threading.Event()

        def blocked_diagnostic_worker():
            entered.set()
            stopped.wait(2)

        worker = threading.Thread(target=blocked_diagnostic_worker)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(stopped.set)
        self.assertTrue(entered.wait(1))
        output = io.StringIO()
        with periodic_python_stacks(output, interval=0.005):
            deadline = time.monotonic() + 2
            while (
                "blocked_diagnostic_worker" not in output.getvalue()
                and time.monotonic() < deadline
            ):
                time.sleep(0.005)
        self.assertIn("blocked_diagnostic_worker", output.getvalue())
        self.assertIn("requires GIL", output.getvalue())
        before = output.getvalue()
        time.sleep(0.03)
        self.assertEqual(output.getvalue(), before)
        self.assertFalse(
            any(
                thread.name == "suite-stack-reporter"
                for thread in threading.enumerate()
            )
        )
        output.close()

    def test_dynamic_code_churn_remains_alive_and_exception_stops_reporter(self):
        stopped = threading.Event()

        def template():
            return 1

        def churn():
            while not stopped.is_set():
                function = types.FunctionType(template.__code__.replace(), {})
                function()

        worker = threading.Thread(target=churn)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(stopped.set)
        output = io.StringIO()
        with self.assertRaisesRegex(ValueError, "fixture failure"):
            with periodic_python_stacks(output, interval=0.005):
                time.sleep(0.15)
                raise ValueError("fixture failure")
        self.assertIn("Periodic Python stacks", output.getvalue())
        self.assertTrue(worker.is_alive())
        self.assertFalse(
            any(
                thread.name == "suite-stack-reporter"
                for thread in threading.enumerate()
            )
        )
