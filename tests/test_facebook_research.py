import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from creator_loop.database import _connect_write, initialize, validate
from creator_loop.facebook_research import (
    DEFAULT_THRESHOLDS,
    candidate,
    candidates,
    criteria,
    hashtag_summary,
    metric,
    research_counts,
    save_selected,
    store_search,
    suggested_queries,
)
from creator_loop.facebook_sources import save_capture
from test_text_intake import _NonWindowsOwnedFile


def settings(**changes):
    return criteria(
        {
            "topic": "Python",
            "purpose": "",
            "since": None,
            "until": None,
            "mode": "COMBINED",
            "queries": ["Python"],
            "max_queries": 4,
            "max_candidates": 40,
            "max_details": 20,
            "thresholds": DEFAULT_THRESHOLDS.copy(),
            **changes,
        }
    )


def observation(pid="12", **changes):
    return {
        "url": "https://www.facebook.com/user/posts/" + pid,
        "excerpt": "Python: hướng dẫn từng bước để kiểm tra dữ liệu, ví dụ và kinh nghiệm thực tế #Python",
        "publisher": "Fixture",
        "publisher_url": "https://www.facebook.com/profile.php?id=10&tracking=discard",
        "published_at": "2026-10-12T00:00:00Z",
        "metrics": {},
        **changes,
    }


class ResearchTests(unittest.TestCase):
    def test_vietnamese_topic_preserves_accents_against_false_positive_words(self):
        params = settings(mode="VIRAL", topic="nhận thức đúng về sự thật")
        false = candidate(
            observation(
                excerpt="Nhân cách và hình thức: đứng lên dùng sự thật",
                metrics={"likes": "5000 likes"},
            ),
            "nhận thức",
            params,
        )
        self.assertFalse(false["assessment"]["topical"])
        self.assertTrue(false["assessment"]["viral"])
        self.assertEqual(research_counts([false])["viral"], 0)
        correct = candidate(
            observation(
                excerpt="NHẬN THỨC ĐÚNG VỀ SỰ THẬT và kiểm chứng thông tin",
                metrics={"likes": "1000 likes"},
            ),
            "nhận thức",
            params,
        )
        self.assertTrue(correct["assessment"]["topical"])
        self.assertEqual(research_counts([correct])["viral"], 1)
        self.assertEqual(correct["assessment"]["method"], "LOCAL_RULES_V1_2")

    def test_viral_requires_observed_threshold_and_topic_not_analysis(self):
        params = settings(mode="VIRAL")
        analytical = candidate(observation("15"), "Python", params)
        self.assertTrue(analytical["assessment"]["analysis"])
        self.assertIsNone(analytical["assessment"]["viral"])
        self.assertEqual(analytical["assessment"]["state"], "NEEDS_REVIEW")
        matched = candidate(
            observation(metrics={"likes": "1000 likes"}), "Python", params
        )
        unrelated = candidate(
            observation("13", excerpt="Java", metrics={"likes": "9000 likes"}),
            "Python",
            params,
        )
        missing = candidate({"url": observation("14")["url"]}, "Python", params)
        self.assertEqual(
            research_counts([matched, matched, analytical, unrelated, missing]),
            {"candidates": 4, "evaluated": 2, "viral": 1},
        )
        self.assertIn("≥ 1000", matched["assessment"]["reasons"][0])
        below = candidate(
            observation(
                metrics={
                    "likes": "999 likes",
                    "comments": "99 comments",
                    "shares": "99 shares",
                    "views": "9999 views",
                }
            ),
            "Python",
            params,
        )
        self.assertFalse(below["assessment"]["viral"])
        self.assertEqual(research_counts([below])["viral"], 0)

    def test_larger_budget_is_still_finite_without_changing_thresholds(self):
        params = settings(max_queries=12, max_candidates=300, max_details=120)
        self.assertEqual(params["thresholds"], DEFAULT_THRESHOLDS)
        for key, invalid in (
            ("max_queries", 13),
            ("max_candidates", 301),
            ("max_details", 121),
        ):
            with self.assertRaises(ValueError):
                settings(**{key: invalid})

    def test_style_word_alone_is_not_a_guide_but_explicit_data_is_quoted(self):
        style = candidate(
            observation(excerpt="Python với phong cách mới"), "Python", settings()
        )
        self.assertFalse(style["assessment"]["analysis"])
        data = candidate(
            observation(excerpt="Python có mức giá 199K mỗi tháng"),
            "Python",
            settings(),
        )
        self.assertTrue(data["assessment"]["analysis"])
        self.assertEqual(
            data["assessment"]["reasons"],
            ["Dữ kiện: có cụm “mức giá” trong phần đọc được"],
        )
        self.assertIn("199K", data["assessment"]["evidence"][0])

    def test_displayed_date_filters_without_fabricating_midnight_or_relative_date(self):
        params = settings(since="2025-10-10", until="2025-10-10")
        item = candidate(
            observation(published_at=None, published_label="10 tháng 10, 2025"),
            "Python",
            params,
        )
        self.assertEqual(item["assessment"]["state"], "QUALIFIED")
        self.assertIsNone(item["published_at"])
        self.assertIn("published_at", item["assessment"]["missing"])
        for label in (
            "2 giờ",
            "10 tháng 10",
            "31 tháng 2, 2025",
            "10 tháng 10, 2025 và 11 tháng 10, 2025",
        ):
            item = candidate(
                observation(published_at=None, published_label=label), "Python", params
            )
            self.assertIsNone(item["assessment"]["time_match"])

    def test_observed_group_member_ids_count_same_publisher_across_groups(self):
        first = candidate(
            observation(
                publisher_url="https://www.facebook.com/groups/12/user/10/?tracking=discard"
            ),
            "Python",
            settings(),
        )
        second = candidate(
            observation(
                "13", publisher_url="https://www.facebook.com/groups/99/user/10/"
            ),
            "Python",
            settings(),
        )
        self.assertEqual(
            first["publisher_url"], "https://www.facebook.com/groups/12/user/10"
        )
        summary = hashtag_summary([first, second])[0]
        self.assertEqual(summary["known_independent_sources"], 1)
        self.assertEqual(summary["posts"], 2)

    def setUp(self):
        if os.name != "nt":
            owned_patch = patch(
                "creator_loop.text_intake.OwnedWindowsFile", _NonWindowsOwnedFile
            )
            owned_patch.start()
            self.addCleanup(owned_patch.stop)

    def test_unknown_zero_compact_numbers_and_viral_without_rank_or_reel(self):
        self.assertIsNone(metric(None))
        self.assertEqual(metric("0 bình luận")["value"], 0)
        self.assertEqual(
            metric("1,2K lượt xem"),
            {"raw": "1,2K lượt xem", "value": 1200, "approximate": True},
        )
        self.assertEqual(metric("1.234 bình luận")["value"], 1234)
        value = candidate(
            observation(
                url="https://www.facebook.com/reel/12",
                excerpt="Python #viral",
                metrics={},
            ),
            "Python",
            settings(),
        )
        self.assertIsNone(value["assessment"]["viral"])
        self.assertEqual(value["assessment"]["state"], "NEEDS_REVIEW")
        self.assertEqual(value["scope"], "CAPTION_ONLY")

    def test_topic_and_signal_and_time_are_required(self):
        params = settings(since="2026-10-01", until="2026-10-12")
        matched = candidate(observation(), "Python", params)
        self.assertEqual(matched["assessment"]["state"], "QUALIFIED")
        self.assertTrue(matched["assessment"]["evidence"])
        outside = candidate(
            observation(published_at="2026-09-12T00:00:00Z"), "Python", params
        )
        self.assertEqual(outside["assessment"]["state"], "REJECTED")
        unknown = candidate(observation(published_at=None), "Python", params)
        self.assertEqual(unknown["assessment"]["state"], "NEEDS_REVIEW")
        unrelated = candidate(
            observation(excerpt="Java hướng dẫn", metrics={"likes": "5000 likes"}),
            "Python",
            params,
        )
        self.assertTrue(unrelated["assessment"]["viral"])
        self.assertEqual(unrelated["assessment"]["state"], "REJECTED")
        url_only = candidate({"url": observation()["url"]}, "Python", settings())
        self.assertEqual(url_only["scope"], "URL_ONLY")
        self.assertIsNone(url_only["assessment"]["topical"])

    def test_modes_and_query_budget_do_not_relax_criteria(self):
        raw = observation(excerpt="Python #viral", metrics={"likes": "1,2K likes"})
        self.assertEqual(
            candidate(raw, "Python", settings(mode="VIRAL"))["assessment"]["state"],
            "QUALIFIED",
        )
        self.assertEqual(
            candidate(raw, "Python", settings(mode="ANALYSIS"))["assessment"]["state"],
            "NEEDS_REVIEW",
        )
        with self.assertRaises(ValueError):
            settings(queries=["a", "b", "c", "d", "e"])
        with self.assertRaises(ValueError):
            settings(since="2026-10-12", until="2026-10-01")
        self.assertTrue(all("Huế" in q for q in suggested_queries("Huế")))

    def test_hashtags_deduplicate_posts_and_publishers_without_global_growth_claim(
        self,
    ):
        params = settings()
        one = candidate(observation(), "Python", params, "2026-10-12T00:00:00+00:00")
        repeat = candidate(observation(), "Python", params, "2026-10-12T01:00:00+00:00")
        two = candidate(observation("13"), "Python", params)
        three = candidate(observation("14", publisher_url=None), "Python", params)
        summary = hashtag_summary([one, repeat, two, three])[0]
        self.assertEqual(summary["posts"], 3)
        self.assertEqual(summary["known_independent_sources"], 1)
        self.assertEqual(summary["unknown_sources"], 1)
        self.assertEqual(len(summary["source_urls"]), 3)
        self.assertEqual(summary["label"], "Được nhắc nhiều trong mẫu quét")
        self.assertIn("Chưa rõ", summary["growth"])
        self.assertNotIn("tracking", one["publisher_url"])

    def test_discovery_is_not_intake_then_selected_duplicate_keeps_first_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root / "creator_loop.sqlite3")
            (root / "runtime").mkdir()
            params = settings()
            q = [{"text": "Python", "kind": "EDITABLE_SUGGESTION", "source_urls": []}]
            item = candidate(observation(), "Python", params)
            store_search(root, params, q, [item])
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM sources").fetchone()[0], 0
                )
            cid = candidates(root)[0]["candidate_id"]
            source_id, new = save_selected(root, cid)
            self.assertTrue(new)
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                original = db.execute("SELECT * FROM source_captures").fetchall()
                files = db.execute("SELECT * FROM asset_files").fetchall()
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE facebook_candidates SET snapshot_json='{}'")
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(
                        "INSERT OR REPLACE INTO facebook_candidates SELECT 'different',search_id,external_id,canonical_url,observed_at,snapshot_json FROM facebook_candidates"
                    )
            self.assertEqual(save_selected(root, cid), (source_id, False))
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                self.assertEqual(
                    db.execute("SELECT * FROM source_captures").fetchall(), original
                )
                self.assertEqual(
                    db.execute("SELECT * FROM asset_files").fetchall(), files
                )
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM facebook_selection_events"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM evidence_versions").fetchone()[0],
                    0,
                )

    def test_tampered_assessment_and_cancel_do_not_create_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            initialize(root / "creator_loop.sqlite3")
            params = settings()
            item = candidate(observation(), "Python", params)
            bad = json.loads(json.dumps(item))
            bad["assessment"]["viral"] = True
            with self.assertRaises(ValueError):
                store_search(root, params, [], [bad])
            store_search(
                root,
                params,
                [{"text": "Python", "kind": "EDITABLE_SUGGESTION", "source_urls": []}],
                [item],
            )
            with self.assertRaises(InterruptedError):
                save_selected(root, candidates(root)[0]["candidate_id"], lambda: True)
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                self.assertEqual(
                    db.execute("SELECT count(*) FROM sources").fetchone()[0], 0
                )

    def test_schema7_upgrade_has_consistent_backup_and_keeps_existing_url_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "creator_loop.sqlite3"
            from unittest.mock import patch

            from creator_loop import database

            with (
                patch.object(database, "MIGRATIONS", database.MIGRATIONS[:7]),
                patch.object(database, "SCHEMA_VERSION", 7),
            ):
                # initialize reads SCHEMA_VERSION dynamically; validation defaults are explicit below.
                db = database._connect_write(path)
                try:
                    db.execute("BEGIN IMMEDIATE")
                    for i, mid in enumerate(database.MIGRATIONS, 1):
                        sql, digest = database._migration_sql(mid)
                        database._execute_migration(db, sql)
                        db.execute(
                            "INSERT INTO schema_migrations VALUES (?,?,?,?)",
                            (mid, digest, "2026-10-12", "0.1.0"),
                        )
                        db.execute(f"PRAGMA user_version={i}")
                    db.commit()
                finally:
                    db.close()
            source_id, _ = save_capture(
                root, keyword="old", url=observation()["url"], excerpt=None
            )
            initialize(path)
            snapshots = list(root.glob("*.pre-v8-*.sqlite3"))
            self.assertEqual(len(snapshots), 1)
            with closing(sqlite3.connect(snapshots[0])) as db:
                db.execute("PRAGMA foreign_keys=ON")
                validate(db, expected_version=7)
                self.assertEqual(
                    db.execute("SELECT source_id FROM sources").fetchone()[0], source_id
                )
            with closing(_connect_write(path)) as db:
                validate(db)
                self.assertEqual(
                    db.execute("SELECT source_id FROM source_captures").fetchone()[0],
                    source_id,
                )
