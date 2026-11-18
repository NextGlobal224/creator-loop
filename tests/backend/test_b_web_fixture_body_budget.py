"""Loopback-only evidence for the fixture oversized-body request budget."""

import http.server
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.b_web_cloud_deploy import (
    fixture_full_request_forecast,
    fixture_raw_body_limit_request,
    fixture_request_budget,
)


class FixtureBodyBudgetTests(unittest.TestCase):
    def test_loopback_requests_are_reserved_before_send_and_redirects_refused(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.path)
                length = int(self.headers["Content-Length"])
                self.rfile.read(length)
                self.send_response(302 if len(received) == 1 else 413)
                if len(received) == 1:
                    self.send_header("Location", "/must-not-follow")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        original_connect = socket.socket.connect

        def loopback_only(sock, address):
            if not isinstance(address, tuple) or address[0] != "127.0.0.1":
                raise AssertionError("Non-loopback network forbidden")
            return original_connect(sock, address)

        def local_transport(_url, headers, body):
            return fixture_raw_body_limit_request(
                f"http://127.0.0.1:{server.server_port}/probe", headers, body
            )

        plan = fixture_full_request_forecast(
            1, None, body_limit=2, cleanup=0, browser=0
        )
        body = b"{}" + b" " * (512 * 1024 - 1)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("socket.socket.connect", new=loopback_only),
            patch(
                "scripts.b_web_cloud_deploy.fixture_raw_body_limit_request",
                side_effect=local_transport,
            ),
        ):
            start, request = fixture_request_budget(
                Path(tmp) / "budget.sqlite", "a" * 64, "https://fixture.invalid", plan
            )
            start()
            target = "https://fixture.invalid/api/qa-snapshot-staging"
            with self.assertRaisesRegex(ValueError, "target"):
                request.raw_body_limit_probe(
                    "https://other.invalid/api/qa-snapshot-staging", {}, body
                )
            with self.assertRaisesRegex(ValueError, "body"):
                request.raw_body_limit_probe(target, {}, body[:-1])
            self.assertEqual(received, [])
            with self.assertRaisesRegex(RuntimeError, "Redirect refused"):
                request.raw_body_limit_probe(target, {}, body)
            for _ in range(plan["total"] - 1):
                self.assertEqual(request.raw_body_limit_probe(target, {}, body), 413)
            with self.assertRaisesRegex(ValueError, "budget exhausted"):
                request.raw_body_limit_probe(target, {}, body)
            self.assertEqual(len(received), plan["total"])
            self.assertNotIn("/must-not-follow", received)
