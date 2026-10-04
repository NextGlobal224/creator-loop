"""Actual readonly guarded copy + published backup/media/candidate review."""

import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_corrupt_copy_resume as resume_fixture
from creator_loop.app_lock import AppDataLock
from creator_loop.corrupt_copy_review import review_corrupt_copy
from creator_loop.restore_assessment import assessment_identity


@unittest.skipUnless(sys.platform == "win32", "native copy review leases")
class CorruptCopyReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = resume_fixture.CorruptCopyResumeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.installation

    def review(self):
        return review_corrupt_copy(self.root, self.fixture.copy, self.installation)

    def test_missing_live_review_shows_bound_backup_time_without_init_or_guard_change(
        self,
    ):
        self.fixture.crash_copy("CORRUPT_SOURCE_RETAINED")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        review = self.review()
        backup = json.loads(
            (
                self.root / "backups" / review["backup_id"] / "backup-manifest.json"
            ).read_text()
        )
        self.assertEqual(review["backup_created_at"], backup["created_at"])
        self.assertEqual(
            review["inspection"]["actual_state"], "SOURCE_RETAINED_LIVE_MISSING_GUARDED"
        )
        self.assertFalse(review["unknown_continuation_allowed"])
        self.assertFalse(review["restored"])
        self.assertEqual(review["assessment_identity"], assessment_identity(review))
        self.assertEqual(
            {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before
        )
        self.assertFalse(self.fixture.fixture.source.exists())
        self.fixture.assert_guarded()

    def test_unknown_readonly_review_allows_only_complete_interrupted_originals(self):
        bundle = self.fixture.unknown_bundle()
        review = self.review()
        self.assertTrue(review["unknown_continuation_allowed"])
        self.assertNotIn("PRIVATE", json.dumps(review))
        original = (
            self.root
            / "backups"
            / json.loads(self.fixture.copy_bytes)["retained_directory"]
        )
        (original / "KEEP-FOREIGN").write_bytes(b"KEEP")
        review = self.review()
        self.assertFalse(review["unknown_continuation_allowed"])
        self.assertEqual(
            {name: (self.root / name).read_bytes() for name in bundle}, bundle
        )
        self.fixture.assert_guarded()

    def test_current_media_issues_review_does_not_claim_health_or_current_loss_counts(
        self,
    ):
        self.fixture.crash_copy("CORRUPT_COPY_STARTED")
        media = self.fixture.fixture.fixture.fixture.media
        original = media.read_bytes()
        media.unlink()
        try:
            review = self.review()
            self.assertEqual(review["media_issue_count"], 1)
            self.assertFalse(review["activated"])
            self.assertFalse(review["media_included"])
            self.assertNotIn("current_counts", review)
            self.assertFalse(media.exists())
            self.assertEqual(self.fixture.fixture.source.read_bytes(), b"")
        finally:
            media.write_bytes(original)
        self.fixture.assert_guarded()

    def test_candidate_inventory_changed_during_media_review_refuses_unchanged_copy(
        self,
    ):
        import creator_loop.corrupt_copy_review as module

        self.fixture.crash_copy("CORRUPT_SOURCE_RETAINED")
        original = module._media_assessment
        candidate = self.fixture.fixture.fixture.candidate

        def changed(*args):
            result = original(*args)
            (candidate / "KEEP-FOREIGN-CANDIDATE").write_bytes(b"KEEP")
            return result

        with patch("creator_loop.corrupt_copy_review._media_assessment", changed):
            with self.assertRaises((RuntimeError, ValueError)):
                self.review()
        self.assertTrue((candidate / "KEEP-FOREIGN-CANDIDATE").exists())
        self.fixture.assert_guarded()

    def test_actual_cli_missing_mixed_and_busy_review_never_initializes_live(self):
        self.fixture.crash_copy("CORRUPT_SOURCE_RETAINED")
        prefix = [
            sys.executable,
            "-m",
            "creator_loop",
            "--review-corrupt-copy",
            str(self.fixture.copy),
        ]
        env = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(self.root),
        }

        def run(extra):
            return subprocess.run(
                prefix + extra, env=env, capture_output=True, timeout=30
            )

        for extra in ([], ["--installation-root", str(self.installation), "--smoke"]):
            result = run(extra)
            self.assertEqual(result.returncode, 2, result.stderr)
        with AppDataLock(self.root):
            result = run(["--installation-root", str(self.installation)])
            self.assertEqual(result.returncode, 3, result.stderr)
        result = run(["--installation-root", str(self.installation)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["inspection"]["actual_state"],
            "SOURCE_RETAINED_LIVE_MISSING_GUARDED",
        )
        self.assertNotIn(b"PRIVATE", result.stdout + result.stderr)
        self.assertFalse(self.fixture.fixture.source.exists())
        self.fixture.assert_guarded()
