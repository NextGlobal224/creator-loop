"""Combined readonly review keeps bytes and refuses stale/raw/stage selections."""

import json
import os
import subprocess
import sys
import unittest

import test_corrupt_restore_copy as copy_fixture
from creator_loop.damaged_restore_review import review_damaged_restore
from creator_loop.restore_assessment import assessment_identity


@unittest.skipUnless(
    sys.platform == "win32", "Native source/raw/stage/candidate leases"
)
class DamagedRestoreReviewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = copy_fixture.CorruptRestoreCopyTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.installation = self.fixture.fixture.fixture.installation
        self.candidate = self.fixture.fixture.candidate
        self.backup_id = self.fixture.fixture.backup_id

    def review(self, **changes):
        return review_damaged_restore(
            self.root, self.backup_id, self.installation, self.candidate, **changes
        )

    def files(self):
        return {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }

    def test_source_and_backup_review_without_raw_stage_never_grants_copy(self):
        before = self.files()
        review = self.review()
        self.assertEqual(review["assessment"]["restore_assessment_format"], 2)
        self.assertIsNone(review["assessment"]["current_counts"])
        self.assertIsNone(review["raw_source"])
        self.assertIsNone(review["preparation"])
        self.assertFalse(review["copy_supported"])
        self.assertFalse(review["restored"])
        self.assertEqual(review["assessment_identity"], assessment_identity(review))
        self.assertEqual(self.files(), before)
        self.assertNotIn("PRIVATE", json.dumps(review))

    def test_raw_and_closed_stage_proofs_bound_to_current_review_preserve_all_bytes(
        self,
    ):
        before = self.files()
        prep = json.loads(self.fixture.manifest.read_text())
        review = self.review(
            raw_manifest=self.root / prep["raw_manifest"],
            preparation_manifest=self.fixture.manifest,
        )
        self.assertTrue(review["raw_source"]["archive_revalidated"])
        self.assertFalse(review["raw_source"]["consistent_backup"])
        self.assertTrue(review["preparation"]["stage_revalidated"])
        self.assertTrue(review["preparation"]["current_binding_matches"])
        self.assertTrue(review["copy_supported"])
        self.assertFalse(review["preparation"]["apply_authorized"])
        self.assertEqual(
            review["preparation"]["manifest_sha256"], self.fixture.preparation_sha
        )
        self.assertEqual(self.files(), before)

    def test_changed_source_shows_valid_historical_stage_as_stale_without_apply(self):
        self.fixture.source.write_bytes(b"PRIVATE NEW DAMAGED SOURCE")
        before = self.files()
        review = self.review(preparation_manifest=self.fixture.manifest)
        self.assertTrue(review["preparation"]["stage_revalidated"])
        self.assertFalse(review["preparation"]["current_binding_matches"])
        self.assertFalse(review["copy_supported"])
        self.assertEqual(self.files(), before)

    def test_selected_raw_manifest_different_from_stage_refuses_preserving_both(self):
        from creator_loop.corrupt_source_preservation import preserve_corrupt_source

        review = self.review()
        raw = preserve_corrupt_source(
            self.root, review["assessment"]["damage"]["damage_identity"]
        )
        before = self.files()
        with self.assertRaisesRegex(ValueError, "differs from prepared archive"):
            self.review(raw_manifest=raw, preparation_manifest=self.fixture.manifest)
        self.assertEqual(self.files(), before)

    def test_actual_cli_orphan_review_flags_never_create_missing_root(self):
        missing = self.root / "OWN MISSING ROOT"
        env = {
            **os.environ,
            "PYTHONPATH": "app",
            "CREATOR_LOOP_DATA_ROOT": str(missing),
        }
        for flags in (
            ["--review-raw-source", str(self.fixture.manifest)],
            ["--review-damaged-preparation", str(self.fixture.manifest), "--smoke"],
            ["--review-damaged-restore", self.backup_id],
        ):
            result = subprocess.run(
                [sys.executable, "-m", "creator_loop", *flags],
                env=env,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertFalse(missing.exists())
