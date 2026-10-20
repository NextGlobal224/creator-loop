"""Fail-closed cloud deployment artifact and rollback boundaries, no cloud calls."""

import copy
import hashlib
import io
import json
import unittest
import zipfile

from scripts.b_web_cloud_deploy import (
    CHECKS,
    MODEL,
    REPO,
    activate_once,
    compatible_bindings,
    runtime_files,
    static_header_policy,
    storage_compatible,
    verify_ci,
    verify_remote_modules,
)


class CloudDeploymentTests(unittest.TestCase):
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
