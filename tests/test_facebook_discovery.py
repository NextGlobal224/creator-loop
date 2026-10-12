import time
import unittest
from unittest.mock import Mock

from creator_loop.facebook_browser import EXTRACT, LAZY_TIMESTAMPS
from creator_loop.facebook_discovery import DETAIL, discover
from playwright.sync_api import TimeoutError as BrowserTimeoutError
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
    def lazy_timeout_page(self, on_click=None):
        page = Page()
        original_evaluate = page.evaluate
        original_locator = page.locator
        element = Mock()
        element.evaluate.side_effect = lambda script: page.url

        def click(**kwargs):
            if on_click is not None:
                on_click()
            raise BrowserTimeoutError("Timestamp not actionable")

        element.click.side_effect = click

        def evaluate(script, argument=None):
            if script == EXTRACT:
                return [{"url": observation("12")["url"]}]
            if script == LAZY_TIMESTAMPS:
                return [{"index": 0, "href": page.url}]
            return original_evaluate(script, argument)

        page.evaluate = evaluate
        page.locator = lambda selector: (
            Mock(nth=Mock(return_value=element))
            if selector == "a[href]"
            else original_locator(selector)
        )
        return page, element

    def test_unreadable_timestamp_keeps_results_and_consumes_budget_once(self):
        page, element = self.lazy_timeout_page()
        result = discover(
            page,
            settings(max_queries=1, max_candidates=40, max_details=3),
            Mock(),
            lambda: False,
            time.monotonic() + 10,
        )
        self.assertEqual(result["status"], "SUCCEEDED")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["details_used"], 2)
        self.assertEqual(page.detail_reads, ["12"])
        element.click.assert_called_once_with(timeout=5000)

    def test_cancel_during_timestamp_timeout_is_not_swallowed(self):
        cancelled = [False]
        page, element = self.lazy_timeout_page(lambda: cancelled.__setitem__(0, True))
        with self.assertRaises(InterruptedError):
            discover(
                page,
                settings(max_queries=1, max_candidates=40, max_details=3),
                Mock(),
                lambda: cancelled[0],
                time.monotonic() + 10,
            )
        element.click.assert_called_once()

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
