"""Fail-closed cloud deployment artifact and rollback boundaries, no cloud calls."""

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.b_web_cloud_deploy import (
    CHECKS,
    MODEL,
    REPO,
    activate_once,
    compatible_bindings,
    expected_binding_types,
    main,
    observe_activated_runtime,
    read_included_usage,
    read_json,
    require_idle_jobs,
    runtime_files,
    static_header_policy,
    storage_compatible,
    verify_ci,
    verify_remote_modules,
)


class CloudDeploymentTests(unittest.TestCase):
    def test_fixture_cloud_bootstrap_real_adapters_with_mock_transport(self):
        from scripts.b_web_cloud_deploy import fixture_bootstrap_cloud

        config = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "cloudflare/creator-web/wrangler.snapshot-qa.jsonc"
            ).read_text(encoding="utf-8")
        )
        content = hashlib.sha256(b"x").hexdigest()
        manifest = {"parts": [{"bytes": 1, "sha256": content}], "bytes": 1}
        digest = hashlib.sha256(
            json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        target = "qa-isolated-restore-" + "a" * 32
        source = "qa-isolated-restore-" + "b" * 32
        config["vars"].update(
            {
                "B_WEB_QA_RESTORE_OBJECT_NAME": target,
                "B_WEB_QA_SOURCE_OBJECT_NAME": source,
                "B_WEB_QA_RESTORE_SNAPSHOT_ID": "c" * 32,
                "B_WEB_QA_RESTORE_MANIFEST_SHA256": digest,
                "B_WEB_QA_EXPORT_SNAPSHOT_ID": "d" * 32,
                "B_WEB_QA_SOURCE_SNAPSHOT_ID": "e" * 32,
                "B_WEB_QA_SOURCE_MANIFEST_SHA256": "f" * 64,
            }
        )
        candidate = "12345678-1234-1234-1234-123456789abc"
        files = {
            "worker/wrangler.snapshot-qa.jsonc": b"{}",
            "worker/src/entry.py": b"pass",
        }
        observe = MagicMock(
            return_value=({}, {}, [{"version_id": candidate, "percentage": 100}])
        )
        events = []

        def request(url, headers, payload=None):
            operation = (payload or {}).get("operation")
            if url.endswith("/workers/workers"):
                events.append("create")
                return {
                    "success": True,
                    "result": {"name": "creator-loop-b-snapshot-qa", "id": "id"},
                }, {}
            if url.endswith("/workers/workers/creator-loop-b-snapshot-qa"):
                return {
                    "success": True,
                    "result": {"name": "creator-loop-b-snapshot-qa", "id": "id"},
                }, {}
            if url.endswith("/deployments"):
                events.append("activate")
                return {
                    "success": True,
                    "result": {
                        "versions": [{"version_id": candidate, "percentage": 100}]
                    },
                }, {}
            if url.endswith("/api/qa-snapshot-export"):
                body = {"qa_object": source, "qa_deployment": candidate}
                body.update(
                    {"manifest": manifest}
                    if operation == "manifest"
                    else {"position": 0, "sha256": content, "content_base64": "eA=="}
                )
                return body, {}
            if url.endswith("/api/qa-snapshot-staging"):
                events.append(operation)
                body = {"qa_object": target, "qa_deployment": candidate}
                body.update(
                    {"restored": False, "staged_parts": 1}
                    if operation == "status"
                    else {"stored": True, "duplicate": False}
                    if operation == "put"
                    else {"restored": True, "jobs_held": True, "alarm_restored": False}
                )
                return body, {}
            if url.endswith("/api/login"):
                events.append("login")
                return {"ok": True}, {"Set-Cookie": "__Host-creator=" + "a" * 64}
            return {"deployment": candidate}, {}

        with (
            tempfile.TemporaryDirectory() as temp,
            patch(
                "scripts.b_web_cloud_deploy.ACCOUNT_SHA",
                hashlib.sha256(b"fixture").hexdigest(),
            ),
            patch(
                "scripts.b_web_cloud_deploy.fixture_artifact_files", return_value=files
            ),
            patch("scripts.b_web_cloud_deploy.verify_fixture_artifact_config"),
            patch(
                "scripts.b_web_cloud_deploy.fixture_cloud_observer",
                return_value=observe,
            ),
            patch(
                "scripts.b_web_cloud_deploy.verify_fixture_namespace_ownership",
                return_value="c" * 32,
            ),
            patch("scripts.b_web_cloud_deploy.read_json", side_effect=request),
            patch(
                "scripts.b_web_cloud_deploy.subprocess.run",
                return_value=MagicMock(
                    returncode=0, stdout=("Worker Version ID: " + candidate).encode()
                ),
            ),
        ):
            args = (
                "fixture",
                {"Authorization": "Bearer fake-test-credential"},
                config,
                b"artifact",
                "a" * 40,
                "b" * 64,
                Path(temp) / "journal.sqlite",
                lambda _: ("a" * 32, "b" * 32),
                lambda: True,
                lambda _: True,
                lambda _: None,
                "operator",
                "password",
                manifest,
            )
            self.assertEqual(fixture_bootstrap_cloud(*args), "c" * 32)
            with self.assertRaisesRegex(ValueError, "already attempted"):
                fixture_bootstrap_cloud(*args)
        self.assertEqual(
            events,
            ["create", "activate", "status", "put", "status", "restore", "login"],
        )

    def test_fixture_cloud_bootstrap_composes_management_adapters_in_order(self):
        from scripts.b_web_cloud_deploy import fixture_bootstrap_cloud

        config = {
            "vars": {
                "B_WEB_QA_SOURCE_OBJECT_NAME": "source",
                "B_WEB_QA_RESTORE_OBJECT_NAME": "target",
                "B_WEB_QA_RESTORE_SNAPSHOT_ID": "snapshot",
                "B_WEB_QA_RESTORE_MANIFEST_SHA256": "manifest",
            }
        }
        candidate = "12345678-1234-1234-1234-123456789abc"
        steps = []
        observe = MagicMock(
            return_value=({}, {}, [{"version_id": candidate, "percentage": 100}])
        )

        def restore(version):
            return {
                "restored": True,
                "jobs_held": True,
                "alarm_restored": False,
                "target": "target",
                "snapshot_id": "snapshot",
                "manifest_sha256": "manifest",
                "deployment": version,
            }

        with (
            tempfile.TemporaryDirectory() as temp,
            patch("scripts.b_web_cloud_deploy.validate_fixture_config"),
            patch(
                "scripts.b_web_cloud_deploy.fixture_cloud_observer",
                return_value=observe,
            ),
            patch(
                "scripts.b_web_cloud_deploy.verify_fixture_namespace_ownership",
                return_value="c" * 32,
            ),
            patch(
                "scripts.b_web_cloud_deploy.fixture_transfer_adapter",
                return_value=lambda _: steps.append("transfer"),
            ),
            patch(
                "scripts.b_web_cloud_deploy.fixture_restore_adapter",
                return_value=restore,
            ),
            patch(
                "scripts.b_web_cloud_deploy.fixture_login_adapter",
                return_value=lambda version: {
                    "authenticated": True,
                    "deployment": version,
                },
            ),
            patch(
                "scripts.b_web_cloud_deploy.create_fixture_worker_metadata",
                side_effect=lambda *args: steps.append("create") or "worker",
            ),
            patch(
                "scripts.b_web_cloud_deploy.upload_fixture_worker_version",
                side_effect=lambda *args: steps.append("upload") or candidate,
            ),
            patch(
                "scripts.b_web_cloud_deploy.activate_fixture_worker_version",
                side_effect=lambda *args: steps.append("activate") or candidate,
            ),
        ):
            result = fixture_bootstrap_cloud(
                "account",
                {},
                config,
                b"artifact",
                "a" * 40,
                "b" * 64,
                Path(temp) / "journal.sqlite",
                lambda _: ("a" * 32, "b" * 32),
                lambda: True,
                lambda _: True,
                lambda _: None,
                "operator",
                "password",
                {},
            )
        self.assertEqual(result, "c" * 32)
        self.assertEqual(steps, ["create", "upload", "activate", "transfer"])

    def test_fixture_upload_checks_gates_and_unknown_result(self):
        from scripts.b_web_cloud_deploy import upload_fixture_worker_version

        account = "fixture"
        candidate = "12345678-1234-1234-1234-123456789abc"
        files = {
            "worker/wrangler.snapshot-qa.jsonc": b"{}",
            "worker/src/entry.py": b"pass",
        }
        args = (
            account,
            {"Authorization": "Bearer fake-test-credential"},
            {},
            b"artifact",
            "a" * 40,
            "b" * 64,
            "worker-id",
            lambda: True,
            lambda: True,
        )
        with (
            patch(
                "scripts.b_web_cloud_deploy.ACCOUNT_SHA",
                hashlib.sha256(account.encode()).hexdigest(),
            ),
            patch(
                "scripts.b_web_cloud_deploy.fixture_artifact_files", return_value=files
            ),
            patch("scripts.b_web_cloud_deploy.verify_fixture_artifact_config"),
            patch.dict(
                os.environ,
                {"CLOUDFLARE_API_KEY": "fake-conflict", "CF_FAKE": "fake-conflict"},
            ),
            patch(
                "scripts.b_web_cloud_deploy.inspect_fixture_worker_after_ambiguous_create",
                return_value={"state": "present", "worker_id": "worker-id"},
            ),
        ):
            with patch(
                "scripts.b_web_cloud_deploy.subprocess.run",
                return_value=MagicMock(
                    returncode=0, stdout=("Worker Version ID: " + candidate).encode()
                ),
            ) as run:
                self.assertEqual(upload_fixture_worker_version(*args), candidate)
                self.assertIn("upload", run.call_args.args[0])
                self.assertEqual(
                    run.call_args.kwargs["env"]["CLOUDFLARE_API_TOKEN"],
                    "fake-test-credential",
                )
                self.assertNotIn("CLOUDFLARE_API_KEY", run.call_args.kwargs["env"])
                self.assertNotIn("CF_FAKE", run.call_args.kwargs["env"])
            with (
                patch(
                    "scripts.b_web_cloud_deploy.subprocess.run",
                    side_effect=TimeoutError(),
                ),
                patch(
                    "scripts.b_web_cloud_deploy.read_json",
                    return_value=({"success": True}, {}),
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError, "metadata=available; do not retry"
                ):
                    upload_fixture_worker_version(*args)
            with patch("scripts.b_web_cloud_deploy.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "safety checks"):
                    upload_fixture_worker_version(
                        *args[:-2], lambda: False, lambda: True
                    )
                run.assert_not_called()
            with patch("scripts.b_web_cloud_deploy.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "bearer credential"):
                    upload_fixture_worker_version(account, {}, *args[2:])
                run.assert_not_called()
            with patch("scripts.b_web_cloud_deploy.subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "shell not confirmed"):
                    upload_fixture_worker_version(
                        *args[:6], "other-worker-id", *args[7:]
                    )
                run.assert_not_called()

    def test_real_fixture_worker_metadata_create_never_retries_unknown(self):
        from scripts.b_web_cloud_deploy import create_fixture_worker_metadata

        config = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "cloudflare/creator-web/wrangler.snapshot-qa.jsonc"
            ).read_text(encoding="utf-8")
        )
        account = "fixture-account"
        account_hash = hashlib.sha256(account.encode()).hexdigest()
        with patch("scripts.b_web_cloud_deploy.ACCOUNT_SHA", account_hash):
            with patch(
                "scripts.b_web_cloud_deploy.read_json",
                return_value=(
                    {
                        "success": True,
                        "result": {
                            "name": "creator-loop-b-snapshot-qa",
                            "id": "worker-id",
                        },
                    },
                    {},
                ),
            ) as read:
                self.assertEqual(
                    create_fixture_worker_metadata(account, {}, config, lambda: True),
                    "worker-id",
                )
                self.assertEqual(
                    read.call_args.args[2], {"name": "creator-loop-b-snapshot-qa"}
                )
            with patch(
                "scripts.b_web_cloud_deploy.read_json",
                side_effect=[
                    TimeoutError(),
                    RuntimeError("HTTPS operation refused: HTTP404"),
                ],
            ) as read:
                with self.assertRaisesRegex(
                    RuntimeError, "metadata=not_found; do not retry"
                ):
                    create_fixture_worker_metadata(account, {}, config, lambda: True)
                self.assertEqual(read.call_count, 2)
            with patch(
                "scripts.b_web_cloud_deploy.read_json",
                side_effect=[
                    ({"success": False}, {}),
                    RuntimeError("HTTPS operation refused: HTTP404"),
                ],
            ) as read:
                with self.assertRaisesRegex(
                    RuntimeError, "metadata=not_found; do not retry"
                ):
                    create_fixture_worker_metadata(account, {}, config, lambda: True)
                self.assertEqual(read.call_count, 2)
            with patch("scripts.b_web_cloud_deploy.read_json") as read:
                with self.assertRaisesRegex(ValueError, "already attempted"):
                    create_fixture_worker_metadata(account, {}, config, lambda: False)
                read.assert_not_called()

    def test_fixture_activation_requires_checks_and_never_retries_timeout(self):
        from scripts.b_web_cloud_deploy import activate_fixture_worker_version

        config = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "cloudflare/creator-web/wrangler.snapshot-qa.jsonc"
            ).read_text(encoding="utf-8")
        )
        candidate = "12345678-1234-1234-1234-123456789abc"
        versions = [{"version_id": candidate, "percentage": 100}]
        with patch(
            "scripts.b_web_cloud_deploy.ACCOUNT_SHA",
            hashlib.sha256(b"fixture").hexdigest(),
        ):
            with patch(
                "scripts.b_web_cloud_deploy.read_json",
                return_value=({"success": True, "result": {"versions": versions}}, {}),
            ) as read:
                self.assertEqual(
                    activate_fixture_worker_version(
                        "fixture", {}, config, candidate, lambda: True, lambda: True
                    ),
                    candidate,
                )
                self.assertEqual(
                    read.call_args.args[2]["code_update_strategy"],
                    {"mode": "immediate"},
                )
            with patch(
                "scripts.b_web_cloud_deploy.read_json",
                side_effect=[
                    TimeoutError(),
                    (
                        {
                            "success": True,
                            "result": {"deployments": [{"versions": versions}]},
                        },
                        {},
                    ),
                ],
            ) as read:
                with self.assertRaisesRegex(
                    RuntimeError, "metadata=candidate; do not retry"
                ):
                    activate_fixture_worker_version(
                        "fixture", {}, config, candidate, lambda: True, lambda: True
                    )
                self.assertEqual(read.call_count, 2)
            with patch(
                "scripts.b_web_cloud_deploy.read_json",
                side_effect=[TimeoutError(), TimeoutError()],
            ) as read:
                with self.assertRaisesRegex(
                    RuntimeError, "metadata=unavailable; do not retry"
                ):
                    activate_fixture_worker_version(
                        "fixture", {}, config, candidate, lambda: True, lambda: True
                    )
                self.assertEqual(read.call_count, 2)
            with patch("scripts.b_web_cloud_deploy.read_json") as read:
                with self.assertRaisesRegex(ValueError, "safety checks"):
                    activate_fixture_worker_version(
                        "fixture", {}, config, candidate, lambda: False, lambda: True
                    )
                read.assert_not_called()

    def test_fixture_activation_reservation_persists(self):
        from scripts.b_web_cloud_deploy import fixture_activation_reservation

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "journal.sqlite"
            self.assertTrue(fixture_activation_reservation(path)())
            self.assertFalse(fixture_activation_reservation(path)())

    def test_fixture_creation_reservation_across_processes(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "create.sqlite")
            code = (
                "import sys; from scripts.b_web_cloud_deploy import fixture_create_reservation; "
                "print(fixture_create_reservation(sys.argv[1])())"
            )
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", code, path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            outcomes = []
            for process in processes:
                try:
                    stdout, stderr = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    self.fail("fixture creation reservation process timed out")
                self.assertEqual(process.returncode, 0, stderr)
                outcomes.append(stdout.strip())
            self.assertCountEqual(outcomes, ["True", "False"])
            replay = subprocess.run(
                [sys.executable, "-c", code, path],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            self.assertEqual(replay.stdout.strip(), "False")

    def test_fixture_reservation_is_shared_across_processes_and_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "journal.sqlite")
            code = (
                "import sys; from scripts.b_web_cloud_deploy import fixture_restore_reservation; "
                "print(fixture_restore_reservation(sys.argv[1])("
                "'11111111-1111-1111-1111-111111111111', "
                "'qa-isolated-restore-'+'a'*32, 'b'*32, 'c'*64))"
            )
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", code, path],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            outcomes = []
            for process in processes:
                try:
                    stdout, stderr = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                    self.fail("fixture reservation process timed out")
                self.assertEqual(process.returncode, 0, stderr)
                outcomes.append(stdout.strip())
            self.assertCountEqual(outcomes, ["True", "False"])
            replay = subprocess.run(
                [sys.executable, "-c", code, path],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            self.assertEqual(replay.stdout.strip(), "False")

    def test_bootstrap_wires_restore_and_login_adapters(self):
        from scripts.b_web_cloud_deploy import fixture_bootstrap_with_adapters

        config = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "cloudflare/creator-web/wrangler.snapshot-qa.jsonc"
            ).read_text(encoding="utf-8")
        )
        part_hash = hashlib.sha256(b"x").hexdigest()
        manifest = {"parts": [{"bytes": 1, "sha256": part_hash}], "bytes": 1}
        digest = hashlib.sha256(
            json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        target = "qa-isolated-restore-" + "a" * 32
        candidate = "12345678-1234-1234-1234-123456789abc"
        config["vars"].update(
            {
                "B_WEB_QA_RESTORE_OBJECT_NAME": target,
                "B_WEB_QA_SOURCE_OBJECT_NAME": "qa-isolated-restore-" + "b" * 32,
                "B_WEB_QA_RESTORE_SNAPSHOT_ID": "c" * 32,
                "B_WEB_QA_RESTORE_MANIFEST_SHA256": digest,
                "B_WEB_QA_EXPORT_SNAPSHOT_ID": "d" * 32,
                "B_WEB_QA_SOURCE_SNAPSHOT_ID": "e" * 32,
                "B_WEB_QA_SOURCE_MANIFEST_SHA256": "f" * 64,
            }
        )
        calls = []

        def request(url, headers, payload=None):
            calls.append((url.rsplit("/", 1)[-1], payload and payload.get("operation")))
            if url.endswith("/api/qa-snapshot-export"):
                body = {
                    "qa_object": config["vars"]["B_WEB_QA_SOURCE_OBJECT_NAME"],
                    "qa_deployment": candidate,
                }
                if payload["operation"] == "manifest":
                    body["manifest"] = manifest
                else:
                    body.update(position=0, sha256=part_hash, content_base64="eA==")
                return body, {}
            if url.endswith("/api/qa-snapshot-staging"):
                body = {"qa_object": target, "qa_deployment": candidate}
                if payload["operation"] == "status":
                    body.update(restored=False, staged_parts=1)
                elif payload["operation"] == "put":
                    body.update(stored=True, duplicate=False)
                else:
                    body.update(restored=True, jobs_held=True, alarm_restored=False)
                return body, {}
            if url.endswith("/api/login"):
                return {"ok": True}, {
                    "Set-Cookie": "__Host-creator=" + "a" * 64 + "; Secure"
                }
            return {"deployment": candidate}, {}

        observe = MagicMock(
            return_value=({}, {}, [{"version_id": candidate, "percentage": 100}])
        )
        with (
            tempfile.TemporaryDirectory() as temp,
            patch("scripts.b_web_cloud_deploy.read_json", side_effect=request),
            patch(
                "scripts.b_web_cloud_deploy.verify_fixture_namespace_ownership",
                return_value="c" * 32,
            ),
        ):
            result = fixture_bootstrap_with_adapters(
                lambda _: ("a" * 32, "b" * 32),
                lambda _: candidate,
                observe,
                lambda _: None,
                config,
                "operator",
                "password",
                manifest,
                Path(temp) / "journal.sqlite",
            )
            create = MagicMock(side_effect=TimeoutError("unknown creation outcome"))
            args = (
                lambda _: ("a" * 32, "b" * 32),
                create,
                observe,
                lambda _: None,
                config,
                "operator",
                "password",
                manifest,
                Path(temp) / "unknown.sqlite",
            )
            with self.assertRaises(TimeoutError):
                fixture_bootstrap_with_adapters(*args)
            with self.assertRaisesRegex(ValueError, "Prior fixture creation attempt"):
                fixture_bootstrap_with_adapters(*args)
            create.assert_called_once()
            from scripts.b_web_cloud_deploy import (
                fixture_restore_adapter,
                fixture_transfer_adapter,
            )

            restored = {
                "qa_object": target,
                "qa_deployment": candidate,
                "restored": True,
                "jobs_held": True,
                "alarm_restored": False,
            }
            with patch(
                "scripts.b_web_cloud_deploy.read_json", return_value=(restored, {})
            ) as read:
                self.assertEqual(
                    fixture_transfer_adapter(config, "operator", manifest)(candidate), 0
                )
                receipt = fixture_restore_adapter(
                    config,
                    "operator",
                    manifest,
                    lambda *_: self.fail("must not reserve again"),
                )(candidate)
                self.assertTrue(receipt["restored"])
                self.assertEqual(read.call_count, 2)
        self.assertEqual(result, "c" * 32)
        self.assertEqual(
            calls,
            [
                ("qa-snapshot-staging", "status"),
                ("qa-snapshot-export", "manifest"),
                ("qa-snapshot-export", "part"),
                ("qa-snapshot-staging", "put"),
                ("qa-snapshot-staging", "status"),
                ("qa-snapshot-staging", "restore"),
                ("login", None),
                ("runtime", None),
            ],
        )

    def test_fixture_restore_reservation_survives_restart_and_race(self):
        from scripts.b_web_cloud_deploy import fixture_restore_reservation

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "restore-journal.sqlite"
            identity = (
                "11111111-1111-1111-1111-111111111111",
                "qa-isolated-restore-" + "a" * 32,
                "b" * 32,
                "c" * 64,
            )
            barrier = threading.Barrier(2)
            outcomes = []

            def attempt():
                reserve = fixture_restore_reservation(path)
                barrier.wait()
                outcomes.append(reserve(*identity))

            workers = [threading.Thread(target=attempt) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
            self.assertCountEqual(outcomes, [True, False])
            reserve = fixture_restore_reservation(path)
            self.assertFalse(reserve(*identity))
            self.assertFalse(reserve(identity[0], identity[1], identity[2], "d" * 64))
            with self.assertRaises(ValueError):
                reserve(identity[0], "production", identity[2], identity[3])
        with self.assertRaises(ValueError):
            fixture_restore_reservation(Path("relative.sqlite"))

    def test_ambiguous_fixture_create_metadata_never_authorizes_retry(self):
        from scripts.b_web_cloud_deploy import (
            inspect_fixture_worker_after_ambiguous_create,
        )

        account = "fixture-account"
        with patch(
            "scripts.b_web_cloud_deploy.ACCOUNT_SHA",
            hashlib.sha256(account.encode()).hexdigest(),
        ):
            with patch(
                "scripts.b_web_cloud_deploy.read_json",
                side_effect=RuntimeError("HTTPS operation refused: HTTP404"),
            ):
                self.assertEqual(
                    inspect_fixture_worker_after_ambiguous_create(account, {}),
                    {"state": "not_found", "retry_permitted": False},
                )
            with patch(
                "scripts.b_web_cloud_deploy.read_json",
                return_value=(
                    {
                        "success": True,
                        "result": {
                            "name": "creator-loop-b-snapshot-qa",
                            "id": "fixture-id",
                        },
                    },
                    {},
                ),
            ):
                observed = inspect_fixture_worker_after_ambiguous_create(account, {})
                self.assertEqual(observed["state"], "present")
                self.assertIs(observed["retry_permitted"], False)
            for response in (
                RuntimeError("HTTPS operation refused: HTTP403"),
                (
                    {"success": True, "result": {"name": "other", "id": "fixture-id"}},
                    {},
                ),
            ):
                with (
                    patch(
                        "scripts.b_web_cloud_deploy.read_json",
                        side_effect=response
                        if isinstance(response, Exception)
                        else None,
                        return_value=response,
                    ),
                    self.assertRaises((RuntimeError, ValueError)),
                ):
                    inspect_fixture_worker_after_ambiguous_create(account, {})

    def test_fixture_restore_adapter_status_reservation_and_no_blind_retry(self):
        from scripts.b_web_cloud_deploy import fixture_restore_adapter

        config = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "cloudflare/creator-web/wrangler.snapshot-qa.jsonc"
            ).read_text(encoding="utf-8")
        )
        manifest = {"parts": [{"bytes": 1, "sha256": "a" * 64}], "bytes": 1}
        encoded = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        target = "qa-isolated-restore-" + "a" * 32
        config["vars"].update(
            {
                "B_WEB_QA_RESTORE_OBJECT_NAME": target,
                "B_WEB_QA_SOURCE_OBJECT_NAME": "qa-isolated-restore-" + "b" * 32,
                "B_WEB_QA_RESTORE_SNAPSHOT_ID": "c" * 32,
                "B_WEB_QA_RESTORE_MANIFEST_SHA256": hashlib.sha256(encoded).hexdigest(),
                "B_WEB_QA_EXPORT_SNAPSHOT_ID": "d" * 32,
                "B_WEB_QA_SOURCE_SNAPSHOT_ID": "e" * 32,
                "B_WEB_QA_SOURCE_MANIFEST_SHA256": "f" * 64,
            }
        )
        version = "11111111-1111-1111-1111-111111111111"
        status = {
            "qa_object": target,
            "qa_deployment": version,
            "restored": False,
            "staged_parts": 1,
        }
        completed = {
            "qa_object": target,
            "qa_deployment": version,
            "restored": True,
            "jobs_held": True,
            "alarm_restored": False,
        }
        oversized = {**manifest, "padding": "x" * (512 * 1024)}
        oversized_config = copy.deepcopy(config)
        oversized_config["vars"]["B_WEB_QA_RESTORE_MANIFEST_SHA256"] = hashlib.sha256(
            json.dumps(
                oversized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        with self.assertRaisesRegex(ValueError, "body limit"):
            fixture_restore_adapter(
                oversized_config, "fixture-key", oversized, MagicMock()
            )
        for scenario in (
            "success",
            "already",
            "timeout",
            "timeout_reconciled",
            "wrong_object",
            "missing_parts",
            "no_reservation",
        ):
            with self.subTest(scenario=scenario):
                first = copy.deepcopy(completed if scenario == "already" else status)
                if scenario == "wrong_object":
                    first["qa_object"] = "other"
                if scenario == "missing_parts":
                    first["staged_parts"] = 0
                second = (
                    TimeoutError("unknown")
                    if scenario in ("timeout", "timeout_reconciled")
                    else (completed, {})
                )
                reserve = MagicMock(return_value=scenario != "no_reservation")
                adapter = fixture_restore_adapter(
                    config, "fixture-key", manifest, reserve
                )
                with patch(
                    "scripts.b_web_cloud_deploy.read_json",
                    side_effect=[
                        (first, {}),
                        second,
                        (completed if scenario == "timeout_reconciled" else status, {}),
                    ],
                ) as request:
                    if scenario in ("success", "already"):
                        self.assertEqual(adapter(version)["target"], target)
                    else:
                        with self.assertRaises((ValueError, TimeoutError)):
                            adapter(version)
                    if scenario == "timeout":
                        with self.assertRaisesRegex(ValueError, "no retry"):
                            adapter(version)
                        self.assertEqual(request.call_count, 3)
                        reserve.assert_called_once()
                    if scenario == "timeout_reconciled":
                        self.assertEqual(adapter(version)["target"], target)
                        self.assertEqual(request.call_count, 3)
                        reserve.assert_called_once()
                    if scenario in ("already", "wrong_object", "missing_parts"):
                        reserve.assert_not_called()

    def test_fixture_login_adapter_checks_cookie_and_runtime_version(self):
        from scripts.b_web_cloud_deploy import fixture_login_adapter

        config = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "cloudflare/creator-web/wrangler.snapshot-qa.jsonc"
            ).read_text(encoding="utf-8")
        )
        candidate = "11111111-1111-1111-1111-111111111111"
        for scenario in ("success", "bad_login", "bad_cookie", "wrong_version"):
            with self.subTest(scenario=scenario):
                login = {"ok": scenario != "bad_login"}
                cookie = (
                    "bad"
                    if scenario == "bad_cookie"
                    else "__Host-creator=" + "a" * 64 + "; Secure"
                )
                runtime = {
                    "deployment": "22222222-2222-2222-2222-222222222222"
                    if scenario == "wrong_version"
                    else candidate
                }
                with patch(
                    "scripts.b_web_cloud_deploy.read_json",
                    side_effect=[(login, {"Set-Cookie": cookie}), (runtime, {})],
                ) as request:
                    adapter = fixture_login_adapter(
                        config, "fixture-operator", "fixture-password"
                    )
                    if scenario == "success":
                        self.assertEqual(
                            adapter(candidate),
                            {"authenticated": True, "deployment": candidate},
                        )
                        self.assertEqual(request.call_count, 2)
                        self.assertEqual(
                            request.call_args_list[0].args[1]["X-QA-Expected-Version"],
                            candidate,
                        )
                        self.assertEqual(
                            request.call_args_list[1].args[1]["Cookie"],
                            "__Host-creator=" + "a" * 64,
                        )
                    else:
                        with self.assertRaises(ValueError):
                            adapter(candidate)
                        self.assertEqual(
                            request.call_count, 2 if scenario == "wrong_version" else 1
                        )

    def test_unverified_qa_targets_refuse_before_network_or_login(self):
        env = {
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": "a" * 40,
            "B_CI_RUN": "8",
            "B_OPERATION": "deploy",
            "B_EXPECTED_DEPLOYMENT": "12345678-1234-1234-1234-123456789abc",
        }
        for target, reason in (("qa19", "quiescence"), ("qa-fixture", "bootstrap")):
            with (
                self.subTest(target=target),
                patch.dict(os.environ, {**env, "B_TARGET": target}),
                patch("scripts.b_web_cloud_deploy.read_json") as http,
                patch("scripts.b_web_cloud_deploy.subprocess.check_output") as gh,
            ):
                with self.assertRaisesRegex(ValueError, reason):
                    main()
                http.assert_not_called()
                gh.assert_not_called()

    def test_fixture_configuration_cannot_bind_production_or_ai(self):
        from pathlib import Path

        from scripts.b_web_cloud_deploy import TARGETS, validate_fixture_config

        path = (
            Path(__file__).resolve().parents[2]
            / "cloudflare/creator-web/wrangler.snapshot-qa.jsonc"
        )
        config = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(config["name"], TARGETS["qa-fixture"])
        self.assertNotIn(config["name"], (TARGETS["production"], TARGETS["qa19"]))
        self.assertEqual(
            config["migrations"], [{"tag": "v1", "new_sqlite_classes": ["Database"]}]
        )
        self.assertEqual(
            config["durable_objects"]["bindings"],
            [{"name": "DATABASE", "class_name": "Database"}],
        )
        self.assertNotIn("ai", config)
        self.assertNotIn("browser", config)
        self.assertEqual(config["vars"]["B_WEB_OBSERVATION_ONLY"], "1")
        self.assertEqual(
            config["vars"]["B_WEB_QA_HOSTNAME"],
            "creator-loop-b-snapshot-qa.creator-loop-ai-beta.workers.dev",
        )
        validate_fixture_config(config)
        expected = expected_binding_types(
            "qa-fixture", {"resources": {"bindings": []}}, config
        )
        self.assertNotIn("AI", expected)
        self.assertNotIn("BROWSER", expected)
        self.assertEqual(expected["OPERATOR_KEY"], "secret_text")
        self.assertEqual(expected["LOGIN_PASSWORD_HASH"], "secret_text")
        with self.assertRaises(ValueError):
            expected_binding_types("qa-fixture", {"resources": {"bindings": []}})
        configured = copy.deepcopy(config)
        configured["vars"].update(
            {
                "B_WEB_QA_RESTORE_OBJECT_NAME": "qa-isolated-restore-" + "d" * 32,
                "B_WEB_QA_RESTORE_SNAPSHOT_ID": "a" * 32,
                "B_WEB_QA_RESTORE_MANIFEST_SHA256": "b" * 64,
                "B_WEB_QA_EXPORT_SNAPSHOT_ID": "c" * 32,
                "B_WEB_QA_SOURCE_OBJECT_NAME": "qa-isolated-restore-" + "e" * 32,
                "B_WEB_QA_SOURCE_SNAPSHOT_ID": "f" * 32,
                "B_WEB_QA_SOURCE_MANIFEST_SHA256": "1" * 64,
            }
        )
        expected_configured = expected_binding_types(
            "qa-fixture", {"resources": {"bindings": []}}, configured
        )
        self.assertEqual(
            expected_configured["B_WEB_QA_RESTORE_MANIFEST_SHA256"], "plain_text"
        )
        incomplete = copy.deepcopy(configured)
        del incomplete["vars"]["B_WEB_QA_EXPORT_SNAPSHOT_ID"]
        with self.assertRaises(ValueError):
            validate_fixture_config(incomplete)
        same = copy.deepcopy(configured)
        same["vars"]["B_WEB_QA_SOURCE_OBJECT_NAME"] = same["vars"][
            "B_WEB_QA_RESTORE_OBJECT_NAME"
        ]
        with self.assertRaises(ValueError):
            validate_fixture_config(same)
        configured["vars"]["B_WEB_QA_RESTORE_OBJECT_NAME"] = "private-owner-v1"
        with self.assertRaises(ValueError):
            validate_fixture_config(configured)
        for field, value in (
            ("name", TARGETS["production"]),
            ("ai", {"binding": "AI"}),
            ("compatibility_flags", []),
        ):
            bad = copy.deepcopy(config)
            bad[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_fixture_config(bad)

    def test_fixture_namespace_must_be_distinct_and_have_no_external_service(self):
        from scripts.b_web_cloud_deploy import validate_fixture_namespace

        production, qa19, fixture = "a" * 32, "b" * 32, "c" * 32
        binding = {
            "name": "DATABASE",
            "type": "durable_object_namespace",
            "namespace_id": fixture,
            "class_name": "Database",
        }
        self.assertEqual(
            validate_fixture_namespace({"bindings": [binding]}, production, qa19),
            fixture,
        )
        for change in (
            {**binding, "namespace_id": production},
            {**binding, "namespace_id": qa19},
            {**binding, "script_name": "creator-loop-b-web"},
        ):
            with self.subTest(binding=change), self.assertRaises(ValueError):
                validate_fixture_namespace({"bindings": [change]}, production, qa19)
        with self.assertRaises(ValueError):
            validate_fixture_namespace(
                {"bindings": [binding, {"name": "AI", "type": "ai"}]}, production, qa19
            )
        for extra in (
            {"name": "UNEXPECTED"},
            {"name": "UNEXPECTED", "type": None},
            {"name": None, "type": None},
            {"name": "ASSETS"},
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate_fixture_namespace(
                    {"bindings": [binding, extra]}, production, qa19
                )

    def test_fixture_runtime_binding_proof_requires_exact_values_and_secrets(self):
        from pathlib import Path

        from scripts.b_web_cloud_deploy import (
            expected_binding_types,
            verify_fixture_bindings,
            verify_fixture_namespace_ownership,
        )

        path = (
            Path(__file__).resolve().parents[2]
            / "cloudflare/creator-web/wrangler.snapshot-qa.jsonc"
        )
        config = json.loads(path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "identities"):
            verify_fixture_bindings(
                {
                    "bindings": [
                        {
                            "name": "DATABASE",
                            "type": "durable_object_namespace",
                            "namespace_id": "c" * 32,
                            "class_name": "Database",
                        }
                    ]
                },
                config,
                "a" * 32,
                "b" * 32,
            )
        config["vars"].update(
            {
                "B_WEB_QA_RESTORE_OBJECT_NAME": "qa-isolated-restore-" + "d" * 32,
                "B_WEB_QA_RESTORE_SNAPSHOT_ID": "a" * 32,
                "B_WEB_QA_RESTORE_MANIFEST_SHA256": "b" * 64,
                "B_WEB_QA_EXPORT_SNAPSHOT_ID": "c" * 32,
                "B_WEB_QA_SOURCE_OBJECT_NAME": "qa-isolated-restore-" + "e" * 32,
                "B_WEB_QA_SOURCE_SNAPSHOT_ID": "f" * 32,
                "B_WEB_QA_SOURCE_MANIFEST_SHA256": "1" * 64,
            }
        )
        expected = expected_binding_types(
            "qa-fixture", {"resources": {"bindings": []}}, config
        )
        bindings = [{"name": name, "type": kind} for name, kind in expected.items()]
        for binding in bindings:
            name = binding["name"]
            if name in config["vars"]:
                binding["text"] = config["vars"][name]
            if name == "DATABASE":
                binding.update(namespace_id="c" * 32, class_name="Database")

        def verify(value):
            return verify_fixture_bindings(value, config, "a" * 32, "b" * 32)

        self.assertEqual(verify({"bindings": bindings}), "c" * 32)
        namespace_rows = [
            {"id": name * 32, "script": script, "class": "Database", "use_sqlite": True}
            for name, script in (
                ("a", "creator-loop-b-web"),
                ("b", "creator-loop-b-web-qa19"),
                ("c", "creator-loop-b-snapshot-qa"),
            )
        ]
        observed = {
            "success": True,
            "result": namespace_rows,
            "result_info": {"total_count": 3},
        }

        def verify_owner(value):
            return verify_fixture_namespace_ownership(
                {"bindings": bindings}, config, "a" * 32, "b" * 32, value
            )

        self.assertEqual(verify_owner(observed), "c" * 32)
        for changed in (
            {**observed, "result": namespace_rows[:-1]},
            {**observed, "result_info": {"total_count": 3, "cursor": "next"}},
            {**observed, "result_info": {"total_count": 3, "cursor": []}},
            {**observed, "result_info": {"total_count": 3, "has_more": 0}},
            {**observed, "result_info": {"total_count": 3, "total_pages": True}},
            {
                **observed,
                "result": [
                    *namespace_rows[:2],
                    {**namespace_rows[2], "script": "other-worker"},
                ],
            },
            {
                **observed,
                "result": [
                    *namespace_rows[:2],
                    {**namespace_rows[2], "use_sqlite": False},
                ],
            },
        ):
            with self.subTest(metadata=changed), self.assertRaises(ValueError):
                verify_owner(changed)
        for missing in (
            "OPERATOR_KEY",
            "LOGIN_PASSWORD_HASH",
            "B_WEB_OBSERVATION_ONLY",
        ):
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                verify({"bindings": [b for b in bindings if b["name"] != missing]})
        bad = copy.deepcopy(bindings)
        next(b for b in bad if b["name"] == "B_WEB_OBSERVATION_ONLY")["text"] = "0"
        with self.assertRaises(ValueError):
            verify({"bindings": bad})

    def test_production_fails_before_any_network_or_activation(self):
        env = {
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": "a" * 40,
            "B_CI_RUN": "8",
            "B_TARGET": "production",
            "B_EXPECTED_DEPLOYMENT": "12345678-1234-1234-1234-123456789abc",
            "B_VERIFY_SOURCE_COMMIT": "a" * 40,
        }
        for operation in ("deploy", "rollback", "verify"):
            with (
                self.subTest(operation=operation),
                patch.dict(os.environ, {**env, "B_OPERATION": operation}),
                patch("scripts.b_web_cloud_deploy.read_json") as http,
                patch("scripts.b_web_cloud_deploy.subprocess.check_output") as gh,
                patch("scripts.b_web_cloud_deploy.activate_once") as activate,
            ):
                with self.assertRaisesRegex(ValueError, "non-activating"):
                    main()
                http.assert_not_called()
                gh.assert_not_called()
                activate.assert_not_called()

    def test_fixture_bootstrap_orders_creation_verification_and_login(self):
        from scripts.b_web_cloud_deploy import fixture_bootstrap_sequence

        candidate = "12345678-1234-1234-1234-123456789abc"
        config = {
            "vars": {
                "B_WEB_QA_SOURCE_OBJECT_NAME": "fixture",
                "B_WEB_QA_RESTORE_OBJECT_NAME": "target",
                "B_WEB_QA_RESTORE_SNAPSHOT_ID": "snapshot",
                "B_WEB_QA_RESTORE_MANIFEST_SHA256": "manifest",
            }
        }
        steps = []
        preflight = MagicMock(
            side_effect=lambda _: steps.append("preflight") or ("a" * 32, "b" * 32)
        )
        create = MagicMock(side_effect=lambda _: steps.append("create") or candidate)
        observe = MagicMock(
            return_value=({}, {}, [{"version_id": candidate, "percentage": 100}])
        )
        restore = MagicMock(
            side_effect=lambda version: (
                steps.append("restore")
                or {
                    "restored": True,
                    "jobs_held": True,
                    "alarm_restored": False,
                    "target": "target",
                    "snapshot_id": "snapshot",
                    "manifest_sha256": "manifest",
                    "deployment": candidate,
                }
            )
        )
        login = MagicMock(
            side_effect=lambda version: (
                steps.append("login")
                or {"authenticated": True, "deployment": candidate}
            )
        )
        record = MagicMock(side_effect=lambda event: steps.append(event["state"]))
        with (
            patch("scripts.b_web_cloud_deploy.validate_fixture_config"),
            patch(
                "scripts.b_web_cloud_deploy.verify_fixture_namespace_ownership",
                return_value="c" * 32,
            ) as verify,
        ):
            self.assertEqual(
                fixture_bootstrap_sequence(
                    preflight, create, observe, restore, login, record, config
                ),
                "c" * 32,
            )
            verify.assert_called_once()
        self.assertEqual(
            steps,
            [
                "preflight",
                "creation_attempting",
                "create",
                "creation_returned",
                "metadata_verified",
                "restore",
                "restore_verified",
                "login",
                "login_verified",
            ],
        )
        create.assert_called_once_with(config)
        observe.assert_called_once_with(candidate, "a" * 32, "b" * 32)
        restore.assert_called_once_with(candidate)
        login.assert_called_once_with(candidate)

    def test_bootstrap_real_cloud_observer_refuses_wrong_account_before_object_use(
        self,
    ):
        from scripts.b_web_cloud_deploy import (
            fixture_bootstrap_sequence,
            fixture_cloud_observer,
        )

        candidate = "12345678-1234-1234-1234-123456789abc"
        config = {"vars": {"B_WEB_QA_SOURCE_OBJECT_NAME": "fixture"}}
        create = MagicMock(return_value=candidate)
        restore, login, record = MagicMock(), MagicMock(), MagicMock()
        with (
            patch("scripts.b_web_cloud_deploy.validate_fixture_config"),
            self.assertRaisesRegex(ValueError, "Wrong Cloudflare account"),
        ):
            observer = fixture_cloud_observer(
                "wrong-account", {}, config, b"archive", "a" * 40, "b" * 64
            )
            fixture_bootstrap_sequence(
                lambda _: ("a" * 32, "b" * 32),
                create,
                observer,
                restore,
                login,
                record,
                config,
            )
        restore.assert_not_called()
        login.assert_not_called()
        create.assert_not_called()
        record.assert_not_called()

    def test_fixture_observer_rejects_bad_artifact_before_worker_creation(self):
        from scripts.b_web_cloud_deploy import fixture_cloud_observer

        account = "fixture-account"
        with (
            patch(
                "scripts.b_web_cloud_deploy.ACCOUNT_SHA",
                hashlib.sha256(account.encode()).hexdigest(),
            ),
            self.assertRaisesRegex(ValueError, "Fixture artifact identity mismatch"),
        ):
            fixture_cloud_observer(account, {}, {}, b"bad", "a" * 40, "0" * 64)

    def test_fixture_precreate_rejects_configuration_mutation(self):
        from scripts.b_web_cloud_deploy import fixture_bootstrap_sequence

        for mutator in ("preflight", "record"):
            with self.subTest(mutator=mutator):
                config = {"vars": {"B_WEB_QA_SOURCE_OBJECT_NAME": "fixture"}}
                expected = copy.deepcopy(config)
                observer = MagicMock()

                def precreate(value):
                    if value != expected:
                        raise ValueError(
                            "Fixture configuration changed before creation"
                        )

                observer.precreate.side_effect = precreate

                def preflight(value):
                    if mutator == "preflight":
                        value["untrusted"] = True
                    return "a" * 32, "b" * 32

                def record(event):
                    if mutator == "record" and event["state"] == "creation_attempting":
                        config["untrusted"] = True

                create = MagicMock()
                with (
                    patch("scripts.b_web_cloud_deploy.validate_fixture_config"),
                    self.assertRaisesRegex(ValueError, "configuration changed"),
                ):
                    fixture_bootstrap_sequence(
                        preflight,
                        create,
                        observer,
                        MagicMock(),
                        MagicMock(),
                        record,
                        config,
                    )
                create.assert_not_called()

    def test_fixture_bootstrap_fails_closed_before_or_after_creation(self):
        from scripts.b_web_cloud_deploy import fixture_bootstrap_sequence

        config = {
            "vars": {
                "B_WEB_QA_SOURCE_OBJECT_NAME": "fixture",
                "B_WEB_QA_RESTORE_OBJECT_NAME": "target",
                "B_WEB_QA_RESTORE_SNAPSHOT_ID": "snapshot",
                "B_WEB_QA_RESTORE_MANIFEST_SHA256": "manifest",
            }
        }
        candidate = "12345678-1234-1234-1234-123456789abc"
        for failure in (
            "missing_config",
            "permission",
            "resource",
            "bad_ids",
            "creation",
            "namespace",
            "version",
            "restore",
            "restore_target",
            "restore_snapshot",
            "restore_manifest",
            "restore_deployment",
            "login",
            "login_false",
        ):
            with self.subTest(failure=failure):
                preflight = MagicMock(return_value=("a" * 32, "b" * 32))
                create = MagicMock(return_value=candidate)
                observe = MagicMock(
                    return_value=(
                        {},
                        {},
                        [{"version_id": candidate, "percentage": 100}],
                    )
                )
                login = MagicMock(
                    return_value={"authenticated": True, "deployment": candidate}
                )
                restore = MagicMock(
                    return_value={
                        "restored": True,
                        "jobs_held": True,
                        "alarm_restored": False,
                        "target": "target",
                        "snapshot_id": "snapshot",
                        "manifest_sha256": "manifest",
                        "deployment": candidate,
                    }
                )
                record = MagicMock()
                if failure in ("permission", "resource"):
                    preflight.side_effect = ValueError(failure)
                if failure == "bad_ids":
                    preflight.return_value = ("a" * 32, "a" * 32)
                if failure == "creation":
                    create.side_effect = TimeoutError("unknown")
                if failure == "version":
                    observe.return_value = (
                        {},
                        {},
                        [{"version_id": candidate, "percentage": 50}],
                    )
                if failure == "login":
                    login.side_effect = ValueError("login refused")
                if failure == "restore":
                    restore.return_value = {
                        "restored": False,
                        "jobs_held": True,
                        "alarm_restored": False,
                    }
                if failure.startswith("restore_"):
                    field = {
                        "restore_target": "target",
                        "restore_snapshot": "snapshot_id",
                        "restore_manifest": "manifest_sha256",
                        "restore_deployment": "deployment",
                    }[failure]
                    restore.return_value[field] = "wrong"
                if failure == "login_false":
                    login.return_value = {
                        "authenticated": False,
                        "deployment": candidate,
                    }
                with (
                    patch("scripts.b_web_cloud_deploy.validate_fixture_config"),
                    patch(
                        "scripts.b_web_cloud_deploy.verify_fixture_namespace_ownership",
                        side_effect=ValueError("wrong namespace")
                        if failure == "namespace"
                        else None,
                        return_value="c" * 32,
                    ),
                    self.assertRaises((ValueError, TimeoutError)),
                ):
                    fixture_bootstrap_sequence(
                        preflight,
                        create,
                        observe,
                        restore,
                        login,
                        record,
                        {"vars": {}} if failure == "missing_config" else config,
                    )
                if failure in ("login", "login_false"):
                    login.assert_called_once()
                else:
                    login.assert_not_called()
                if failure in ("missing_config", "permission", "resource", "bad_ids"):
                    create.assert_not_called()
                else:
                    create.assert_called_once()
                    self.assertIn(
                        record.call_args_list[-1].args[0]["state"],
                        (
                            "creation_unknown",
                            "postcreate_unverified",
                            "restore_unverified",
                            "login_unverified",
                        ),
                    )

    def test_fixture_cloud_observer_reads_only_management_metadata(self):
        from pathlib import Path

        from scripts.b_web_cloud_deploy import (
            expected_binding_types,
            observe_fixture_cloud,
        )

        account = "fixture-account"
        config = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "cloudflare/creator-web/wrangler.snapshot-qa.jsonc"
            ).read_text(encoding="utf-8")
        )
        artifact_config = copy.deepcopy(config)
        config["vars"].update(
            {
                "B_WEB_QA_RESTORE_OBJECT_NAME": "qa-isolated-restore-" + "d" * 32,
                "B_WEB_QA_RESTORE_SNAPSHOT_ID": "a" * 32,
                "B_WEB_QA_RESTORE_MANIFEST_SHA256": "b" * 64,
                "B_WEB_QA_EXPORT_SNAPSHOT_ID": "c" * 32,
                "B_WEB_QA_SOURCE_OBJECT_NAME": "qa-isolated-restore-" + "e" * 32,
                "B_WEB_QA_SOURCE_SNAPSHOT_ID": "f" * 32,
                "B_WEB_QA_SOURCE_MANIFEST_SHA256": "1" * 64,
            }
        )
        bindings = [
            {"name": name, "type": kind}
            for name, kind in expected_binding_types(
                "qa-fixture", {"resources": {"bindings": []}}, config
            ).items()
        ]
        for item in bindings:
            if item["name"] in config["vars"]:
                item["text"] = config["vars"][item["name"]]
            if item["name"] == "DATABASE":
                item.update(namespace_id="c" * 32, class_name="Database")
        candidate = "12345678-1234-1234-1234-123456789abc"
        settings = {
            "id": candidate,
            "compatibility_date": "2026-10-13",
            "compatibility_flags": ["python_workers"],
            "modules": [
                {
                    "name": "entry.py",
                    "content_base64": "c291cmNl",
                    "content_type": "text/x-python",
                }
            ],
            "bindings": bindings,
        }
        namespaces = {
            "success": True,
            "result_info": {"total_count": 3},
            "result": [
                {
                    "id": name * 32,
                    "script": script,
                    "class": "Database",
                    "use_sqlite": True,
                }
                for name, script in (
                    ("a", "creator-loop-b-web"),
                    ("b", "creator-loop-b-web-qa19"),
                    ("c", "creator-loop-b-snapshot-qa"),
                )
            ],
        }
        deployment = {
            "success": True,
            "result": {
                "deployments": [
                    {"versions": [{"version_id": candidate, "percentage": 100}]}
                ]
            },
        }
        for scenario in (
            "valid_but_unfenced",
            "wrong_traffic",
            "wrong_version",
            "missing_version",
            "missing_modules",
            "wrong_bytes",
            "invalid_encoding",
            "duplicate_modules",
            "artifact_collision",
            "invalid_artifact_bytes",
            "wrong_content_type",
            "missing_content_type",
            "wrong_artifact_config",
            "changed_traffic",
        ):
            with self.subTest(scenario=scenario):
                first = copy.deepcopy(deployment)
                final = copy.deepcopy(deployment)
                body = copy.deepcopy(settings)
                files = {
                    "worker/src/entry.py": b"source",
                    "worker/src/creator_ai.mjs": b"js",
                    "worker/src/migrations/001.sql": b"sql",
                    "worker/python_modules/workers/workflows.js": b"sdk",
                    "worker/python_modules/workers/__init__.py": b"init",
                    "worker/wrangler.snapshot-qa.jsonc": json.dumps(
                        artifact_config
                    ).encode(),
                }
                body["modules"].extend(
                    [
                        {
                            "name": "creator_ai.mjs",
                            "content_base64": "anM=",
                            "content_type": "application/javascript+module",
                        },
                        {
                            "name": "migrations/001.sql",
                            "content_base64": "c3Fs",
                            "content_type": "text/plain",
                        },
                        {
                            "name": "python_modules/workers/workflows.js",
                            "content_base64": "c2Rr",
                            "content_type": "application/javascript+module",
                        },
                        {
                            "name": "python_modules/workers/__init__.py",
                            "content_base64": "aW5pdA==",
                            "content_type": "application/octet-stream",
                        },
                    ]
                )
                if scenario == "wrong_traffic":
                    first["result"]["deployments"][0]["versions"][0]["version_id"] = (
                        "other"
                    )
                if scenario == "wrong_version":
                    body["id"] = "other"
                if scenario == "missing_version":
                    del body["id"]
                if scenario == "missing_modules":
                    del body["modules"]
                if scenario == "wrong_bytes":
                    body["modules"][0]["content_base64"] = "d3Jvbmc="
                if scenario == "invalid_encoding":
                    body["modules"][0]["content_base64"] = "!invalid!"
                if scenario == "duplicate_modules":
                    body["modules"].append(copy.deepcopy(body["modules"][0]))
                if scenario == "artifact_collision":
                    files.update(
                        {
                            "worker/src/python_modules/x.py": b"a",
                            "worker/python_modules/x.py": b"b",
                        }
                    )
                if scenario == "invalid_artifact_bytes":
                    files["worker/src/entry.py"] = "source"
                if scenario == "wrong_content_type":
                    body["modules"][0]["content_type"] = "text/plain"
                if scenario == "missing_content_type":
                    del body["modules"][0]["content_type"]
                if scenario == "wrong_artifact_config":
                    changed_config = copy.deepcopy(artifact_config)
                    changed_config["compatibility_date"] = "2020-01-01"
                    files["worker/wrangler.snapshot-qa.jsonc"] = json.dumps(
                        changed_config
                    ).encode()
                if scenario == "changed_traffic":
                    final["result"]["deployments"][0]["versions"][0]["percentage"] = 50
                replies = [
                    (first, {}),
                    ({"success": True, "result": body}, {}),
                    (namespaces, {}),
                    (final, {}),
                ]
                with (
                    patch(
                        "scripts.b_web_cloud_deploy.ACCOUNT_SHA",
                        hashlib.sha256(account.encode()).hexdigest(),
                    ),
                    patch(
                        "scripts.b_web_cloud_deploy.read_json", side_effect=replies
                    ) as read,
                    patch(
                        "scripts.b_web_cloud_deploy.fixture_artifact_files",
                        return_value=files,
                    ),
                    self.assertRaisesRegex(
                        ValueError,
                        {
                            "valid_but_unfenced": "fence and static asset",
                            "wrong_traffic": "not receiving all traffic",
                            "wrong_version": "version metadata unavailable",
                            "missing_version": "version metadata unavailable",
                            "missing_modules": "Version-bound modules missing",
                            "wrong_bytes": "modules differ",
                            "invalid_encoding": "Invalid version-bound module encoding",
                            "duplicate_modules": "Invalid version-bound module",
                            "artifact_collision": "Ambiguous or invalid artifact module",
                            "invalid_artifact_bytes": "Ambiguous or invalid artifact module",
                            "wrong_content_type": "content type mismatch",
                            "missing_content_type": "content type mismatch",
                            "wrong_artifact_config": "artifact configuration mismatch",
                            "changed_traffic": "deployment changed",
                        }[scenario],
                    ),
                ):
                    observe_fixture_cloud(
                        account,
                        {},
                        config,
                        "a" * 32,
                        "b" * 32,
                        b"artifact",
                        "a" * 40,
                        "b" * 64,
                        candidate,
                    )
                if scenario == "wrong_traffic":
                    self.assertEqual(read.call_count, 1)
                if scenario == "valid_but_unfenced":
                    self.assertEqual(read.call_count, 4)
                    self.assertIn(
                        "/versions/" + candidate + "?include=modules",
                        read.call_args_list[1].args[0],
                    )

    def test_idle_check_requires_complete_native_count(self):
        require_idle_jobs({"jobs": [], "active_job_count": 0})
        for count in (None, True, -1, 1, "0"):
            with self.subTest(count=count), self.assertRaises(ValueError):
                require_idle_jobs({"jobs": [], "active_job_count": count})

    def test_old_qa_runtime_without_count_fails_closed(self):
        # The installed QA runtime cannot yet report the exhaustive count.
        # Do not bootstrap by interpreting its filtered UI list as complete.
        with self.assertRaises(ValueError):
            require_idle_jobs({"revision": 5, "jobs": []})

    NODES = (
        "workersInvocationsAdaptive",
        "durableObjectsInvocationsAdaptiveGroups",
        "durableObjectsPeriodicGroups",
        "durableObjectsSqlStorageGroups",
    )

    def limits(self):
        return {
            n: {"enabled": True, "maxDuration": 32 * 86400, "notOlderThan": 90 * 86400}
            for n in self.NODES
        }

    def observed(self, node, rows):
        return {"data": {"viewer": {"accounts": [{node: rows}]}}}, {}

    def fixture_read(self, url, headers, data):
        if "settings{" in data["query"]:
            return self.observed("settings", self.limits())
        n = next(n for n in self.NODES if n + "(" in data["query"])
        rows = (
            [{"sum": {"requests": 7, "cpuTimeUs": 15000}}]
            if n == self.NODES[0]
            else [{"sum": {"requests": 8}}]
            if n == self.NODES[1]
            else [{"sum": {"duration": 2, "rowsRead": 3, "rowsWritten": 4}}]
            if n == self.NODES[2]
            else [
                {"dimensions": {"namespaceId": "A"}, "max": {"storedBytes": 99}},
                {"dimensions": {"namespaceId": "B"}, "max": {"storedBytes": 31}},
            ]
        )
        return self.observed(n, rows)

    def test_native_settings_full_sums_and_complete_bounded_storage_window(self):
        first = datetime.now(timezone.utc) - timedelta(days=31)
        last = first + timedelta(days=31)
        with (
            patch(
                "scripts.b_web_cloud_deploy.read_json", side_effect=self.fixture_read
            ) as read,
            patch("scripts.b_web_cloud_deploy.time.sleep") as sleep,
            patch("scripts.b_web_cloud_deploy.time.monotonic", return_value=0),
        ):
            result = read_included_usage(
                "fixture", {}, first.isoformat(), last.isoformat()
            )
        self.assertEqual(read.call_count, 5)
        self.assertEqual(sleep.call_count, 4)
        self.assertTrue(all(c.args == (2.0,) for c in sleep.call_args_list))
        self.assertEqual(read.call_args_list[0].args[2]["variables"], {"a": "fixture"})
        self.assertTrue(
            all(
                c.args[1]["X-Rate-Limit-Type"] == "account-based"
                for c in read.call_args_list
            )
        )
        seen = []
        for c in read.call_args_list[1:4]:
            data = c.args[2]
            self.assertEqual(
                data["variables"],
                {"a": "fixture", "s": first.isoformat(), "e": last.isoformat()},
            )
            seen.append(next(n for n in self.NODES if n + "(" in data["query"]))
        self.assertEqual(seen, list(self.NODES[:-1]))
        storage = read.call_args_list[4].args[2]
        self.assertIn(self.NODES[-1] + "(limit:10,", storage["query"])
        self.assertEqual(
            storage["variables"],
            {"a": "fixture", "s": first.isoformat(), "e": last.isoformat()},
        )
        self.assertEqual(
            result[self.NODES[0]][0]["sum"], {"requests": 7, "cpuTimeUs": 15000}
        )
        self.assertEqual(
            sum(r["max"]["storedBytes"] for r in result[self.NODES[-1]]), 130
        )

    def test_full_window_keeps_historical_and_current_namespace_peaks(self):
        storage = self.NODES[-1]
        rows = [
            {"dimensions": {"namespaceId": "historic"}, "max": {"storedBytes": 90}},
            {"dimensions": {"namespaceId": "current"}, "max": {"storedBytes": 20}},
            {"dimensions": {"namespaceId": "native-zero"}, "max": {"storedBytes": 0}},
        ]

        def read(url, headers, data):
            return (
                self.observed(storage, rows)
                if storage + "(" in data["query"]
                else self.fixture_read(url, headers, data)
            )

        with (
            patch("scripts.b_web_cloud_deploy.read_json", side_effect=read) as query,
            patch("scripts.b_web_cloud_deploy.time.sleep"),
        ):
            result = read_included_usage(
                "fixture", {}, "2026-09-22T01:15:00+00:00", "2026-10-23T01:15:00+00:00"
            )
        self.assertEqual(result[storage], rows)
        self.assertEqual(query.call_count, 5)

    def test_full_storage_window_empty_or_missing_never_authorizes_zero(self):
        storage = self.NODES[-1]
        for rows in ([], None):

            def read(url, headers, data):
                return (
                    self.observed(storage, rows)
                    if storage + "(" in data["query"]
                    else self.fixture_read(url, headers, data)
                )

            with (
                self.subTest(rows=rows),
                patch("scripts.b_web_cloud_deploy.read_json", side_effect=read),
                patch("scripts.b_web_cloud_deploy.time.sleep"),
            ):
                with self.assertRaisesRegex(ValueError, "absent or truncated"):
                    read_included_usage(
                        "fixture",
                        {},
                        "2026-09-22T01:15:00+00:00",
                        "2026-10-23T01:15:00+00:00",
                    )

    def test_storage_refusal_does_not_return_partial_or_retry(self):
        storage = self.NODES[-1]

        def read(url, headers, data):
            if storage + "(" in data["query"]:
                return {"errors": [{"extensions": {"code": "budget"}}]}, {}
            return self.fixture_read(url, headers, data)

        with (
            patch("scripts.b_web_cloud_deploy.read_json", side_effect=read) as query,
            patch("scripts.b_web_cloud_deploy.time.sleep"),
            patch("scripts.b_web_cloud_deploy.Path.write_text"),
        ):
            with self.assertRaisesRegex(ValueError, "Cannot verify included"):
                read_included_usage(
                    "fixture",
                    {},
                    "2026-09-22T01:15:00+00:00",
                    "2026-10-23T01:15:00+00:00",
                )
        self.assertEqual(query.call_count, 5)

    def test_account_policy_does_not_mutate_caller_rest_headers(self):
        headers = {"Authorization": "Bearer fixture-only"}
        with (
            patch(
                "scripts.b_web_cloud_deploy.read_json", side_effect=self.fixture_read
            ),
            patch("scripts.b_web_cloud_deploy.time.sleep"),
        ):
            read_included_usage(
                "fixture",
                headers,
                "2026-10-22T01:15:00+00:00",
                "2026-10-23T01:15:00+00:00",
            )
        self.assertEqual(headers, {"Authorization": "Bearer fixture-only"})

    def test_error_categories_keep_window_order_without_provider_private_text(self):
        for message, kind in (
            (
                "Rate limiter budget depleted: Bearer private-token account-private",
                "rate_limit",
            ),
            ("query consumed excessive resources: private-token", "query_resources"),
            (
                "in combination, too many nodes, zones and accounts: private-token",
                "scope_combination",
            ),
            ({"token": "private-token"}, "unknown"),
        ):
            with (
                self.subTest(kind=kind),
                patch(
                    "scripts.b_web_cloud_deploy.read_json",
                    return_value=(
                        {
                            "errors": [
                                {"message": message, "extensions": {"code": "budget"}}
                            ]
                        },
                        {},
                    ),
                ),
                patch("scripts.b_web_cloud_deploy.Path.write_text") as write,
            ):
                with self.assertRaises(ValueError):
                    read_included_usage(
                        "account-private",
                        {},
                        "2026-10-22T01:15:00+00:00",
                        "2026-10-23T01:15:00+00:00",
                    )
            packet = json.loads(write.call_args.args[0])
            self.assertEqual(packet["kinds"], [kind])
            self.assertEqual(packet["attempt"], 1)
            self.assertEqual(packet["window"], {})
            self.assertNotIn("private-token", write.call_args.args[0])
            self.assertNotIn("account-private", write.call_args.args[0])

    def test_dataset_disabled_missing_or_range_unavailable_stops_before_usage(self):
        first = datetime.now(timezone.utc) - timedelta(days=31)
        last = first + timedelta(days=31)
        for change in (
            {"enabled": False},
            {"maxDuration": 86400},
            {"notOlderThan": 86400},
            {"maxDuration": None},
            {"maxDuration": True},
        ):
            settings = self.limits()
            settings[self.NODES[0]].update(change)
            with (
                self.subTest(change=change),
                patch(
                    "scripts.b_web_cloud_deploy.read_json",
                    return_value=self.observed("settings", settings),
                ) as read,
            ):
                with self.assertRaisesRegex(ValueError, "window unavailable"):
                    read_included_usage(
                        "fixture", {}, first.isoformat(), last.isoformat()
                    )
                self.assertEqual(read.call_count, 1)
        with patch(
            "scripts.b_web_cloud_deploy.read_json",
            return_value=self.observed("settings", None),
        ):
            with self.assertRaisesRegex(ValueError, "settings missing"):
                read_included_usage("fixture", {}, first.isoformat(), last.isoformat())

    def test_missing_negative_nonfinite_or_empty_usage_never_becomes_zero(self):
        start, end = "2026-10-01T00:00:00+00:00", "2026-10-02T00:00:00+00:00"
        for rows in (
            None,
            [],
            [{"sum": {"requests": 1}}],
            [{"sum": {"requests": -1, "cpuTimeUs": 1}}],
            [{"sum": {"requests": 1, "cpuTimeUs": float("nan")}}],
        ):

            def read(url, headers, data):
                return (
                    self.observed("settings", self.limits())
                    if "settings{" in data["query"]
                    else self.observed(self.NODES[0], rows)
                )

            with (
                self.subTest(rows=rows),
                patch("scripts.b_web_cloud_deploy.read_json", side_effect=read),
                patch("scripts.b_web_cloud_deploy.time.sleep"),
            ):
                with self.assertRaises(ValueError):
                    read_included_usage("fixture", {}, start, end)

    def test_deadline_or_invalid_accounting_window_never_starts_query(self):
        with (
            patch("scripts.b_web_cloud_deploy.read_json") as read,
            patch("scripts.b_web_cloud_deploy.time.monotonic", side_effect=[0, 121]),
        ):
            with self.assertRaisesRegex(ValueError, "deadline"):
                read_included_usage(
                    "fixture",
                    {},
                    "2026-10-01T00:00:00+00:00",
                    "2026-10-02T00:00:00+00:00",
                )
            read.assert_not_called()
        with patch("scripts.b_web_cloud_deploy.read_json") as read:
            with self.assertRaisesRegex(ValueError, "accounting window"):
                read_included_usage(
                    "fixture",
                    {},
                    "2026-08-01T00:00:00+00:00",
                    "2026-10-02T00:00:00+00:00",
                )
            read.assert_not_called()

    def test_usage_error_artifact_never_copies_arbitrary_provider_codes(self):
        for code in ("private-account-identifier", {"token": "private"}, 12345):
            with (
                self.subTest(code=code),
                patch(
                    "scripts.b_web_cloud_deploy.read_json",
                    return_value=({"errors": [{"extensions": {"code": code}}]}, {}),
                ),
                patch("scripts.b_web_cloud_deploy.Path.write_text") as write,
            ):
                with self.assertRaises(ValueError):
                    read_included_usage(
                        "fixture",
                        {},
                        "2026-10-01T00:00:00+00:00",
                        "2026-10-02T00:00:00+00:00",
                    )
                self.assertEqual(
                    json.loads(write.call_args.args[0])["codes"], ["unknown"]
                )

    def test_native_rate_refusal_stops_without_retry_or_sleep_for_reset(self):
        with (
            patch(
                "scripts.b_web_cloud_deploy.read_json",
                return_value=(
                    {
                        "errors": [
                            {
                                "message": "Rate limiter budget depleted",
                                "extensions": {"code": "budget"},
                            }
                        ]
                    },
                    {},
                ),
            ) as read,
            patch("scripts.b_web_cloud_deploy.Path.write_text") as write,
            patch("scripts.b_web_cloud_deploy.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(ValueError, "Cannot verify included"):
                read_included_usage(
                    "fixture",
                    {},
                    "2026-10-01T00:00:00+00:00",
                    "2026-10-02T00:00:00+00:00",
                )
        self.assertEqual(read.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(
            json.loads(write.call_args.args[0]),
            {
                "node": "settings",
                "codes": ["budget"],
                "kinds": ["rate_limit"],
                "attempt": 1,
                "mode": "account-based",
                "window": {},
                "model_calls": 0,
            },
        )

    def test_duplicate_or_truncated_storage_is_closed(self):
        for rows in (
            [{"dimensions": {"namespaceId": "A"}, "max": {"storedBytes": 1}}] * 2,
            [{"max": {"storedBytes": 1}}] * 10,
        ):

            def read(url, headers, data):
                if self.NODES[-1] + "(" in data["query"]:
                    return self.observed(self.NODES[-1], rows)
                return self.fixture_read(url, headers, data)

            with (
                self.subTest(size=len(rows)),
                patch(
                    "scripts.b_web_cloud_deploy.read_json", side_effect=read
                ) as query,
                patch("scripts.b_web_cloud_deploy.time.sleep"),
            ):
                with self.assertRaises(ValueError):
                    read_included_usage(
                        "fixture",
                        {},
                        "2026-10-01T00:00:00+00:00",
                        "2026-10-02T00:00:00+00:00",
                    )
                self.assertEqual(query.call_count, 5)

    def test_existing_isolated_operator_is_preserved_without_adding_a_binding(self):
        empty = {"resources": {"bindings": []}}
        operator = {
            "resources": {"bindings": [{"name": "OPERATOR_KEY", "type": "secret_text"}]}
        }
        self.assertNotIn("OPERATOR_KEY", expected_binding_types("qa19", empty))
        self.assertEqual(
            expected_binding_types("qa19", operator)["OPERATOR_KEY"], "secret_text"
        )
        self.assertEqual(
            expected_binding_types("production", empty)["OPERATOR_KEY"], "secret_text"
        )
        for bindings in (
            [{"name": "OPERATOR_KEY", "type": "plain_text"}],
            operator["resources"]["bindings"] * 2,
        ):
            with self.assertRaisesRegex(ValueError, "Unexpected existing QA"):
                expected_binding_types("qa19", {"resources": {"bindings": bindings}})

    def test_browser_binding_is_exactly_declared_not_inferred_from_old_version(self):
        empty = {"resources": {"bindings": []}}
        self.assertNotIn("BROWSER", expected_binding_types("qa19", empty, {}))
        self.assertEqual(
            expected_binding_types("qa19", empty, {"browser": {"binding": "BROWSER"}})[
                "BROWSER"
            ],
            "browser",
        )
        for value in ({"binding": "OTHER"}, {"binding": "BROWSER", "remote": True}):
            with self.assertRaisesRegex(ValueError, "Unexpected browser"):
                expected_binding_types("qa19", empty, {"browser": value})

    def test_existing_version_verification_reads_trusted_old_main_without_activation(
        self,
    ):
        import scripts.b_web_cloud_deploy as deploy

        commit, version = "a" * 40, "11111111-1111-1111-1111-111111111111"
        account = "fixture-account"
        config = {"compatibility_date": "2026-10-13", "compatibility_flags": []}
        files = {
            "worker/src/entry.py": b"entry",
            "worker/src/creator_ai.mjs": b"engine",
            "worker/wrangler.jsonc": json.dumps(config).encode(),
            "worker/public/_headers": b"/*\n  Cache-Control: no-store\n",
            "worker/public/index.html": b"page",
        }
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as z:
            for name, body in files.items():
                z.writestr(name, body)
            z.writestr(
                "runtime.json",
                json.dumps(
                    {
                        "format": "B_WEB_CI_RUNTIME_V1",
                        "commit": commit,
                        "files": {n: deploy.sha(b) for n, b in files.items()},
                    }
                ),
            )
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, "w") as z:
            z.writestr("b-web-runtime.zip", bundle.getvalue())
        archive = outer.getvalue()
        meta = {
            "head_sha": commit,
            "head_branch": "main",
            "event": "push",
            "workflow_id": 7,
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": REPO},
        }

        def gh(args, **kwargs):
            path = args[-1]
            if path.endswith("/zip"):
                return archive
            if path.endswith("/artifacts?per_page=100"):
                data = {
                    "total_count": 1,
                    "artifacts": [
                        {
                            "id": 9,
                            "name": "b-web-runtime",
                            "expired": False,
                            "digest": "sha256:" + deploy.sha(archive),
                        }
                    ],
                }
            elif path.endswith("/jobs?per_page=100"):
                data = {
                    "total_count": 3,
                    "jobs": [{"name": n, "conclusion": "success"} for n in CHECKS],
                }
            elif path.endswith("/ci.yml"):
                data = {"id": 7}
            elif path.endswith("/runs/8"):
                data = meta
            else:
                self.fail("Unexpected GitHub operation: " + path)
            return json.dumps(data).encode()

        bindings = [
            {"name": n, "type": t}
            for n, t in {
                "AI": "ai",
                "ASSETS": "assets",
                "CREATOR_MODEL": "plain_text",
                "CREATOR_VERSION": "version_metadata",
                "DATABASE": "durable_object_namespace",
                "LOGIN_PASSWORD_HASH": "secret_text",
            }.items()
        ]
        next(b for b in bindings if b["name"] == "DATABASE").update(
            namespace_id="fixture", class_name="Database"
        )
        next(b for b in bindings if b["name"] == "CREATOR_MODEL")["text"] = MODEL

        def read(url, headers, data=None):
            # Only session creation and read-only analytics may use POST.
            if data is not None:
                self.assertTrue(url.endswith(("/api/login", "/graphql")), url)
            if url.endswith("/api/login"):
                return {}, {"Set-Cookie": "__Host-creator=fixture; Secure"}
            if url.endswith("/api/state"):
                return {"revision": 5, "jobs": [], "active_job_count": 0}, {}
            if url.endswith("/api/runtime"):
                return {"deployment": version, "model": MODEL}, {}
            if url.endswith("/deployments"):
                result = {
                    "deployments": [
                        {"versions": [{"version_id": version, "percentage": 100}]}
                    ]
                }
            elif "/versions/" in url:
                result = {"resources": {"bindings": bindings}}
            elif url.endswith("/account-settings"):
                result = {"default_usage_model": "standard"}
            elif url.endswith("/settings"):
                result = {**config, "bindings": bindings}
            elif url.endswith("/graphql"):
                if "settings{" in data["query"]:
                    return self.observed("settings", self.limits())
                return {
                    "data": {
                        "viewer": {
                            "accounts": [
                                {
                                    "workersInvocationsAdaptive": [
                                        {"sum": {"requests": 1, "cpuTimeUs": 1}}
                                    ],
                                    "durableObjectsPeriodicGroups": [
                                        {
                                            "sum": {
                                                "duration": 1,
                                                "rowsRead": 1,
                                                "rowsWritten": 1,
                                            }
                                        }
                                    ],
                                    "durableObjectsInvocationsAdaptiveGroups": [
                                        {"sum": {"requests": 1}}
                                    ],
                                    "durableObjectsSqlStorageGroups": [
                                        {
                                            "dimensions": {"namespaceId": "fixture"},
                                            "max": {"storedBytes": 1},
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                }, {}
            else:
                self.fail("Unexpected HTTPS operation: " + url)
            return {"success": True, "result": result}, {}

        env = {
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_SHA": "b" * 40,
            "GITHUB_RUN_ID": "10",
            "B_OPERATION": "verify",
            "B_VERIFY_SOURCE_COMMIT": commit,
            "B_CI_RUN": "8",
            "B_TARGET": "qa19",
            "B_EXPECTED_DEPLOYMENT": version,
            "CLOUDFLARE_ACCOUNT_ID": account,
            "CLOUDFLARE_API_TOKEN": "fixture",
            "B_WEB_QA19_PASSWORD": "fixture",
        }
        with (
            patch.dict(os.environ, env),
            patch.object(deploy, "require_legacy_qa_fence"),
            patch.object(deploy, "ACCOUNT_SHA", deploy.sha(account.encode())),
            patch.object(deploy.subprocess, "check_output", side_effect=gh),
            patch.object(deploy.subprocess, "run") as upload,
            patch.object(deploy, "activate_once") as activate,
            patch.object(deploy, "read_json", side_effect=read) as read_mock,
            patch.object(deploy.time, "sleep"),
            patch.object(
                deploy,
                "read_bytes",
                side_effect=[
                    (b"multipart", {"Content-Type": "fixture"}),
                    (b"page", {"cache-control": "no-store"}),
                ],
            ),
            patch.object(
                deploy,
                "verify_remote_modules",
                return_value={"entry.py": deploy.sha(b"entry")},
            ),
            patch.object(deploy.Path, "write_text") as write,
        ):
            main()
        upload.assert_not_called()
        activate.assert_not_called()
        urls = [call.args[0] for call in read_mock.call_args_list]
        self.assertLess(
            next(i for i, url in enumerate(urls) if url.endswith("/graphql")),
            next(i for i, url in enumerate(urls) if url.endswith("/api/login")),
        )
        receipt = json.loads(write.call_args.args[0])
        self.assertFalse(receipt["activation_attempted"])
        self.assertTrue(receipt["live_runtime_confirmed"])
        self.assertEqual(receipt["source_commit"], commit)
        self.assertEqual(receipt["workflow_commit"], "b" * 40)

    def test_propagation_reads_are_bounded_and_never_mask_changed_deployment(self):
        app = MagicMock(
            side_effect=[
                {"deployment": "old", "model": MODEL},
                {"deployment": "candidate", "model": MODEL},
            ]
        )
        sleeper = MagicMock()
        observed = observe_activated_runtime(
            app, lambda: "candidate", "candidate", sleep=sleeper
        )
        self.assertEqual(observed["deployment"], "candidate")
        sleeper.assert_called_once_with(8)
        app = MagicMock(return_value={"deployment": "old", "model": MODEL})
        sleeper.reset_mock()
        with self.assertRaisesRegex(ValueError, "not confirmed"):
            observe_activated_runtime(
                app, lambda: "candidate", "candidate", sleep=sleeper
            )
        self.assertEqual(app.call_count, 8)
        self.assertEqual(sleeper.call_count, 7)
        app.reset_mock()
        with self.assertRaisesRegex(ValueError, "Deployment changed"):
            observe_activated_runtime(app, lambda: "other", "candidate", sleep=sleeper)
        app.assert_not_called()
        for failure in (RuntimeError("HTTP403"),):
            app = MagicMock(side_effect=failure)
            with self.assertRaisesRegex(RuntimeError, "HTTP403"):
                observe_activated_runtime(
                    app, lambda: "candidate", "candidate", sleep=sleeper
                )
            self.assertEqual(app.call_count, 1)
        with self.assertRaisesRegex(ValueError, "model differs"):
            observe_activated_runtime(
                lambda _: {"deployment": "candidate", "model": "changed"},
                lambda: "candidate",
                "candidate",
                sleep=sleeper,
            )

    def test_https_client_identifies_application_and_preserves_same_origin_json(self):
        origin = "https://qa.invalid"
        response = MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.headers = {"Content-Type": "application/json"}
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch(
            "scripts.b_web_cloud_deploy.urllib.request.build_opener",
            return_value=opener,
        ):
            result, _ = read_json(
                origin + "/api/login",
                {"Origin": origin, "Content-Type": "application/json"},
                {"password": "fixture"},
            )
        self.assertTrue(result["ok"])
        request = opener.open.call_args.args[0]
        self.assertEqual(
            request.get_header("User-agent"), "CreatorLoop-B-Web-CloudDeploy/1"
        )
        self.assertEqual(request.get_header("Origin"), origin)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {"password": "fixture"})

    def test_activation_rechecks_after_staging_and_never_retries_ambiguity(self):
        events = []

        def check():
            events.append("fresh-check")
            return {"revision": 5}

        def create(data):
            events.append(data)

        def record(version):
            events.append(("attempt", version))

        self.assertEqual(
            activate_once(check, create, lambda: "candidate", "candidate", record),
            {"revision": 5},
        )
        self.assertEqual(events[0], "fresh-check")
        self.assertEqual(events[1], ("attempt", "candidate"))
        self.assertEqual(
            events[2]["versions"], [{"version_id": "candidate", "percentage": 100}]
        )
        events.clear()

        def changed():
            raise ValueError("new main/deployment/active job")

        with self.assertRaises(ValueError):
            activate_once(changed, create, lambda: "candidate", "candidate", record)
        self.assertEqual(events, [])
        with self.assertRaises(ValueError):
            activate_once(check, create, lambda: "other", "candidate", record)
        self.assertEqual(sum(isinstance(e, dict) for e in events), 1)

    def test_rollback_checks_complete_python_tree_and_migration_configuration(self):
        files = {
            "worker/src/entry.py": b"entry",
            "worker/python_modules/domain.py": b"domain",
            "worker/src/migrations/001.sql": b"CREATE TABLE fixture(id)",
            "worker/wrangler.jsonc": json.dumps(
                {"migrations": [{"tag": "v1"}], "compatibility_date": "2026-10-13"}
            ).encode(),
        }
        storage_compatible(files, files)
        for name, value in (
            ("worker/python_modules/domain.py", b"changed"),
            ("worker/python_modules/new.py", b"new"),
            ("worker/src/migrations/001.sql", b"changed migration"),
            ("worker/wrangler.jsonc", b'{"migrations":[{"tag":"v2"}]}'),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                storage_compatible(files, {**files, name: value})

    def test_assets_control_headers_are_verified_as_response_policy(self):
        files = {
            "worker/public/_headers": b"/*\n  Cache-Control: no-store\n  X-Frame-Options: DENY\n"
        }
        self.assertEqual(
            static_header_policy(files),
            {"cache-control": "no-store", "x-frame-options": "DENY"},
        )
        for raw in (
            b"/other\n  Cache-Control: no-store",
            b"/*\nredirect",
            b"/*\n  A: b\n  a: c",
            b"/*",
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                static_header_policy({"worker/public/_headers": raw})

    def test_remote_module_check_rejects_missing_added_changed_and_duplicate_parts(
        self,
    ):
        files = {
            "worker/src/entry.py": b"entry",
            "worker/python_modules/domain.py": b"domain",
        }

        def payload(parts):
            return (
                b"".join(
                    b'--boundary\r\nContent-Disposition: form-data; name="'
                    + n.encode()
                    + b'"; filename="'
                    + n.encode()
                    + b'"\r\nContent-Type: application/octet-stream\r\n\r\n'
                    + b
                    + b"\r\n"
                    for n, b in parts
                )
                + b"--boundary--\r\n"
            )

        parts = [("entry.py", b"entry"), ("python_modules/domain.py", b"domain")]
        self.assertEqual(
            len(
                verify_remote_modules(
                    payload(parts), "multipart/form-data; boundary=boundary", files
                )
            ),
            2,
        )
        for changed in (
            parts[:1],
            parts + [("extra.py", b"x")],
            [("entry.py", b"changed"), parts[1]],
            parts + [parts[0]],
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                verify_remote_modules(
                    payload(changed), "multipart/form-data; boundary=boundary", files
                )

    def test_main_ci_identity_and_every_required_job_are_mandatory(self):
        commit = "a" * 40
        run = {
            "head_sha": commit,
            "head_branch": "main",
            "event": "push",
            "workflow_id": 7,
            "status": "completed",
            "conclusion": "success",
            "repository": {"full_name": REPO},
        }
        jobs = {
            "total_count": len(CHECKS),
            "jobs": [{"name": name, "conclusion": "success"} for name in CHECKS],
        }
        verify_ci(run, jobs, {"id": 7}, commit)
        for changed in (
            {"head_sha": "b" * 40},
            {"head_branch": "untrusted"},
            {"event": "pull_request"},
            {"workflow_id": 8},
            {"conclusion": "failure"},
            {"repository": {"full_name": "other/repo"}},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                verify_ci({**run, **changed}, jobs, {"id": 7}, commit)
        for name in CHECKS:
            altered = copy.deepcopy(jobs)
            next(j for j in altered["jobs"] if j["name"] == name)["conclusion"] = (
                "skipped"
            )
            with self.subTest(name=name), self.assertRaises(ValueError):
                verify_ci(run, altered, {"id": 7}, commit)

    def archive(self, mutate=None):
        files = {
            "worker/src/entry.py": b"fixture",
            "worker/src/creator_ai.mjs": b"fixture",
            "worker/wrangler.jsonc": b"{}",
        }
        manifest = {
            "format": "B_WEB_CI_RUNTIME_V1",
            "commit": "a" * 40,
            "files": {
                name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()
            },
        }
        if mutate:
            mutate(files, manifest)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, raw in files.items():
                archive.writestr(name, raw)
            archive.writestr("runtime.json", json.dumps(manifest))
        return output.getvalue()

    def test_archive_refuses_changed_bytes_traversal_and_wrong_commit(self):
        self.assertIn("worker/src/entry.py", runtime_files(self.archive(), "a" * 40))
        for mutate in (
            lambda files, manifest: files.update({"worker/src/entry.py": b"tampered"}),
            lambda files, manifest: files.update({"worker/../../escape": b"bad"}),
            lambda files, manifest: manifest.update(commit="b" * 40),
        ):
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                runtime_files(self.archive(mutate), "a" * 40)

    def test_fixture_artifact_requires_commit_digest_and_bundle_closure(self):
        from scripts.b_web_cloud_deploy import fixture_artifact_files

        def prepare(files, manifest):
            files.update(
                {
                    "worker/wrangler.snapshot-qa.jsonc": b"{}",
                    "worker/python_modules/workers/__init__.py": b"sdk",
                    "worker/public/login.html": b"login",
                    "worker/public/login.js": b"js",
                    "worker/public/style.css": b"css",
                    "worker/public/_headers": b"headers",
                    "worker/public/index.html": b"index",
                    "worker/public/app.js": b"app",
                    "worker/public/pending.mjs": b"pending",
                    "worker/public/operator-grant.mjs": b"grant",
                    "worker/public/intake.mjs": b"intake",
                    "worker/public/source-review.mjs": b"review",
                    "worker/src/migrations/0001_one.sql": b"sql",
                    "worker/src/migrations/0002_two.sql": b"sql2",
                    "worker/python_modules/creator_loop/backend/runtime/database.py": b'MIGRATIONS = ("0001_one", "0002_two")',
                }
            )
            files["worker/bundle-manifest.json"] = json.dumps(
                {
                    "files": [
                        {
                            "path": "workers/__init__.py",
                            "sha256": hashlib.sha256(b"sdk").hexdigest(),
                        },
                        {
                            "path": "creator_ai.mjs",
                            "sha256": hashlib.sha256(b"fixture").hexdigest(),
                        },
                        {
                            "path": "creator_loop/backend/runtime/database.py",
                            "sha256": hashlib.sha256(
                                b'MIGRATIONS = ("0001_one", "0002_two")'
                            ).hexdigest(),
                        },
                    ]
                }
            ).encode()
            manifest["files"] = {
                name: hashlib.sha256(data).hexdigest() for name, data in files.items()
            }

        raw = self.archive(prepare)
        self.assertIn(
            "worker/public/login.html",
            fixture_artifact_files(raw, "a" * 40, hashlib.sha256(raw).hexdigest()),
        )
        for commit, digest in (
            ("b" * 40, hashlib.sha256(raw).hexdigest()),
            ("a" * 40, "0" * 64),
        ):
            with (
                self.subTest(commit=commit, digest=digest),
                self.assertRaises(ValueError),
            ):
                fixture_artifact_files(raw, commit, digest)

        def missing(files, manifest):
            prepare(files, manifest)
            del files["worker/public/login.js"]
            del manifest["files"]["worker/public/login.js"]

        incomplete = self.archive(missing)
        with self.assertRaisesRegex(ValueError, "missing required files"):
            fixture_artifact_files(
                incomplete, "a" * 40, hashlib.sha256(incomplete).hexdigest()
            )

        def unlisted(files, manifest):
            prepare(files, manifest)
            files["worker/python_modules/unlisted.py"] = b"extra"
            manifest["files"]["worker/python_modules/unlisted.py"] = hashlib.sha256(
                b"extra"
            ).hexdigest()

        extra = self.archive(unlisted)
        with self.assertRaisesRegex(ValueError, "bundle closure"):
            fixture_artifact_files(extra, "a" * 40, hashlib.sha256(extra).hexdigest())

        def missing_migration(files, manifest):
            prepare(files, manifest)
            del files["worker/src/migrations/0002_two.sql"]
            del manifest["files"]["worker/src/migrations/0002_two.sql"]

        incomplete = self.archive(missing_migration)
        with self.assertRaisesRegex(ValueError, "bundle closure"):
            fixture_artifact_files(
                incomplete, "a" * 40, hashlib.sha256(incomplete).hexdigest()
            )

    def test_rollback_refuses_different_namespace_class_or_model(self):
        current = {
            "resources": {
                "bindings": [
                    {
                        "name": "DATABASE",
                        "namespace_id": "fixture-namespace",
                        "class_name": "Database",
                    },
                    {"name": "CREATOR_MODEL", "text": MODEL},
                ]
            }
        }
        compatible_bindings(current, current)
        for name, field, value in (
            ("DATABASE", "namespace_id", "different"),
            ("DATABASE", "class_name", "Other"),
            ("CREATOR_MODEL", "text", "other-model"),
        ):
            changed = copy.deepcopy(current)
            next(b for b in changed["resources"]["bindings"] if b["name"] == name)[
                field
            ] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                compatible_bindings(current, changed)


if __name__ == "__main__":
    unittest.main()
