import time
import unittest
from unittest.mock import Mock

from creator_loop.facebook_browser import EXTRACT
from creator_loop.facebook_discovery import DETAIL, discover
from test_facebook_research import observation, settings


class Page:
    def __init__(self):
        self.url = "https://www.facebook.com/"
        self.history = []
        self.mouse = Mock()
        self.detail_reads = []

    def goto(self, url, **kwargs):
        self.history.append(self.url)
        self.url = url

    def go_back(self, **kwargs):
        self.url = self.history.pop()

    def wait_for_timeout(self, duration):
        pass

    def is_closed(self):
        return False

    def locator(self, selector):
        locator = Mock()
        locator.count.return_value = 0
        return locator

    def evaluate(self, script, argument=None):
        if script == EXTRACT:
            return [
                {"url": observation(str(n))["url"], "excerpt": None, "publisher": None}
                for n in range(12, 18)
            ]
        if script == DETAIL:
            self.detail_reads.append(argument)
            return observation(argument)
        return []


class DiscoveryTests(unittest.TestCase):
    def test_candidate_detail_and_query_limits_apply_globally(self):
        page = Page()
        result = discover(
            page,
            settings(max_candidates=4, max_details=2),
            Mock(),
            lambda: False,
            time.monotonic() + 10,
        )
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["details_used"], 2)
        self.assertEqual(len(page.detail_reads), 2)
        self.assertLessEqual(len(result["queries"]), 4)
        self.assertEqual(
            sum(
                row["assessment"]["state"] == "NEEDS_REVIEW"
                for row in result["candidates"]
            ),
            2,
        )

    def test_hashtag_expansion_is_sourced_and_duplicates_not_reopened(self):
        page = Page()
        result = discover(
            page,
            settings(max_queries=2, max_candidates=40, max_details=20),
            Mock(),
            lambda: False,
            time.monotonic() + 10,
        )
        self.assertEqual(len(result["queries"]), 2)
        self.assertEqual(result["queries"][1]["kind"], "OBSERVED_HASHTAG")
        self.assertTrue(result["queries"][1]["source_urls"])
        self.assertIn("#python", result["queries"][1]["text"])
        self.assertEqual(result["count"], 6)
        self.assertEqual(len(page.detail_reads), len(set(page.detail_reads)))

    def test_cancel_before_navigation_keeps_browser_and_never_reads_details(self):
        page = Page()
        with self.assertRaises(InterruptedError):
            discover(page, settings(), Mock(), lambda: True, time.monotonic() + 10)
        self.assertEqual(page.url, "https://www.facebook.com/")
        self.assertEqual(page.detail_reads, [])
