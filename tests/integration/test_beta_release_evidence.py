import copy
import unittest

from scripts.beta_release_evidence import DEFERRED, REQUIRED, STEPS, check_beta_evidence


class BetaEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {
            "release_channel": "beta",
            "release_tag": "v0.1.0-beta.1",
            "app_version": "0.1.0",
            "git_commit": "a" * 40,
            "provenance": {
                "ci_run": "https://github.com/NextGlobal224/creator-loop/actions/runs/1"
            },
            "artifact_sha256": "b" * 64,
            "files": {"CreatorLoop/CreatorLoop.exe": {"sha256": "c" * 64}},
            "component_compatibility": {
                "status": "VERIFIED_BETA_LOCAL_RUNTIME",
                "engine": {"native_runtime_evidence_sha256": "d" * 64},
                "model": {"native_runtime_evidence_sha256": "e" * 64},
            },
        }
        self.manifest["files"][
            "CreatorLoop/_internal/creator_loop/resources/facebook-browser-pin.json"
        ] = {"sha256": "f" * 64}
        for kind in ("engine", "model"):
            self.manifest["component_compatibility"][kind].update(
                name="whisper",
                version="1.8.7",
                source="https://github.com/ggml-org/whisper.cpp",
                license="MIT",
                sha256="e" * 64,
                local_use_allowed=True,
            )
        self.receipt = {
            "component_compatibility": copy.deepcopy(
                self.manifest["component_compatibility"]
            ),
            "development_manifest_sha256": "d" * 64,
            "format": 1,
            "channel": "beta",
            "artifact": {
                "commit": "a" * 40,
                "ci_run": self.manifest["provenance"]["ci_run"],
                "zip_sha256": "b" * 64,
                "exe_sha256": "c" * 64,
            },
            "tested_packaged_artifact": True,
            "deferred": sorted(DEFERRED),
            "scopes": [
                {
                    "id": i,
                    "status": "PASS",
                    "method": "NATIVE_ARTIFACT",
                    "evidence_sha256": "d" * 64,
                }
                for i in sorted(REQUIRED)
            ],
            "facebook": {
                "method": "NATIVE_ARTIFACT",
                "tested_artifact": True,
                "no_meta_api_or_service": True,
                "real_post_url_sha256": "e" * 64,
                "browser_pin_sha256": "f" * 64,
                "steps": dict.fromkeys(STEPS, True),
            },
        }

    def check(self):
        check_beta_evidence(self.receipt, self.manifest, "v0.1.0-beta.1")

    def test_structured_gate_positive_is_fixture_only(self):
        self.check()

    def test_missing_step_source_mock_stale_and_bool_one_refused(self):
        original = copy.deepcopy(self.receipt)
        mutations = [
            lambda r: r["facebook"]["steps"].pop("normal_close"),
            lambda r: r["facebook"].update(method="SOURCE"),
            lambda r: r.update(tested_packaged_artifact=1),
            lambda r: r["artifact"].update(zip_sha256="9" * 64),
            lambda r: r["facebook"]["steps"].update(cancel_owned_live_browser=False),
            lambda r: r.update(deferred=list(DEFERRED) + ["fourth"]),
            lambda r: r["scopes"].pop(),
            lambda r: r["scopes"][0].update(status="SKIP"),
            lambda r: r["scopes"][0].update(id=r["scopes"][1]["id"]),
            lambda r: r["scopes"][0].update(method="REUSED_UNCHANGED_SOURCE"),
            lambda r: r["facebook"].update(real_post_url_sha256=None),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                self.receipt = copy.deepcopy(original)
                mutate(self.receipt)
                with self.assertRaises(ValueError):
                    self.check()

    def test_cross_channel_version_and_v1_status_refused(self):
        for field, value in [
            ("release_channel", "stable"),
            ("release_tag", "v0.1.0"),
            ("app_version", "0.2.0"),
        ]:
            old = self.manifest[field]
            self.manifest[field] = value
            with self.assertRaises(ValueError):
                self.check()
            self.manifest[field] = old
        self.manifest["component_compatibility"]["status"] = "VERIFIED_V1"
        with self.assertRaises(ValueError):
            self.check()

    def test_extra_private_payload_and_wrong_browser_pin_refused(self):
        self.receipt["cookies"] = "must not publish"
        with self.assertRaises(ValueError):
            self.check()
        self.receipt.pop("cookies")
        self.receipt["facebook"]["browser_pin_sha256"] = "9" * 64
        with self.assertRaises(ValueError):
            self.check()
