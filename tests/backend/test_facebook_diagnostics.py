import json
import unittest

from creator_loop.backend.facebook.facebook_diagnostics import (
    browser_failure,
    diagnostic_label,
)


class BrowserDiagnosticTests(unittest.TestCase):
    def test_timeout_classification_keeps_sdk_optional_and_hides_messages(self):
        sdk_timeout = type(
            "TimeoutError", (Exception,), {"__module__": "playwright._impl._errors"}
        )
        for error in (TimeoutError("secret"), sdk_timeout("ws://session/token")):
            result = browser_failure(error)
            self.assertEqual(result["status"], "TIMED_OUT")
            self.assertEqual(result["count"], 0)
            self.assertEqual(diagnostic_label(result["diagnostic"]), "TimeoutError")
            self.assertNotIn("secret", json.dumps(result))
            self.assertNotIn("ws://", json.dumps(result))

    def test_failure_never_keeps_exception_payload_or_external_traceback(self):
        try:
            raise ValueError("cookie=private; token=private; https://secret.example")
        except ValueError as error:
            result = browser_failure(error)
        self.assertEqual(result["status"], "BROWSER_FAILED")
        self.assertEqual(result["diagnostic"]["locations"], [])
        self.assertEqual(diagnostic_label(result["diagnostic"]), "ValueError")
        self.assertNotIn("private", json.dumps(result))

    def test_label_rejects_untrusted_diagnostics(self):
        location = "creator_loop.backend.facebook.facebook_discovery.read_detail:123"
        valid = {"exception_type": "Error", "locations": [location]}
        self.assertEqual(diagnostic_label(valid), "Error @ " + location)
        for invalid in (
            {**valid, "url": "https://secret.example"},
            {**valid, "exception_type": "Error: token=secret"},
            {**valid, "locations": ["ws://session/token"]},
            {**valid, "locations": [location] * 7},
        ):
            self.assertEqual(diagnostic_label(invalid), "")

    def test_actual_app_frame_is_retained_without_page_or_exception_text(self):
        from creator_loop.backend.facebook.facebook_browser import session_problem

        class Page:
            url = "https://www.facebook.com/"

            def locator(self, selector):
                raise RuntimeError("ws://private-session?token=secret")

        try:
            session_problem(Page())
        except RuntimeError as error:
            result = browser_failure(error)
        label = diagnostic_label(result["diagnostic"])
        self.assertIn("facebook_browser.session_problem:", label)
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("ws://", json.dumps(result))
