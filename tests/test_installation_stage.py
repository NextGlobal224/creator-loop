"""Byte verification, unsafe archives and separation from user data/old versions."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from creator_loop.installation_stage import load_release_manifest, stage_installation


class InstallationStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.installation = self.base / "Cài đặt Huế"
        self.installation.mkdir()
        self.data = self.base / "Dữ liệu"
        self.data.mkdir()
        self.db = self.data / "creator_loop.sqlite3"
        self.db.write_bytes(b"untouched fixture database")
        self.old = self.installation / "previous-version"
        self.old.mkdir()
        (self.old / "keep.txt").write_bytes(b"old installation")
        self.archive = self.base / "candidate.zip"
        self.manifest_path = self.base / "manifest.json"
        self.members = {
            "CreatorLoop/CreatorLoop.exe": b"fixture launcher, never execute",
            "CreatorLoop/_internal/runtime.dat": b"runtime fixture",
        }
        self._write()

    def _write(self):
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, value in self.members.items():
                archive.writestr(name, value)
        self.manifest = {
            "manifest_version": 1,
            "app_version": "0.1.0",
            "git_commit": "a" * 40,
            "target": "win-x64",
            "tested_artifact": True,
            "artifact_sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "schema_from": 0,
            "schema_to": 6,
            "schema_read_min": 6,
            "schema_read_max": 6,
            "build_time": "2026-10-03T00:00:00Z",
            "minimum_os": "Windows 10 x64 build 19045",
            "runtime": "bundled fixture",
            "dependencies": {"fixture": "1"},
            "component_compatibility": {"fixture": "no engines"},
            "migration_ids": ["fixture"],
            "provenance": {"fixture": "generated test bytes"},
            "release_notes": "fixture",
            "recovery_notes": "Keep prior DB and installation; confirm loss after backup before restore",
            "files": {
                name: {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                for name, value in self.members.items()
            },
        }
        self._save()

    def _save(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def _stage(self, root=None):
        return stage_installation(
            self.archive,
            self.manifest_path,
            root or self.installation,
            user_data_root=self.data,
        )

    def _preserved(self):
        self.assertEqual(self.db.read_bytes(), b"untouched fixture database")
        self.assertEqual((self.old / "keep.txt").read_bytes(), b"old installation")
        self.assertEqual(
            {path.name for path in self.installation.iterdir()}, {self.old.name}
        )

    def test_verified_candidate_keeps_old_installation_and_user_data(self):
        original_zip = self.archive.read_bytes()
        target = self._stage()
        for name, value in self.members.items():
            self.assertEqual((target / name).read_bytes(), value)
        self.assertEqual(
            load_release_manifest(target / "release-manifest.json"), self.manifest
        )
        self.assertEqual(self.archive.read_bytes(), original_zip)
        self.assertEqual(self.db.read_bytes(), b"untouched fixture database")
        self.assertEqual((self.old / "keep.txt").read_bytes(), b"old installation")
        second = self._stage()
        self.assertNotEqual(target, second)
        self.assertTrue(target.is_dir())

    def test_zip_and_member_digest_mismatches_never_publish(self):
        self.manifest["artifact_sha256"] = "0" * 64
        self._save()
        with self.assertRaisesRegex(ValueError, "Artifact digest"):
            self._stage()
        self._write()
        self.manifest["files"]["CreatorLoop/CreatorLoop.exe"]["sha256"] = "0" * 64
        self._save()
        with self.assertRaisesRegex(ValueError, "file digest"):
            self._stage()
        self._preserved()

    def test_unsafe_paths_case_collisions_and_symlink_members_fail(self):
        for name in (
            "../outside.txt",
            "CreatorLoop/../outside.txt",
            "CreatorLoop/CON.txt",
            "CreatorLoop/x:stream",
            "CreatorLoop/trailing. ",
            "CreatorLoop\\escape.txt",
        ):
            with self.subTest(name=name):
                self.members[name] = b"unsafe"
                self._write()
                with self.assertRaises(ValueError):
                    self._stage()
                self.members.pop(name)
                self._preserved()
        self._write()
        with zipfile.ZipFile(self.archive, "a") as archive:
            link = zipfile.ZipInfo("CreatorLoop/link")
            link.create_system = 3
            link.external_attr = 0o120777 << 16
            archive.writestr(link, "outside")
        self.manifest["artifact_sha256"] = hashlib.sha256(
            self.archive.read_bytes()
        ).hexdigest()
        self._save()
        with self.assertRaisesRegex(ValueError, "Linked"):
            self._stage()
        self._preserved()

    def test_missing_extra_and_duplicate_members_fail(self):
        for variant in ("missing", "extra", "duplicate"):
            self._write()
            with zipfile.ZipFile(
                self.archive, "w" if variant == "missing" else "a"
            ) as archive:
                if variant == "extra":
                    archive.writestr("CreatorLoop/unlisted", b"unlisted")
                elif variant == "duplicate":
                    archive.writestr("CreatorLoop/creatorloop.exe", b"case collision")
            self.manifest["artifact_sha256"] = hashlib.sha256(
                self.archive.read_bytes()
            ).hexdigest()
            self._save()
            with self.assertRaises(ValueError):
                self._stage()
            self._preserved()

    def test_missing_compatibility_and_budget_refuse(self):
        for field in (
            "schema_read_min",
            "dependencies",
            "runtime",
            "recovery_notes",
            "provenance",
        ):
            self._write()
            self.manifest.pop(field)
            self._save()
            with self.assertRaises(ValueError):
                self._stage()
        self._write()
        with patch("creator_loop.installation_stage.MAX_EXPANDED_BYTES", 1):
            with self.assertRaises(ValueError):
                self._stage()
        self._preserved()

    def test_file_directory_and_directory_case_collisions_refuse(self):
        for name in ("CreatorLoop/_INTERNAL/another.dat", "CreatorLoop/_internal"):
            self.members[name] = b"collision"
            self._write()
            with self.assertRaisesRegex(ValueError, "collid|conflicts"):
                self._stage()
            self.members.pop(name)
            self._preserved()

    def test_manifest_writer_records_tested_bytes_and_build_context(self):
        from scripts.write_release_manifest import write_manifest

        package = self.base / "extracted/CreatorLoop"
        for name, content in self.members.items():
            path = package.joinpath(*name.split("/")[1:])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        with (
            patch(
                "scripts.write_release_manifest.platform.system", return_value="Windows"
            ),
            patch(
                "scripts.write_release_manifest.platform.machine", return_value="AMD64"
            ),
            patch(
                "scripts.write_release_manifest.importlib.metadata.version",
                return_value="fixture-1",
            ),
        ):
            write_manifest(
                self.archive,
                package,
                self.manifest_path,
                "a" * 40,
                "https://example.test/ci/1",
            )
        actual = load_release_manifest(self.manifest_path)
        self.assertEqual(actual["files"], self.manifest["files"])
        self.assertEqual(actual["artifact_sha256"], self.manifest["artifact_sha256"])
        self.assertEqual(actual["provenance"]["ci_run"], "https://example.test/ci/1")
        self.assertIn("schema_read_min", actual)
        self.assertIn("PySide6", actual["dependencies"])
        self.assertIn("backup", actual["recovery_notes"])

    def test_overlap_disk_and_write_failures_preserve_db_and_prior_versions(self):
        for root in (self.data, self.data / "nested", self.base):
            root.mkdir(exist_ok=True)
            with self.assertRaises(ValueError):
                self._stage(root)
        with patch("creator_loop.installation_stage.shutil.disk_usage") as disk:
            disk.return_value.free = 0
            with self.assertRaises(OSError):
                self._stage()
        with patch(
            "creator_loop.installation_stage.os.fsync",
            side_effect=OSError("fixture write failure"),
        ):
            with self.assertRaises(OSError):
                self._stage()
        self._preserved()

    def test_cli_staging_uses_existing_installation_root_and_does_not_open_db(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
        env["CREATOR_LOOP_DATA_ROOT"] = str(self.data)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "creator_loop",
                "--stage-update",
                str(self.archive),
                "--release-manifest",
                str(self.manifest_path),
                "--installation-root",
                str(self.installation),
            ],
            env=env,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"activation pending", result.stdout)
        self.assertEqual(self.db.read_bytes(), b"untouched fixture database")
