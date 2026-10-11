from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from creator_loop.facebook_browser import SessionRequired, collect
from creator_loop.facebook_cdp import (
    ConnectionRequired,
    attached_operation,
    chrome_endpoint,
    facebook_tabs,
    is_facebook_url,
)


class FacebookCdpTests(unittest.TestCase):
    def page(self, url, target="facebook-tab", target_url=None):
        page = Mock()
        page.url = url
        page.is_closed.return_value = False
        page.title.return_value = "Facebook"
        page.locator.return_value.count.return_value = 1
        page.target_info = {
            "targetId": target,
            "type": "page",
            "url": target_url or url,
        }
        return page

    def playwright(self, *pages):
        context = Mock()
        context.pages = pages
        sessions = []

        def session(page):
            item = Mock()
            item.send.return_value = {"targetInfo": page.target_info}
            sessions.append(item)
            return item

        context.new_cdp_session.side_effect = session
        browser = Mock()
        browser.contexts = [context]
        pw = Mock()
        pw.chromium.connect_over_cdp.return_value = browser
        return pw, browser, context, sessions

    def operation(self, pw, mode="connect", target=None, cancel=lambda: False):
        request = {
            "mode": mode,
            "target_id": target,
            "keyword": "fixture",
            "limit": 20,
            "url": "https://www.facebook.com/reel/12",
        }
        with patch(
            "creator_loop.facebook_cdp.chrome_endpoint",
            return_value="ws://127.0.0.1:9222/devtools/browser/abcdefgh",
        ):
            return attached_operation(
                pw, request, Mock(), cancel, time.monotonic() + 10
            )

    def test_descriptor_confines_endpoint_to_loopback_without_url_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            descriptor = Path(temporary) / "DevToolsActivePort"
            descriptor.write_text(
                "9222\n/devtools/browser/abcdefgh\n", encoding="utf-8"
            )
            self.assertEqual(
                chrome_endpoint(descriptor),
                "ws://127.0.0.1:9222/devtools/browser/abcdefgh",
            )
            for body in (
                "0\n/devtools/browser/abcdefgh",
                "65536\n/devtools/browser/abcdefgh",
                "9222\nws://evil.test/devtools/browser/abcdefgh",
                "9222\n/devtools/browser/abcdefgh?token=secret",
                "x" * 4097,
            ):
                descriptor.write_text(body, encoding="utf-8")
                with self.assertRaises(ConnectionRequired):
                    chrome_endpoint(descriptor)
            descriptor.unlink()
            with self.assertRaises(ConnectionRequired):
                chrome_endpoint(descriptor)

    def test_connect_checks_session_once_and_never_closes_or_changes_existing_pages(
        self,
    ):
        facebook = self.page("https://www.facebook.com/")
        other = self.page("https://unrelated.test/private")
        pw, browser, context, sessions = self.playwright(other, facebook)
        with patch(
            "creator_loop.facebook_browser.session_problem", return_value=None
        ) as auth:
            result = self.operation(pw)
        self.assertEqual(
            result, {"status": "SESSION_READY", "count": 0, "target_id": "facebook-tab"}
        )
        auth.assert_called_once_with(facebook)
        pw.chromium.connect_over_cdp.assert_called_once_with(
            "ws://127.0.0.1:9222/devtools/browser/abcdefgh",
            timeout=30000,
            no_defaults=True,
        )
        browser.close.assert_not_called()
        context.close.assert_not_called()
        context.new_page.assert_not_called()
        facebook.goto.assert_not_called()
        facebook.close.assert_not_called()
        other.title.assert_not_called()
        other.locator.assert_not_called()
        self.assertEqual(len(sessions), 1)
        sessions[0].detach.assert_called_once()

    def test_multiple_facebook_tabs_require_explicit_selection_before_auth_or_scan(
        self,
    ):
        first = self.page("https://www.facebook.com/", "first")
        second = self.page("https://www.facebook.com/groups/12/", "second")
        pw, _, _, _ = self.playwright(first, second)
        with patch("creator_loop.facebook_browser.session_problem") as auth:
            result = self.operation(pw)
        self.assertEqual(result["status"], "SELECT_TAB")
        auth.assert_not_called()
        with patch(
            "creator_loop.facebook_browser.session_problem", return_value=None
        ) as auth:
            result = self.operation(pw, target="second")
        self.assertEqual(result["target_id"], "second")
        auth.assert_called_once_with(second)
        with patch("creator_loop.facebook_browser.session_problem") as auth:
            result = self.operation(pw, target="stale-tab")
        self.assertEqual(result["status"], "FACEBOOK_TAB_REQUIRED")
        auth.assert_not_called()

    def test_redirected_or_spoofed_facebook_target_never_read(self):
        for url in (
            "https://facebook.com.evil.test/",
            "http://facebook.com/",
            "https://facebook.com@evil.test/",
            "https://facebook.com:9222/",
        ):
            self.assertFalse(is_facebook_url(url))
        facebook = self.page(
            "https://www.facebook.com/", target_url="https://unrelated.test/"
        )
        pw, browser, _, sessions = self.playwright(facebook)
        self.assertEqual(facebook_tabs(browser), [])
        facebook.title.assert_not_called()
        sessions[0].detach.assert_called_once()
        self.assertEqual(self.operation(pw)["status"], "FACEBOOK_TAB_REQUIRED")

    def test_cancel_and_auth_guard_do_not_close_user_browser(self):
        facebook = self.page("https://www.facebook.com/checkpoint/")
        pw, browser, context, _ = self.playwright(facebook)
        with patch(
            "creator_loop.facebook_browser.session_problem", return_value="CAPTCHA"
        ):
            with self.assertRaisesRegex(SessionRequired, "CAPTCHA"):
                self.operation(pw)
        with self.assertRaises(InterruptedError):
            self.operation(pw, cancel=lambda: True)
        browser.close.assert_not_called()
        context.close.assert_not_called()
        facebook.close.assert_not_called()
        facebook.goto.assert_not_called()

    def test_open_source_keeps_user_browser_and_rejects_non_permalink(self):
        facebook = self.page("https://www.facebook.com/")
        pw, browser, context, _ = self.playwright(facebook)
        with patch("creator_loop.facebook_browser.session_problem", return_value=None):
            result = self.operation(pw, mode="open", target="facebook-tab")
        self.assertEqual(result["status"], "OPENED_KEEP_BROWSER")
        facebook.goto.assert_called_once_with(
            "https://www.facebook.com/reel/12",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        browser.close.assert_not_called()
        context.close.assert_not_called()
        facebook.close.assert_not_called()

    def test_scanner_stops_before_touching_a_tab_that_left_facebook(self):
        page = self.page("https://unrelated.test/")
        with self.assertRaisesRegex(SessionRequired, "FACEBOOK_TAB_REQUIRED"):
            collect(
                page,
                "fixture",
                20,
                Mock(),
                lambda: False,
                time.monotonic() + 10,
                scope_check=lambda: is_facebook_url(page.url),
            )
        page.goto.assert_not_called()
        page.locator.assert_not_called()
        page.evaluate.assert_not_called()

    def test_missing_auth_guard_alone_does_not_confirm_a_session(self):
        facebook = self.page("https://www.facebook.com/")
        facebook.locator.return_value.count.return_value = 0
        pw, browser, _, _ = self.playwright(facebook)
        with patch("creator_loop.facebook_browser.session_problem", return_value=None):
            result = self.operation(pw)
        self.assertEqual(result["status"], "SESSION_UNCONFIRMED")
        browser.close.assert_not_called()
        facebook.goto.assert_not_called()
