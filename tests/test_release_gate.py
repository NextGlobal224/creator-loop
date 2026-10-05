"""Synthetic policy fixtures do not provide real V1 release acceptance evidence."""

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.check_release_gate import check_release_gate
from scripts.package_product_docs import DOCUMENTS


class ReleaseGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "synthetic.zip"
        self.manifest_path = self.root / "manifest.json"
        self.acceptance = self.root / "acceptance.md"
        self.commit = "a" * 40
        self.run_url = "https://github.com/NextGlobal224/creator-loop/actions/runs/123"
        self.rows = [
            f"| Gate fixture | {i:02d} — synthetic | ĐÃ NGHIỆM THU | synthetic policy only | | — |"
            for i in range(1, 25)
        ]
        # Publication itself has not happened when its prerequisites are checked.
        self.rows[23] = self.rows[23].replace("ĐÃ NGHIỆM THU", "CHƯA LÀM")
        self.acceptance.write_text("\n".join(self.rows), encoding="utf-8")
        self.members = {
            "CreatorLoop/CreatorLoop.exe": b"FAKE POLICY FIXTURE; NEVER EXECUTE",
            "CreatorLoop/build-info.json": json.dumps(
                {"git_commit": self.commit, "target": "win-x64"}
            ).encode(),
        }
        for name in DOCUMENTS:
            key = (
                "CreatorLoop/USER_GUIDE.html"
                if name == "USER_GUIDE.md"
                else "CreatorLoop/docs/" + Path(name).stem + ".html"
            )
            self.members[key] = (
                b"Synthetic offline policy fixture, not user walkthrough"
            )
        component = {
            "name": "synthetic",
            "version": "fake-v1",
            "source": "https://example.invalid/fixture",
            "license": "FAKE ONLY",
            "sha256": "b" * 64,
            "local_use_allowed": True,
        }
        self.manifest = {
            "manifest_version": 1,
            "app_version": "0.1.0",
            "git_commit": self.commit,
            "target": "win-x64",
            "tested_artifact": True,
            "schema_from": 0,
            "schema_to": 6,
            "schema_read_min": 6,
            "schema_read_max": 6,
            "build_time": "2026-10-05T00:00:00Z",
            "minimum_os": "Windows fixture",
            "runtime": "synthetic",
            "dependencies": {"fixture": "1"},
            "migration_ids": ["fixture"],
            "component_compatibility": {
                "status": "VERIFIED_V1",
                "engine": dict(component),
                "model": dict(component),
            },
            "provenance": {
                "repository": "NextGlobal224/creator-loop",
                "ci_run": self.run_url,
                "method": "GitHub Actions Windows build; synthetic policy test",
            },
            "release_notes": "Synthetic final-policy fixture, never publish",
            "recovery_notes": "Keep backup/old installation; confirm data loss before restore",
        }
        self._write()

    def _write(self):
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as output:
            for name, data in self.members.items():
                output.writestr(name, data)
        self.manifest["artifact_sha256"] = hashlib.sha256(
            self.archive.read_bytes()
        ).hexdigest()
        self.manifest["files"] = {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in self.members.items()
        }
        self._save()

    def _save(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def _check(self, **changes):
        options = {
            "expected_commit": self.commit,
            "expected_run_url": self.run_url,
            "tag": "v0.1.0",
            **changes,
        }
        check_release_gate(self.archive, self.manifest_path, self.acceptance, **options)

    def test_synthetic_complete_preconditions_do_not_require_publication_already_done(
        self,
    ):
        before = self.archive.read_bytes()
        self._check()
        self.assertEqual(self.archive.read_bytes(), before)

    def test_tested_artifact_flag_cannot_replace_blocked_model_machine_acceptance(self):
        rows = list(self.rows)
        for number in (20, 23):
            rows[number - 1] = rows[number - 1].replace("ĐÃ NGHIỆM THU", "BỊ CHẶN")
        self.acceptance.write_text("\n".join(rows), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "prerequisites are not accepted"):
            self._check()

    def test_development_notes_and_unresolved_model_cannot_publish(self):
        for field, value in (
            ("release_notes", "Development checkpoint"),
            ("release_notes", "V1 release gate remains incomplete"),
            (
                "recovery_notes",
                "Final recovery/hardware/release validation remains required",
            ),
        ):
            original = self.manifest[field]
            self.manifest[field] = value
            self._save()
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self._check()
            self.manifest[field] = original
        self.manifest["component_compatibility"]["status"] = "unresolved"
        self._save()
        with self.assertRaisesRegex(ValueError, "compatibility"):
            self._check()

    def test_boolean_or_missing_component_evidence_cannot_claim_verified(self):
        model = self.manifest["component_compatibility"]["model"]
        for key, value in (
            ("sha256", "invalid"),
            ("license", ""),
            ("source", ""),
            ("version", ""),
            ("local_use_allowed", 1),
        ):
            original = model[key]
            model[key] = value
            self._save()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self._check()
            model[key] = original

    def test_commit_tag_and_ci_run_must_match_downloaded_bytes(self):
        for change in (
            {"expected_commit": "c" * 40},
            {"tag": "v9.9.9"},
            {"expected_run_url": self.run_url + "4"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self._check(**change)

    def test_missing_duplicate_and_pending_acceptance_rows_refuse(self):
        variants = [
            self.rows[:-1],
            self.rows + [self.rows[0]],
            [row.replace("ĐÃ NGHIỆM THU", "ĐANG LÀM") for row in self.rows],
            [row.replace("| | — |", "| unresolved | — |") for row in self.rows],
        ]
        for rows in variants:
            self.acceptance.write_text("\n".join(rows), encoding="utf-8")
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                self._check()

    def test_zip_digest_mismatch_refuses(self):
        with self.archive.open("ab") as output:
            output.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "ZIP differs"):
            self._check()

    def test_rehashing_zip_does_not_hide_member_tamper(self):
        self.members["CreatorLoop/CreatorLoop.exe"] = b"different fake bytes"
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as output:
            for name, data in self.members.items():
                output.writestr(name, data)
        self.manifest["artifact_sha256"] = hashlib.sha256(
            self.archive.read_bytes()
        ).hexdigest()
        self._save()
        with self.assertRaisesRegex(ValueError, "member differs"):
            self._check()

    def test_missing_offline_recovery_guide_and_stale_build_identity_refuse(self):
        removed = self.members.pop("CreatorLoop/docs/UPDATE_RECOVERY.html")
        self._write()
        with self.assertRaisesRegex(ValueError, "offline guide"):
            self._check()
        self.members["CreatorLoop/docs/UPDATE_RECOVERY.html"] = removed
        self.members["CreatorLoop/build-info.json"] = json.dumps(
            {"git_commit": "d" * 40, "target": "win-x64"}
        ).encode()
        self._write()
        with self.assertRaisesRegex(ValueError, "guide identity"):
            self._check()

    def test_publish_workflow_calls_guard_before_gh_release(self):
        workflow = (
            Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"
        ).read_text(encoding="utf-8")
        release = workflow.split("  release:\n", 1)[1]
        self.assertLess(
            release.index("scripts/check_release_gate.py"),
            release.index("gh release create"),
        )
        self.assertIn(
            '--expected-run-url "${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"',
            release,
        )
