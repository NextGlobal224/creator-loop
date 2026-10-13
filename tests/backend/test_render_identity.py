"""Renderer cache/provenance snapshots with bounded synthetic fonts and source."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import struct
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from creator_loop.backend.runtime.local_components import ComponentSpec
from creator_loop.backend.video import render_identity
from creator_loop.backend.video.render_identity import (
    SOURCE_NAMES,
    SOURCE_PREFIX,
    RendererIdentityError,
    renderer_identity,
)


def synthetic_font(version: str = "Version 1.234") -> bytes:
    text = version.encode("utf-16-be")
    name = (
        struct.pack(">HHH", 0, 1, 18)
        + struct.pack(">HHHHHH", 3, 1, 0x409, 5, len(text), 0)
        + text
    )
    return (
        struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
        + struct.pack(">4sIII", b"name", 0, 28, len(name))
        + name
    )


class RenderIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        for number, name in enumerate(SOURCE_NAMES):
            (self.source / name).write_bytes(
                f"# synthetic {name}\nVALUE = {number}\n".encode()
            )
        self.font = self.root / "font.ttf"
        self.font.write_bytes(synthetic_font())
        self.specs = tuple(
            ComponentSpec(
                name + "-exe",
                "ENGINE",
                self.root / (name + ".exe"),
                "9.0.2",
                digit * 64,
                128,
                "https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-9.0.2-essentials_build.zip",
                "GPL-3.0-or-later",
                "https://www.gyan.dev/ffmpeg/builds/",
                512 * 1024**2,
                True,
                False,
            )
            for name, digit in (("ffmpeg", "a"), ("ffprobe", "b"))
        )
        for target, value in (
            ("__file__", str(self.source / "render_identity.py")),
            ("_font", lambda: self.font),
        ):
            patcher = patch.object(render_identity, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(sys, "frozen", False, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_deterministic_code_font_components_and_effective_configuration(self):
        first = renderer_identity(self.specs)
        second = renderer_identity(tuple(reversed(self.specs)))
        self.assertEqual(first, second)
        self.assertEqual(first["font_version"], "Version 1.234")
        self.assertEqual(
            first["font_sha256"], hashlib.sha256(self.font.read_bytes()).hexdigest()
        )
        self.assertEqual(first["font_path"], str(self.font.resolve()))
        self.assertEqual(len(first["source_hashes"]), 7)
        self.assertEqual(first["configuration"]["font_sha256"], first["font_sha256"])
        self.assertEqual(
            first["configuration"]["components"]["ffmpeg-exe"]["sha256"], "a" * 64
        )
        self.assertIn("libx264", first["encoder_args"])
        self.assertEqual(first["budgets"]["render_timeout_seconds"], 180)

    def test_lf_normalization_matches_official_git_source_hashes(self):
        before = renderer_identity(self.specs)
        for name in SOURCE_NAMES:
            path = self.source / name
            path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
        after = renderer_identity(self.specs)
        self.assertEqual(before["source_hashes"], after["source_hashes"])
        self.assertEqual(before["source_fingerprint"], after["source_fingerprint"])

    def test_source_font_or_selected_component_change_invalidates_identity(self):
        original = renderer_identity(self.specs)
        (self.source / "renderer.py").write_bytes(b"# changed implementation\n")
        self.assertNotEqual(
            renderer_identity(self.specs)["source_fingerprint"],
            original["source_fingerprint"],
        )
        self.font.write_bytes(synthetic_font("Version 2.000"))
        updated = renderer_identity(self.specs)
        self.assertNotEqual(updated["font_sha256"], original["font_sha256"])
        self.assertEqual(updated["font_version"], "Version 2.000")
        tools = (replace(self.specs[0], sha256="c" * 64), self.specs[1])
        self.assertNotEqual(
            renderer_identity(tools)["pinned_components"], original["pinned_components"]
        )

    def test_frozen_identity_uses_build_info_without_source_files_or_git(self):
        expected = renderer_identity(self.specs)["source_hashes"]
        installation = self.root / "installation"
        installation.mkdir()
        (installation / "build-info.json").write_text(
            json.dumps({"source_hashes": expected}), encoding="utf-8"
        )
        with (
            patch.object(sys, "frozen", True),
            patch.object(sys, "executable", str(installation / "CreatorLoop.exe")),
            patch.object(
                render_identity, "__file__", str(self.root / "absent" / "compiled.py")
            ),
            patch(
                "subprocess.Popen", side_effect=AssertionError("Git/process forbidden")
            ),
        ):
            result = renderer_identity(self.specs)
        self.assertEqual(result["source_hashes"], expected)

    def test_frozen_missing_nonhex_duplicate_or_oversize_build_identity_fails_closed(
        self,
    ):
        installation = self.root / "bad-installation"
        installation.mkdir()
        manifest = installation / "build-info.json"
        hashes = {SOURCE_PREFIX + name: "d" * 64 for name in SOURCE_NAMES}
        variants = [
            None,
            "{}",
            json.dumps({"source_hashes": {}}),
            json.dumps(
                {"source_hashes": {**hashes, SOURCE_PREFIX + "renderer.py": "nothex"}}
            ),
            '{"source_hashes":{},"source_hashes":{}}',
            " " * (2 * 1024 * 1024 + 1),
        ]
        with (
            patch.object(sys, "frozen", True),
            patch.object(sys, "executable", str(installation / "CreatorLoop.exe")),
        ):
            for raw in variants:
                manifest.unlink(missing_ok=True)
                if raw is not None:
                    manifest.write_text(raw, encoding="utf-8")
                with (
                    self.subTest(raw=raw[:50] if raw else raw),
                    self.assertRaises(RendererIdentityError),
                ):
                    renderer_identity(self.specs)

    def test_missing_source_and_invalid_or_oversize_font_fail_closed(self):
        (self.source / "audio.py").unlink()
        with self.assertRaises(RendererIdentityError):
            renderer_identity(self.specs)
        (self.source / "audio.py").write_bytes(b"# restored\n")
        self.font.write_bytes(b"not a TrueType font")
        with self.assertRaises(RendererIdentityError):
            renderer_identity(self.specs)
        with self.font.open("wb") as stream:
            stream.truncate(20 * 1024 * 1024 + 1)
        with self.assertRaises(RendererIdentityError):
            renderer_identity(self.specs)

    def test_hardlinked_font_is_supported_without_becoming_an_asset(self):
        os.link(self.font, self.root / "font-alias.ttf")
        self.assertEqual(renderer_identity(self.specs)["font_version"], "Version 1.234")

    def test_component_set_is_complete_consistent_and_licensed(self):
        for specs in (
            self.specs[:1],
            (*self.specs, self.specs[0]),
            (self.specs[0], replace(self.specs[1], version="different")),
            (replace(self.specs[0], kind="MODEL"), self.specs[1]),
            (replace(self.specs[0], local_use_allowed=False), self.specs[1]),
        ):
            with self.subTest(specs=specs), self.assertRaises(ValueError):
                renderer_identity(specs)

    def test_import_and_identity_start_no_process_or_network(self):
        with (
            patch("subprocess.Popen", side_effect=AssertionError("Process on import")),
            patch(
                "socket.create_connection",
                side_effect=AssertionError("Network on import"),
            ),
        ):
            # Execute fresh module code rather than rely on an already cached import.
            specification = importlib.util.spec_from_file_location(
                "creator_loop_video_identity_import_probe",
                render_identity._source_hashes.__code__.co_filename,
            )
            self.assertIsNotNone(specification)
            self.assertIsNotNone(specification.loader)
            module = importlib.util.module_from_spec(specification)
            with patch(
                "creator_loop.backend.video.renderer._font",
                side_effect=AssertionError("Font read on import"),
            ):
                specification.loader.exec_module(module)
            renderer_identity(self.specs)


if __name__ == "__main__":
    unittest.main()
