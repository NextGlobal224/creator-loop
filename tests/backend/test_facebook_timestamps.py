import time
import unittest
from unittest.mock import Mock, patch

from creator_loop.backend.facebook.facebook_browser import resolve_search_timestamps


class FacebookTimestampTests(unittest.TestCase):
    def page(self):
        page = Mock()
        page.url = "https://www.facebook.com/search/videos/?q=fixture"
        candidate = {
            "index": 3,
            "href": "https://www.facebook.com/search/videos/?__cft__[0]=fixture",
        }
        page.evaluate.return_value = [candidate]
        element = page.locator.return_value.nth.return_value
        element.evaluate.return_value = candidate["href"]
        previous = page.url
        element.click.side_effect = lambda **_: setattr(
            page,
            "url",
            "https://www.facebook.com/groups/23/?multi_permalinks=45&hoisted_section_header_type=recently_seen",
        )
        page.go_back.side_effect = lambda **_: setattr(page, "url", previous)
        return page, element

    def resolve(self, page, found, seen, cancelled=lambda: False):
        with patch(
            "creator_loop.backend.facebook.facebook_browser.session_problem",
            return_value=None,
        ):
            resolve_search_timestamps(
                page, found, seen, 20, cancelled, time.monotonic() + 10, lambda: True
            )

    def test_timestamp_navigation_resolves_group_post_without_fabricating_text(self):
        page, element = self.page()
        found, seen = {}, set()
        self.resolve(page, found, seen)
        self.assertEqual(
            found,
            {
                "45": {
                    "url": "https://www.facebook.com/groups/23/posts/45",
                    "excerpt": None,
                    "publisher": None,
                }
            },
        )
        element.click.assert_called_once_with(timeout=5000)
        page.go_back.assert_called_once_with(
            wait_until="domcontentloaded", timeout=15000
        )
        self.resolve(page, found, seen)
        element.click.assert_called_once()
        page.close.assert_not_called()

    def test_stale_candidate_never_clicked(self):
        page, element = self.page()
        element.evaluate.return_value = "https://www.facebook.com/profile.php?id=99"
        found = {}
        self.resolve(page, found, set())
        self.assertEqual(found, {})
        element.click.assert_not_called()

    def test_cancel_during_navigation_stops_before_further_actions_or_import(self):
        page, element = self.page()
        found = {}
        with self.assertRaises(InterruptedError):
            self.resolve(page, found, set(), lambda: element.click.called)
        self.assertEqual(found, {})
        page.go_back.assert_not_called()
        page.close.assert_not_called()
