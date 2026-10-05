"""Fake component artifacts only; no real engine/model acceptance claims."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from creator_loop.local_components import (
    DEFAULT_WORKER_MEMORY,
    HASH_CHUNK,
    MANIFEST_LIMIT,
    ComponentPreflightError,
    hold_verified_components,
    load_component_manifest,
    parse_component_manifest,
)
from creator_loop.publication_media import _open_read_lock


class LocalComponentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True)
        self.engine = self.root / "fake engine 非実行.fixture"
        self.model = self.root / "fake model.fixture"
        self.engine.write_bytes(b"FAKE ENGINE: never execute or distribute\x00\xff")
        self.model.write_bytes(bytes(range(256)) * 8193)
        self.entries = [
            self.entry(self.engine, "fake-engine", "ENGINE"),
            self.entry(self.model, "fake-model", "MODEL"),
        ]
        self.opened = []

    def entry(self, path, component_id, kind):
        return {
            "component_id": component_id,
            "kind": kind,
            "path": str(path),
            "version": "fake-test-v1",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "byte_size": path.stat().st_size,
            "source_url": "https://example.invalid/fixtures",
            "license": "FAKE TEST DECLARATION; no real component permission",
            "license_url": "https://example.invalid/fixture-license",
            "local_use_allowed": True,
            "owned": False,
            "worker_memory_bytes": DEFAULT_WORKER_MEMORY,
        }

    def raw(self, entries=None):
        return json.dumps(
            {
                "manifest_version": 1,
                "components": self.entries if entries is None else entries,
            }
        ).encode("utf-8")

    def specs(self):
        return parse_component_manifest(self.raw())

    def tracked_open(self, path):
        stream = _open_read_lock(path)
        self.opened.append(stream)
        return stream

    def test_verifies_exact_bytes_retains_readonly_handles_and_closes_only_own(self):
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with self.engine.open("rb") as foreign:
            with hold_verified_components(self.specs()) as verified:
                self.assertEqual(len(verified), 2)
                for item in verified:
                    self.assertFalse(item.stream.writable())
                    self.assertFalse(os.get_inheritable(item.stream.fileno()))
                    self.assertEqual(item.stream.read(), item.spec.path.read_bytes())
                    self.assertFalse(item.spec.owned)
                self.assertFalse(foreign.closed)
            self.assertTrue(all(item.stream.closed for item in verified))
            self.assertFalse(foreign.closed)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})

    def test_invalid_metadata_and_ownership_permission_fail_closed(self):
        changes = [
            {"version": ""},
            {"version": "fake\nsecret"},
            {"owned": True},
            {"local_use_allowed": False},
            {"local_use_allowed": 1},
            {"byte_size": True},
            {"byte_size": 0},
            {"sha256": "a" * 63},
            {"path": "relative.fixture"},
            {"kind": "AUTO"},
            {"worker_memory_bytes": True},
            {"worker_memory_bytes": 4 * 1024**3 + 1},
            {"source_url": "http://example.invalid/component"},
            {"source_url": "https://secret:token@example.invalid/component"},
            {"source_url": "https://example.invalid/component?token=secret"},
            {"license_url": "https://example.invalid/#license"},
            {"license": ""},
            {"version": "\ud800"},
            {"unknown_field": "ignored?"},
        ]
        for change in changes:
            with (
                self.subTest(change=change),
                self.assertRaises(ComponentPreflightError),
            ):
                parse_component_manifest(self.raw([{**self.entries[0], **change}]))
        incomplete = dict(self.entries[0])
        del incomplete["license"]
        with self.assertRaises(ComponentPreflightError):
            parse_component_manifest(self.raw([incomplete]))

    def test_manifest_bounds_duplicate_keys_ids_paths_and_version(self):
        malformed = [
            b"\xff",
            b"{",
            b"[]",
            b"[" * 2000,
            b" " * (MANIFEST_LIMIT + 1),
            b'{"manifest_version":1,"manifest_version":1,"components":[]}',
            b'{"manifest_version":' + b"1" * 5000 + b',"components":[]}',
        ]
        for raw in malformed:
            with (
                self.subTest(raw_length=len(raw)),
                self.assertRaises(ComponentPreflightError),
            ):
                parse_component_manifest(raw)
        for entries in (
            [],
            self.entries * 9,
            [self.entries[0]] * 2,
            [self.entries[0], {**self.entries[1], "path": str(self.engine)}],
        ):
            with self.assertRaises(ComponentPreflightError):
                parse_component_manifest(self.raw(entries))
        raw = json.loads(self.raw())
        raw["manifest_version"] = True
        with self.assertRaises(ComponentPreflightError):
            parse_component_manifest(json.dumps(raw).encode())

    def test_direct_caller_cannot_bypass_permission_or_ownership_checks(self):
        for changes in ({"owned": True}, {"local_use_allowed": False}, {"version": ""}):
            with (
                self.subTest(changes=changes),
                self.assertRaises(ComponentPreflightError),
            ):
                with hold_verified_components((replace(self.specs()[0], **changes),)):
                    self.fail("Invalid declaration accepted")

    def test_mismatch_and_missing_second_artifact_release_first_handle(self):
        for change in (
            {"sha256": "0" * 64},
            {"byte_size": self.model.stat().st_size + 1},
            {"path": str(self.root / "missing.fixture")},
        ):
            specs = parse_component_manifest(
                self.raw([self.entries[0], {**self.entries[1], **change}])
            )
            self.opened = []
            with patch(
                "creator_loop.local_components._open_read_lock",
                side_effect=self.tracked_open,
            ):
                with self.assertRaises(ComponentPreflightError):
                    with hold_verified_components(specs):
                        self.fail("Invalid physical artifact accepted")
            self.assertTrue(self.opened)
            self.assertTrue(all(stream.closed for stream in self.opened))
            with self.engine.open("r+b") as stream:
                self.assertEqual(stream.read(), self.engine.read_bytes())

    def test_alias_hardlink_rejected_and_both_external_names_preserved(self):
        alias = self.root / "hardlink.fixture"
        os.link(self.engine, alias)
        before = self.engine.read_bytes()
        with self.assertRaises(ComponentPreflightError):
            with hold_verified_components((self.specs()[0],)):
                self.fail("Hardlinked artifact accepted")
        self.assertEqual(self.engine.read_bytes(), before)
        self.assertEqual(alias.read_bytes(), before)

    def test_symlink_ancestor_or_file_never_grants_external_ownership(self):
        link = self.root / "linked-fixtures"
        try:
            link.symlink_to(self.root, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlink creation unavailable: {exc}")
        spec = replace(self.specs()[0], path=link / self.engine.name)
        with self.assertRaises(ComponentPreflightError):
            with hold_verified_components((spec,)):
                self.fail("Linked artifact accepted")
        self.assertTrue(link.is_symlink())
        self.assertTrue(self.engine.is_file())

    def test_open_race_to_different_native_file_identity_refused_even_identical_bytes(
        self,
    ):
        replacement = self.root / "replacement.fixture"
        replacement.write_bytes(self.engine.read_bytes())
        with patch(
            "creator_loop.local_components._open_read_lock",
            side_effect=lambda _p: self.tracked_open(replacement),
        ):
            with self.assertRaises(ComponentPreflightError):
                with hold_verified_components((self.specs()[0],)):
                    self.fail("Different held native file accepted")
        self.assertTrue(all(stream.closed for stream in self.opened))
        self.assertTrue(self.engine.is_file())
        self.assertTrue(replacement.is_file())

    def test_hash_reads_are_bounded_and_handle_position_returns_to_zero(self):
        sizes = []

        def track(path):
            stream = self.tracked_open(path)
            real_read = stream.read

            def read(size):
                sizes.append(size)
                return real_read(size)

            stream.read = read
            return stream

        with patch("creator_loop.local_components._open_read_lock", side_effect=track):
            with hold_verified_components((self.specs()[1],)) as verified:
                self.assertEqual(verified[0].stream.tell(), 0)
        self.assertGreaterEqual(len(sizes), 3)
        self.assertTrue(all(0 < size <= HASH_CHUNK for size in sizes))

    def test_cancel_and_deadline_release_native_handles_without_deleting_files(self):
        specs = self.specs()
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 3

        with patch(
            "creator_loop.local_components._open_read_lock",
            side_effect=self.tracked_open,
        ):
            with self.assertRaises(InterruptedError):
                with hold_verified_components(specs, cancelled=cancelled):
                    self.fail("Cancellation ignored")
        self.assertTrue(self.opened)
        self.assertTrue(all(stream.closed for stream in self.opened))
        self.opened = []
        with patch(
            "creator_loop.local_components._open_read_lock",
            side_effect=self.tracked_open,
        ):
            with patch(
                "creator_loop.local_components.time.monotonic",
                side_effect=[0, 0.001, 0.02],
            ):
                with self.assertRaises(TimeoutError):
                    with hold_verified_components((specs[0],), timeout_seconds=0.01):
                        self.fail("Deadline ignored")
        self.assertTrue(self.opened)
        self.assertTrue(all(stream.closed for stream in self.opened))
        self.assertTrue(self.engine.is_file())
        self.assertTrue(self.model.is_file())

    def test_memory_is_declared_sequential_budget_not_file_size_or_hardware_measurement(
        self,
    ):
        with hold_verified_components(self.specs()) as verified:
            self.assertEqual(len(verified), 2)  # max, not sum: workers run sequentially
        spec = replace(self.specs()[0], worker_memory_bytes=DEFAULT_WORKER_MEMORY + 1)
        with self.assertRaises(ComponentPreflightError):
            with hold_verified_components((spec,)):
                self.fail("Over-budget declaration accepted")
        for limit in (True, -1, 5 * 1024**3):
            with self.assertRaises(ComponentPreflightError):
                with hold_verified_components(self.specs(), worker_memory_limit=limit):
                    self.fail("Invalid budget accepted")
        for deadline in (float("nan"), float("inf"), 0, 601, True):
            with self.assertRaises(ComponentPreflightError):
                with hold_verified_components(self.specs(), timeout_seconds=deadline):
                    self.fail("Invalid deadline accepted")

    @unittest.skipUnless(sys.platform == "win32", "Windows retained sharing proof")
    def test_windows_readonly_lease_denies_write_delete_replace_until_release(self):
        replacement = self.root / "unowned replacement.fixture"
        replacement.write_bytes(b"KEEP UNOWNED")
        with hold_verified_components(self.specs()):
            for path in (self.engine, self.model):
                with self.assertRaises(OSError):
                    path.open("r+b")
                with self.assertRaises(OSError):
                    path.unlink()
                with self.assertRaises(OSError):
                    os.replace(replacement, path)
                with path.open("rb") as reader:
                    self.assertTrue(reader.read(1))
        with self.engine.open("r+b") as writer:
            self.assertTrue(writer.read(1))
        self.assertEqual(replacement.read_bytes(), b"KEEP UNOWNED")

    def test_cli_metadata_only_no_db_no_artifact_execution_and_nonzero_failure(self):
        manifest = self.root / "selected components.json"
        manifest.write_bytes(self.raw())
        self.assertEqual(load_component_manifest(manifest), self.specs())
        data_root = self.root / "must-not-create-user-data"
        env = {**os.environ, "CREATOR_LOOP_DATA_ROOT": str(data_root)}
        app_path = str(Path(__file__).resolve().parents[1] / "app")
        env["PYTHONPATH"] = app_path
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        command = [
            sys.executable,
            "-m",
            "creator_loop",
            "--check-components",
            str(manifest),
        ]
        result = subprocess.run(
            command, cwd=self.root, env=env, capture_output=True, timeout=20
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["check"], "LOCAL_ARTIFACTS_VERIFIED")
        self.assertFalse(payload["runtime_compatibility_verified"])
        self.assertTrue(all(not item["owned"] for item in payload["components"]))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertFalse(data_root.exists())
        manifest.write_bytes(self.raw([{**self.entries[0], "sha256": "0" * 64}]))
        result = subprocess.run(
            command, cwd=self.root, env=env, capture_output=True, timeout=20
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["check"], "FAILED")
        self.assertFalse(data_root.exists())
        self.assertNotIn(self.engine.read_bytes(), result.stdout + result.stderr)
        result = subprocess.run(
            command + ["--smoke"],
            cwd=self.root,
            env=env,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(data_root.exists())

    def test_directory_refused_before_opening_and_no_file_creation(self):
        specs = (replace(self.specs()[0], path=self.root),)
        with patch(
            "creator_loop.local_components._open_read_lock",
            side_effect=self.tracked_open,
        ):
            with self.assertRaises(ComponentPreflightError):
                with hold_verified_components(specs):
                    self.fail("Directory accepted as component")
        self.assertEqual(self.opened, [])

    @unittest.skipIf(
        sys.platform == "win32",
        "Windows lease denies concurrent writer; portable boundary check",
    )
    def test_first_artifact_mutation_while_hashing_second_refused_before_yield(self):
        specs = self.specs()

        def swap_after_first(path):
            if path == self.model:
                self.engine.write_bytes(b"CHANGED DURING SECOND HASH")
            return self.tracked_open(path)

        with patch(
            "creator_loop.local_components._open_read_lock",
            side_effect=swap_after_first,
        ):
            with self.assertRaises(ComponentPreflightError):
                with hold_verified_components(specs):
                    self.fail("Earlier component mutation accepted at lease boundary")
        self.assertTrue(all(stream.closed for stream in self.opened))
        self.assertTrue(self.engine.is_file())
        self.assertTrue(self.model.is_file())
