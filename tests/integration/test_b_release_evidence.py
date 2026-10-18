"""B profile fails closed without weakening full-beta/stable eligibility."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.b_release_evidence import B_SCOPES, check_b_evidence
from scripts.beta_release_evidence import DEFERRED, check_beta_evidence


class BReleaseEvidenceTests(unittest.TestCase):
    def test_finalizer_uses_actual_schema_and_preserves_archive_bytes(self):
        from scripts.finalize_beta_manifest import finalize

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "archive.zip"
            archive.write_bytes(
                b"opaque exact ZIP fixture; gate mocked for metadata-only test"
            )
            before = archive.read_bytes()
            manifest = root / "manifest.json"
            body = {
                "git_commit": "a" * 40,
                "provenance": {
                    "ci_run": "https://github.com/NextGlobal224/creator-loop/actions/runs/1"
                },
                "artifact_sha256": "b" * 64,
                "schema_to": 10,
                "schema_read_min": 10,
                "schema_read_max": 10,
            }
            manifest.write_text(json.dumps(body))
            evidence = root / "evidence.json"
            evidence.write_text(
                json.dumps(
                    {
                        "scope": "B",
                        "development_manifest_sha256": hashlib.sha256(
                            manifest.read_bytes()
                        ).hexdigest(),
                        "component_compatibility": {},
                    }
                )
            )
            output = root / "final.json"
            with patch("scripts.finalize_beta_manifest.check_release_gate") as gate:
                finalize(archive, manifest, evidence, "v0.1.0-beta.1", output)
                gate.assert_called_once()
            final = json.loads(output.read_text("utf-8"))
            self.assertEqual(final["release_scope"], "B")
            self.assertIn("Schema 10", final["recovery_notes"])
            self.assertNotIn("Schema 7", final["recovery_notes"])
            self.assertEqual(archive.read_bytes(), before)

    def setUp(self):
        files = {"CreatorLoop/CreatorLoop.exe": {"sha256": "c" * 64}}
        for name in (
            "TEXT.md",
            "IMAGE_CAPTION.md",
            "VIDEO.md",
            "image.png",
            "outputs.json",
            "creator-cloud.json",
        ):
            files["CreatorLoop/examples/B/" + name] = {"sha256": "d" * 64}
        files["CreatorLoop/docs/B_BETA_GUIDE.html"] = {"sha256": "d" * 64}
        self.manifest = {
            "release_channel": "beta",
            "release_scope": "B",
            "release_tag": "v0.1.0-beta.1",
            "app_version": "0.1.0",
            "git_commit": "a" * 40,
            "artifact_sha256": "b" * 64,
            "provenance": {
                "ci_run": "https://github.com/NextGlobal224/creator-loop/actions/runs/1"
            },
            "source_hashes": {
                "app/creator_loop/backend/creator/research_pipeline.py": "e" * 64
            },
            "files": files,
            "component_compatibility": {
                "status": "VERIFIED_B_RUNTIME",
                "evidence_sha256": "f" * 64,
            },
        }
        self.receipt = {
            "format": 1,
            "channel": "beta",
            "scope": "B",
            "development_manifest_sha256": "d" * 64,
            "artifact": {
                "commit": "a" * 40,
                "ci_run": self.manifest["provenance"]["ci_run"],
                "zip_sha256": "b" * 64,
                "exe_sha256": "c" * 64,
            },
            "tested_packaged_artifact": True,
            "deferred": sorted(DEFERRED),
            "excluded": [
                "A-new-scan",
                "C-render-voice-quality",
                "full-beta",
                "stable-V1",
            ],
            "component_compatibility": copy.deepcopy(
                self.manifest["component_compatibility"]
            ),
            "scopes": [
                {
                    "id": name,
                    "status": "PASS",
                    "method": "NATIVE_ARTIFACT",
                    "evidence_sha256": "f" * 64,
                }
                for name in sorted(B_SCOPES)
            ],
            "cloud": {
                "provider": "CLOUDFLARE_WORKERS_AI",
                "model": "@cf/openai/gpt-oss-120b",
                "worker_version": "bcc58e3c-35bc-4d06-a601-52fc8fed929b",
                "config_sha256": "d" * 64,
                "live_evidence_sha256": "f" * 64,
                "runtime_evidence_sha256": "f" * 64,
                "compatibility_evidence_sha256": "f" * 64,
                "source_files": dict(self.manifest["source_hashes"]),
                "format_map": {
                    "TEXT": "Workers AI cloud; local persistence/review",
                    "IMAGE_CAPTION": "configured Codex/local backend; local image association",
                    "VIDEO": "configured Codex/local backend; B script only, C separate",
                },
            },
            "outputs": [
                {
                    "format": name,
                    "version": "a" * 32,
                    "body_sha256": "d" * 64,
                    "package_path": "CreatorLoop/examples/B/" + path,
                    "quality_evidence_sha256": "f" * 64,
                }
                for name, path in (
                    ("TEXT", "TEXT.md"),
                    ("IMAGE_CAPTION", "IMAGE_CAPTION.md"),
                    ("VIDEO", "VIDEO.md"),
                )
            ],
        }

    def check(self):
        check_b_evidence(self.receipt, self.manifest, "v0.1.0-beta.1")

    def test_structure_fixture_only_not_real_acceptance(self):
        self.check()
        with self.assertRaises(ValueError):
            check_beta_evidence(self.receipt, self.manifest, "v0.1.0-beta.1")

    def test_scope_native_output_source_cloud_and_privacy_fail_closed(self):
        original = copy.deepcopy(self.receipt)
        mutations = [
            lambda r: r.update(scope="full-beta"),
            lambda r: r.update(tested_packaged_artifact=1),
            lambda r: r["scopes"].pop(),
            lambda r: next(s for s in r["scopes"] if s["id"] == "native-b-ui").update(
                method="REUSED_COMPATIBLE_EVIDENCE"
            ),
            lambda r: r["outputs"][1].update(body_sha256="0" * 64),
            lambda r: r["cloud"]["source_files"].update(
                {"app/creator_loop/backend/creator/research_pipeline.py": "0" * 64}
            ),
            lambda r: r["cloud"].update(config_sha256="0" * 64),
            lambda r: r.update(token="forbidden"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                self.receipt = copy.deepcopy(original)
                mutate(self.receipt)
                with self.assertRaises(ValueError):
                    self.check()
