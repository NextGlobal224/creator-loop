"""Manual measurements through the real sealed Package/Approval/Post write flow."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from creator_loop.database import _connect_write, initialize, open_readonly
from creator_loop.drafts import DraftAssertion, create_draft
from creator_loop.observations import (
    MetricInput,
    _record_manual_observation,
    list_observations,
)
from creator_loop.packages import PackageItem, PublicationRepository
from creator_loop.projects import create_project


class ObservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "Số liệu Huế"
        self.root.mkdir()
        self.path = self.root / "creator_loop.sqlite3"
        initialize(self.path)
        self.db = _connect_write(self.path)
        self.addCleanup(self.db.close)
        project = create_project(self.db, title="Manual measurement")
        body = "Giả thuyết: góc kể mới."
        draft = create_draft(
            self.db,
            project_id=project.project_id,
            body_text=body,
            format="POST",
            actor="author",
            claims=[],
            assertions=[DraftAssertion(0, len(body), None, "EDITORIAL")],
        )
        self.repo = PublicationRepository(self.path)
        fingerprint = self.repo.create_package(
            package_id="package",
            project_id=project.project_id,
            draft_version_id=draft.draft_version_id,
            platform="FB",
            format="POST",
            created_at="2026-10-03T00:00:00Z",
            items=[PackageItem("CAPTION", 0, text_payload=body)],
            data_root=self.root,
        )
        self.repo.record_approval(
            package_id="package",
            expected_fingerprint=fingerprint,
            decision="APPROVED",
            actor="human",
            reason="Inspected hypothesis",
            data_root=self.root,
        )
        self.repo.prepare_manual_post(
            post_id="post",
            package_id="package",
            expected_fingerprint=fingerprint,
            data_root=self.root,
        )
        self.fingerprint = fingerprint

    def _publish(self):
        self.repo.record_manual_post(
            post_id="post",
            package_id="package",
            expected_fingerprint=self.fingerprint,
            external_post_id=None,
            external_url=None,
            published_at="2026-10-03T07:00:00+07:00",
            data_root=self.root,
        )

    def _record(self, metrics=(), observed_at="2026-10-03T08:00:00+07:00", post="post"):
        return self.repo.record_manual_observation(
            post_id=post,
            observed_at=observed_at,
            metrics=metrics,
        )

    def test_multiple_measurements_preserve_zero_unknown_definition_and_provenance(
        self,
    ):
        self._publish()
        first = self._record(
            (
                MetricInput("views", "POST", 0, "count", "FB-views-v1", "0"),
                MetricInput("reach", "POST", None, "count", "FB-reach-v1"),
                MetricInput("views", "ACCOUNT", 12, "count", "FB-account-v2", "12"),
            )
        )
        second = self._record(
            (MetricInput("views", "POST", 7, "count", "FB-views-v2", "7"),)
        )
        with closing(open_readonly(self.path)) as read:
            read.execute("BEGIN")
            history = list_observations(read, "post")
            self.assertEqual(history, (first, second))
            self.assertEqual(
                read.execute("SELECT count(*) FROM observation_metrics").fetchone()[0],
                3,
            )
        self.assertEqual(first.value_for("views"), 0)
        self.assertIsNone(first.value_for("reach"))
        self.assertIsNone(first.value_for("impressions"))
        self.assertEqual(first.value_for("views", "ACCOUNT"), 12)
        self.assertEqual(first.platform, "FB")
        self.assertEqual(first.observed_at, "2026-10-03T01:00:00Z")
        self.assertEqual(first.collector_type, "MANUAL")
        self.assertIsNone(first.processing_run_id)
        self.assertNotEqual(first.observation_id, second.observation_id)
        self.assertEqual(first.metrics[-1].definition_version, "FB-views-v1")

    def test_pending_missing_and_prepublication_time_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "previously published"):
            self._record()
        self._publish()
        with self.assertRaisesRegex(ValueError, "previously published"):
            self._record(post="missing")
        for instant in ("2026-10-02T23:59:59Z", "2026-10-03T01:00:00", "bad"):
            with self.subTest(instant=instant), self.assertRaises(ValueError):
                self._record(observed_at=instant)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 0
        )

    def test_invalid_values_and_metadata_never_create_partial_observation(self):
        self._publish()
        invalid = [
            MetricInput("views", "POST", v, "count", "v1")
            for v in (-1, 0.5, float("nan"), float("inf"), True, 2**53 + 1)
        ] + [
            MetricInput("", "POST", 1, "count", "v1"),
            MetricInput(" views ", "POST", 1, "count", "v1"),
            MetricInput("views", "WRONG", 1, "count", "v1"),
            MetricInput("views", "POST", 1, "", "v1"),
            MetricInput("views", "POST", 1, "count", ""),
            MetricInput("views", "POST", None, "count", "v1", "unknown"),
        ]
        for metric in invalid:
            with self.subTest(metric=metric), self.assertRaises(ValueError):
                self._record((metric,))
        known = MetricInput("views", "POST", 1, "count", "v1")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self._record((known, known))
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 0
        )

    def test_insert_failure_rolls_back_observation_and_first_metric(self):
        self._publish()
        self.db.execute("""CREATE TRIGGER injected_fail BEFORE INSERT ON observation_metrics
            WHEN NEW.metric_key='views' BEGIN SELECT RAISE(ABORT,'injected'); END""")
        self.db.commit()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "injected"):
            self._record(
                (
                    MetricInput("reach", "POST", 2, "count", "v1"),
                    MetricInput("views", "POST", 3, "count", "v1"),
                )
            )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 0
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM observation_metrics").fetchone()[0], 0
        )
        self.assertFalse(self.db.in_transaction)

    def test_sql_insert_cannot_bypass_numeric_and_definition_guards(self):
        self._publish()
        event = self._record()
        for value, unit, definition in (
            (-1, "count", "v1"),
            (0.5, "COUNT", "v1"),
            (float("inf"), "ratio", "v1"),
            (1, "", "v1"),
            (1, "count", ""),
            ("bad", "count", "v1"),
        ):
            with (
                self.subTest(value=value, unit=unit),
                self.assertRaises(sqlite3.IntegrityError),
            ):
                self.db.execute(
                    "INSERT INTO observation_metrics VALUES(?,'views','POST',?,?,?,NULL)",
                    (event.observation_id, value, unit, definition),
                )
            self.db.rollback()
        self.assertEqual(list_observations(self.db, "post"), (event,))

    def test_history_cannot_update_delete_or_replace_and_fk_remains_valid(self):
        self._publish()
        event = self._record((MetricInput("reach", "POST", 2, "count", "v1"),))
        statements = [
            (
                "UPDATE observations SET observed_at='bad' WHERE observation_id=?",
                (event.observation_id,),
            ),
            (
                "DELETE FROM observations WHERE observation_id=?",
                (event.observation_id,),
            ),
            (
                "UPDATE observation_metrics SET numeric_value=9 WHERE observation_id=?",
                (event.observation_id,),
            ),
            (
                "DELETE FROM observation_metrics WHERE observation_id=?",
                (event.observation_id,),
            ),
            (
                "INSERT OR REPLACE INTO observations SELECT * FROM observations WHERE observation_id=?",
                (event.observation_id,),
            ),
            (
                "INSERT OR REPLACE INTO observation_metrics SELECT * FROM observation_metrics WHERE observation_id=?",
                (event.observation_id,),
            ),
            (
                "INSERT INTO observation_metrics VALUES('orphan','views','POST',1,'count','v1',NULL)",
                (),
            ),
        ]
        for sql, args in statements:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, args)
            self.db.rollback()
        self.assertEqual(list_observations(self.db, "post"), (event,))
        self.assertEqual(self.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_existing_post_can_be_measured_after_revoke_removed_and_project_archive(
        self,
    ):
        self._publish()
        self.repo.record_approval(
            package_id="package",
            expected_fingerprint=self.fingerprint,
            decision="REVOKED",
            actor="human",
            reason="Future posting stopped",
            data_root=self.root,
        )
        self.db.execute("UPDATE posts SET status='REMOVED' WHERE post_id='post'")
        self.db.execute("UPDATE projects SET status='ARCHIVED'")
        self.db.commit()
        event = self._record()
        self.assertEqual(event.metrics, ())
        self.assertIsNone(event.value_for("views"))

    def test_owns_transaction_without_committing_caller_work(self):
        self._publish()
        self.db.execute("BEGIN IMMEDIATE")
        with self.assertRaisesRegex(ValueError, "own transaction"):
            _record_manual_observation(
                self.db, post_id="post", observed_at="2026-10-03T01:00:00Z", metrics=()
            )
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
