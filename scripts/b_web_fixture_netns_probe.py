"""Loopback-only browser probe; run in an independently isolated Linux netns.

This is not a Cloudflare QA runner and must not receive project credentials.
"""

from __future__ import annotations

import errno
import http.server
import os
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from scripts.b_web_cloud_deploy import (
    fixture_browser_api_get,
    fixture_browser_route,
    fixture_full_request_forecast,
    fixture_request_budget,
)


def verify_child_namespaces():
    """Refuse if visible driver/browser descendants escape this net namespace."""
    processes = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = dict(
                line.split(":", 1)
                for line in (entry / "status").read_text().splitlines()
                if ":" in line
            )
            processes[int(entry.name)] = (
                int(fields["PPid"].strip()),
                fields["Name"].strip(),
            )
        except (OSError, KeyError, ValueError):
            continue
    descendants = {os.getpid()}
    while True:
        expanded = descendants | {
            pid for pid, (parent, _) in processes.items() if parent in descendants
        }
        if expanded == descendants:
            break
        descendants = expanded
    selected = [(pid, processes[pid][1]) for pid in descendants if pid in processes]
    if not any("chrome" in name for _, name in selected) or not any(
        "node" in name for _, name in selected
    ):
        raise RuntimeError("Browser and Playwright driver descendants not confirmed")
    namespace = os.readlink("/proc/self/ns/net")
    for pid, _ in selected:
        try:
            if os.readlink(f"/proc/{pid}/ns/net") != namespace:
                raise RuntimeError("Child process escaped network namespace")
        except FileNotFoundError:
            raise RuntimeError(
                "Child process disappeared during namespace check"
            ) from None


def main():
    if os.name != "posix" or os.geteuid() == 0:
        raise RuntimeError("Run as an unprivileged user in an isolated Linux netns")
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("Network namespace is not loopback-only")
    with socket.socket() as probe:
        probe.settimeout(2)
        if probe.connect_ex(("203.0.113.1", 443)) not in (
            errno.ENETUNREACH,
            errno.EHOSTUNREACH,
            errno.EACCES,
            errno.EPERM,
        ):
            raise RuntimeError("External egress not proven unavailable")
    received = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            if self.path == "/disconnect":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/should-not-follow")
                self.end_headers()
                return
            body = (
                b'<html><img src="/asset"></html>' if self.path == "/" else b"fixture"
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with tempfile.TemporaryDirectory() as temp:
        cert, key = Path(temp) / "cert.pem", Path(temp) / "key.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(cert),
                "-days",
                "1",
                "-subj",
                "/CN=127.0.0.1",
            ],
            check=True,
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"https://127.0.0.1:{server.server_port}"
            plan = fixture_full_request_forecast(
                1, None, body_limit=0, cleanup=0, browser=5
            )
            start, budget = fixture_request_budget(
                Path(temp) / "budget.sqlite", "a" * 64, origin, plan
            )
            start()
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    executable_path="/usr/bin/google-chrome", headless=True
                )
                context = browser.new_context(
                    ignore_https_errors=True, service_workers="block"
                )
                context.route(
                    "**/*",
                    lambda route: fixture_browser_route(
                        route, budget.reserve_browser_url
                    ),
                )
                context.route_web_socket("**/*", lambda ws: ws.close())
                page = context.new_page()
                verify_child_namespaces()
                try:
                    context.request.get(
                        "https://203.0.113.1:443/",
                        max_redirects=0,
                        max_retries=0,
                        timeout=1500,
                    )
                except Exception:
                    pass
                else:
                    raise AssertionError("Playwright driver external egress succeeded")
                page.goto(origin + "/", wait_until="networkidle", timeout=15000)
                assert received.count("/") == 1 and received.count("/asset") == 1
                page.evaluate("""() => new Promise(resolve => {
                    const ws = new WebSocket('wss://127.0.0.1:' + location.port + '/ws');
                    ws.onclose = ws.onerror = () => resolve(true);
                    setTimeout(() => resolve(false), 1500);
                })""")
                assert "/ws" not in received
                page.evaluate("""() => Promise.race([
                    navigator.serviceWorker.register('/sw.js').then(() => true, () => false),
                    new Promise(resolve => setTimeout(() => resolve(false), 1500))
                ])""")
                assert "/sw.js" not in received
                try:
                    fixture_browser_api_get(
                        context.request,
                        origin + "/redirect",
                        budget.reserve_browser_url,
                    )
                    raise AssertionError("redirect accepted")
                except ValueError as error:
                    assert "redirect refused" in str(error)
                assert "/should-not-follow" not in received
                response = fixture_browser_api_get(
                    context.request, origin + "/api", budget.reserve_browser_url
                )
                assert response.status == 200
                response.dispose()
                try:
                    fixture_browser_api_get(
                        context.request,
                        origin + "/disconnect",
                        budget.reserve_browser_url,
                    )
                except Exception:
                    pass
                else:
                    raise AssertionError("Disconnected request unexpectedly succeeded")
                assert received.count("/disconnect") == 1
                before = list(received)
                try:
                    fixture_browser_api_get(
                        context.request,
                        origin + "/exhausted",
                        budget.reserve_browser_url,
                    )
                    raise AssertionError("exhausted budget accepted")
                except ValueError as error:
                    assert "browser budget exhausted" in str(error)
                assert received == before
                context.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    print("loopback browser route, asset, explicit API, redirect and exhaustion: PASS")
    print("service-worker/WebSocket blocking and child network namespaces: PASS")


if __name__ == "__main__":
    main()
