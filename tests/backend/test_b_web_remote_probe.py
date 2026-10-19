"""Probe must never send its generated QA credential to arbitrary origins."""

import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from scripts.b_web_profiles import required_pairs
from scripts.b_web_remote_probe import NoRedirect, probe


class BWebRemoteProbeTests(unittest.TestCase):
    def test_manual_profile_does_not_waive_full_default(self):
        self.assertEqual(len(required_pairs()), 6)
        self.assertEqual(
            required_pairs("B_WEB_MANUAL_BETA"),
            {("MANUAL", "TEXT"), ("MANUAL", "IMAGE_CAPTION")},
        )
        with self.assertRaises(ValueError):
            required_pairs("MANUAL")

    def test_wrong_coverage_rejected_before_network(self):
        with patch("urllib.request.build_opener") as network:
            for profile, count in (("B_WEB", 2), ("B_WEB_MANUAL_BETA", 6)):
                with self.subTest(profile=profile), self.assertRaises(ValueError):
                    probe(
                        "https://creator-loop-b-web.creator-loop-ai-beta.workers.dev",
                        [{}] * count,
                        "fixture-noncredential",
                        "fixture-deployment",
                        profile,
                    )
            network.assert_not_called()

    def test_foreign_origin_refused_before_network(self):
        origins = (
            "https://attacker.workers.dev",
            "http://creator-loop-b-web.creator-loop-ai-beta.workers.dev",
            "https://creator-loop-b-web.creator-loop-ai-beta.workers.dev/",
            "https://creator-loop-b-web.creator-loop-ai-beta.workers.dev@attacker.workers.dev",
        )
        with patch("urllib.request.build_opener") as network:
            for origin in origins:
                with self.subTest(origin=origin), self.assertRaises(ValueError):
                    probe(
                        origin, [{}] * 6, "fixture-noncredential", "fixture-deployment"
                    )
            network.assert_not_called()

    def test_redirect_refused_before_forwarding(self):
        request = urllib.request.Request("https://trusted.invalid/api/login")
        with self.assertRaises(urllib.error.HTTPError):
            NoRedirect().redirect_request(
                request, None, 302, "Found", {}, "https://attacker.invalid"
            )
