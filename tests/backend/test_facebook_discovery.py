import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from creator_loop.backend.facebook.facebook_browser import EXTRACT, LAZY_TIMESTAMPS
from creator_loop.backend.facebook.facebook_discovery import (
    DETAIL,
    METRIC_SCROLL,
    discover,
    read_detail,
)
from creator_loop.backend.facebook.facebook_research import validate_candidate

from tests.backend.test_facebook_research import observation, settings


class BrowserTimeoutError(Exception):
    pass


TIMEOUT_SDK = {"playwright.sync_api": SimpleNamespace(TimeoutError=BrowserTimeoutError)}


class Page:
    def __init__(self):
        self.url = "https://www.facebook.com/"
        self.history = []
        self.mouse = Mock()
        self.detail_reads = []
        self.scrolled = False

    def goto(self, url, **kwargs):
        self.history.append(self.url)
        self.url = url
        self.scrolled = False

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
            if not self.scrolled:
                self.detail_reads.append(argument)
            return observation(argument)
        if script == METRIC_SCROLL:
            self.scrolled = True
            return True
        return []


class DiscoveryTests(unittest.TestCase):
    def test_viral_skips_off_topic_and_reuses_sufficient_metadata(self):
        page = Page()
        original = page.evaluate
        page.evaluate = lambda script, arg=None: (
            [
                observation("12", metrics={"likes": "1500 likes"}),
                observation("13"),
                observation("14", excerpt="Java", metrics={"likes": "9000 likes"}),
            ]
            if script == EXTRACT
            else original(script, arg)
        )
        result = discover(
            page,
            settings(mode="VIRAL", max_queries=1),
            Mock(),
            lambda: False,
            time.monotonic() + 10,
        )
        self.assertEqual(page.detail_reads, ["13"])
        self.assertEqual(result["details_used"], 1)
        self.assertEqual(
            sum(
                c["assessment"]["topical"] is True and c["assessment"]["viral"] is True
                for c in result["candidates"]
            ),
            1,
        )

    def test_viral_stops_at_forty_unique_qualified_without_detail_reopening(self):
        page = Page()
        original = page.evaluate
        page.evaluate = lambda script, arg=None: (
            [
                observation(str(n), metrics={"likes": "1500 likes"})
                for n in range(100, 140)
            ]
            if script == EXTRACT
            else original(script, arg)
        )
        result = discover(
            page,
            settings(
                mode="VIRAL",
                queries=["Python", "Python guide"],
                max_candidates=160,
                max_details=80,
            ),
            Mock(),
            lambda: False,
            time.monotonic() + 10,
        )
        self.assertEqual(result["count"], 40)
        self.assertEqual(len(result["queries"]), 1)
        self.assertEqual(result["details_used"], 0)

    def test_detail_waits_for_late_caption_and_keeps_it_after_metric_scroll(self):
        page = Page()
        page.url = observation()["url"]
        reads = [
            {"url": page.url, "excerpt": None, "metrics": {}},
            observation(published_label="10 tháng 10, 2025"),
            {
                "url": page.url,
                "excerpt": None,
                "metrics": {"likes": "Thích: 488 người"},
            },
        ]
        original = page.evaluate
        page.evaluate = lambda script, arg: (
            reads.pop(0) if script == DETAIL else original(script, arg)
        )
        raw = read_detail(page, page.url, Mock())
        self.assertEqual(raw["excerpt"], observation()["excerpt"])
        self.assertEqual(raw["published_label"], "10 tháng 10, 2025")
        self.assertEqual(raw["metrics"]["likes"], "Thích: 488 người")
        self.assertEqual(reads, [])

    def test_detail_wait_is_finite_and_does_not_copy_other_post(self):
        page = Page()
        page.url = observation()["url"]
        page.evaluate = Mock(return_value=observation("99"))
        raw = read_detail(page, page.url, Mock())
        self.assertEqual(page.evaluate.call_count, 8)
        self.assertNotIn("excerpt", raw)
        self.assertEqual(raw["url"], observation()["url"])

    def test_flat_rescan_does_not_erase_rich_caption_hashtags_or_metrics(self):
        page = Page()
        result = discover(
            page,
            settings(max_queries=1, max_details=6),
            Mock(),
            lambda: False,
            time.monotonic() + 10,
        )
        for item in result["candidates"]:
            validate_candidate(item, settings(max_queries=1, max_details=6))
            self.assertTrue(item["excerpt"])
            self.assertEqual(item["hashtags"], ["#Python"])
            self.assertEqual(item["scope"], "VISIBLE_TEXT")

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
        with patch.dict("sys.modules", TIMEOUT_SDK):
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
        with (
            patch.dict("sys.modules", TIMEOUT_SDK),
            self.assertRaises(InterruptedError),
        ):
            discover(
                page,
                settings(max_queries=1, max_candidates=40, max_details=3),
                Mock(),
                lambda: cancelled[0],
                time.monotonic() + 10,
            )
        element.click.assert_called_once()

    def test_unexpected_timestamp_error_still_fails(self):
        page, element = self.lazy_timeout_page()
        element.click.side_effect = ValueError("Invalid timestamp state")
        with patch.dict("sys.modules", TIMEOUT_SDK), self.assertRaises(ValueError):
            discover(
                page,
                settings(max_queries=1, max_candidates=40, max_details=3),
                Mock(),
                lambda: False,
                time.monotonic() + 10,
            )

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
