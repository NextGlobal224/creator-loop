"""Fail-closed cloud deployment artifact and rollback boundaries, no cloud calls."""

import copy
import hashlib
import io
import json
import os
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
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
    runtime_files,
    static_header_policy,
    storage_compatible,
    verify_ci,
    verify_remote_modules,
)


class CloudDeploymentTests(unittest.TestCase):
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
        self.assertEqual(read.call_count, 9)
        self.assertEqual(sleep.call_count, 8)
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
        stop = last
        for c in read.call_args_list[4:]:
            data = c.args[2]
            self.assertIn(self.NODES[-1] + "(", data["query"])
            self.assertIn("limit:10,", data["query"])
            variables = data["variables"]
            self.assertEqual(variables["a"], "fixture")
            begin = datetime.fromisoformat(variables["s"])
            self.assertEqual(datetime.fromisoformat(variables["e"]), stop)
            self.assertLessEqual(stop - begin, timedelta(days=7))
            self.assertGreater(stop, begin)
            stop = begin
        self.assertEqual(stop, first)
        self.assertEqual(
            result[self.NODES[0]][0]["sum"], {"requests": 7, "cpuTimeUs": 15000}
        )
        self.assertEqual(
            sum(r["max"]["storedBytes"] for r in result[self.NODES[-1]]), 130
        )

    def test_partition_maxima_preserve_historical_namespace_and_empty_samples(self):
        storage = self.NODES[-1]
        partitions = iter(
            [
                [{"dimensions": {"namespaceId": "A"}, "max": {"storedBytes": 10}}],
                [],
                [
                    {"dimensions": {"namespaceId": "A"}, "max": {"storedBytes": 90}},
                    {"dimensions": {"namespaceId": "B"}, "max": {"storedBytes": 20}},
                ],
                [],
                [{"dimensions": {"namespaceId": "C"}, "max": {"storedBytes": 0}}],
            ]
        )

        def read(url, headers, data):
            return (
                self.observed(storage, next(partitions))
                if storage + "(" in data["query"]
                else self.fixture_read(url, headers, data)
            )

        with (
            patch("scripts.b_web_cloud_deploy.read_json", side_effect=read),
            patch("scripts.b_web_cloud_deploy.time.sleep"),
        ):
            result = read_included_usage(
                "fixture", {}, "2026-09-22T01:15:00+00:00", "2026-10-23T01:15:00+00:00"
            )
        self.assertEqual(
            {
                r["dimensions"]["namespaceId"]: r["max"]["storedBytes"]
                for r in result[storage]
            },
            {"A": 90, "B": 20, "C": 0},
        )

    def test_all_storage_partitions_empty_or_missing_never_authorizes_zero(self):
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

    def test_middle_storage_partition_refusal_does_not_return_partial_or_retry(self):
        storage = self.NODES[-1]
        parts = 0

        def read(url, headers, data):
            nonlocal parts
            if storage + "(" in data["query"]:
                parts += 1
                if parts == 2:
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
        self.assertEqual(parts, 2)
        self.assertEqual(query.call_count, 6)

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
                return {"revision": 5, "jobs": []}, {}
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
            patch.object(deploy, "ACCOUNT_SHA", deploy.sha(account.encode())),
            patch.object(deploy.subprocess, "check_output", side_effect=gh),
            patch.object(deploy.subprocess, "run") as upload,
            patch.object(deploy, "activate_once") as activate,
            patch.object(deploy, "read_json", side_effect=read),
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
