"""Scoped renderer orchestration tests; fake tools never run media or models."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from creator_loop.backend.video.renderer import LocalFFmpegRenderer
from creator_loop.backend.video.tools import LocalToolError, ToolOutput
from creator_loop.shared.workflow import (
    DraftSnapshot,
    RenderPreset,
    RenderRequest,
    Scene,
    Subtitle,
    VerifiedMedia,
)


class FakeTools:
    version = "test"

    def __init__(self):
        self.calls = []
        self.output_frames = 48
        self.duration_ms = 2000
        self.width, self.height = 360, 640
        self.cancel_after_output = None
        self.fail_encode = False
        self.bad_output = False

    @contextmanager
    def lease(self, cancelled, timeout_seconds):
        yield self

    def run(self, tool, arguments, **kwargs):
        self.calls.append((tool, arguments))
        cwd = kwargs["cwd"]
        if "-version" in arguments:
            return ToolOutput(
                f"{tool} version test\nconfiguration: --enable-libx264 --enable-libfreetype"
            )
        if tool == "ffprobe":
            path = Path(arguments[-1])
            if path.suffix == ".wav":
                streams = [{"codec_type": "audio", "codec_name": "pcm_s16le"}]
            else:
                streams = [
                    {
                        "codec_type": "video",
                        "codec_name": "h264",
                        "width": 999 if self.bad_output else self.width,
                        "height": self.height,
                        "nb_frames": str(self.output_frames),
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ]
            return ToolOutput(
                json.dumps(
                    {
                        "streams": streams,
                        "format": {
                            "duration": str(self.duration_ms / 1000),
                            "size": str(path.stat().st_size),
                            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                        },
                    }
                )
            )
        if "null" in arguments:
            return ToolOutput("")
        if self.fail_encode:
            raise LocalToolError("Synthetic encoder failure")
        output = Path(arguments[-1])
        if not output.is_absolute():
            output = cwd / output
        output.write_bytes(
            b"FAKE MP4 UNIT TEST ONLY\n"
            + b"\n".join(path.read_bytes() for path in sorted(cwd.glob("scene-*.txt")))
            + b"\n".join(
                path.read_bytes() for path in sorted(cwd.glob("subtitle-*.txt"))
            )
        )
        if ".partial." in output.name and self.cancel_after_output is not None:
            self.cancel_after_output()
        return ToolOutput("")


class RendererTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "Đầu ra có dấu space"
        self.root.mkdir()
        self.font = self.root / "synthetic-font.ttf"
        self.font.write_bytes(b"FAKE FONT NOT FOR NATIVE RENDER")
        self.tools = FakeTools()
        self.renderer = LocalFFmpegRenderer(
            self.root, tools=self.tools, font_path=self.font
        )
        self.draft = DraftSnapshot(
            "draft-v1", "project", 1, "Sự thật cần căn cứ.", "VIDEO"
        )
        self.request = RenderRequest(
            "run1",
            self.draft,
            (Scene("Sự thật cần căn cứ.", 2000),),
            self.root / "storage" / "derived",
        )
        self.events = []

    def render(self, request=None, cancelled=lambda: False):
        return self.renderer.render(
            request or self.request, self.events.append, cancelled
        )

    def test_complete_exact_version_mp4_has_digest_and_no_import_execution(self):
        self.assertEqual(self.tools.calls, [])
        result = self.render()
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.draft_version_id, "draft-v1")
        self.assertEqual(result.duration_ms, 2000)
        self.assertEqual(result.byte_size, result.output_path.stat().st_size)
        self.assertEqual(
            result.sha256, hashlib.sha256(result.output_path.read_bytes()).hexdigest()
        )
        self.assertEqual(self.events[-1].status, "SUCCEEDED")
        self.assertEqual(list((self.root / "runtime").iterdir()), [])
        self.assertFalse(list(self.request.output_directory.glob("*.partial.mp4")))
        self.assertTrue(any("null" in args for _tool, args in self.tools.calls))

    def test_new_draft_creates_new_render_keeps_old_and_same_run_cannot_overwrite(self):
        old = self.render()
        old_bytes = old.output_path.read_bytes()
        modified = replace(
            self.request,
            run_id="run2",
            draft=replace(self.draft, draft_version_id="draft-v2", version_no=2),
            scenes=(Scene("Kiểm chứng trước khi kết luận.", 2000),),
        )
        new = self.render(modified)
        self.assertEqual(new.status, "SUCCEEDED")
        self.assertNotEqual(new.sha256, old.sha256)
        self.assertEqual(old.output_path.read_bytes(), old_bytes)
        duplicate = self.render()
        self.assertEqual(duplicate.status, "FAILED")
        self.assertIsNone(duplicate.output_path)
        self.assertEqual(old.output_path.read_bytes(), old_bytes)

    def test_cancelled_partial_never_becomes_success_and_cleanup_preserves_old(self):
        old = self.render()
        flag = [False]
        self.tools.cancel_after_output = lambda: flag.__setitem__(0, True)
        cancelled = self.render(
            replace(self.request, run_id="cancelled"), lambda: flag[0]
        )
        self.assertEqual(cancelled.status, "CANCELLED")
        self.assertIsNone(cancelled.output_path)
        self.assertTrue(old.output_path.is_file())
        self.assertFalse(
            (self.request.output_directory / "render-cancelled.mp4").exists()
        )
        self.assertEqual(len(list(self.request.output_directory.iterdir())), 1)
        self.assertEqual(list((self.root / "runtime").iterdir()), [])

    def test_cancel_before_start_does_not_execute_any_tool(self):
        result = self.render(cancelled=lambda: True)
        self.assertEqual(result.status, "CANCELLED")
        self.assertEqual(self.tools.calls, [])

    def test_hardlinked_system_font_can_be_copied_but_media_alias_is_rejected(self):
        font_alias = self.root / "font-winsxs-alias.ttf"
        os.link(self.font, font_alias)
        result = self.render()
        self.assertEqual(result.status, "SUCCEEDED", result.error)
        media_path = self.root / "hardlinked.png"
        media_path.write_bytes(b"synthetic image")
        os.link(media_path, self.root / "another-hardlink.png")
        image = VerifiedMedia(
            "image",
            "asset",
            media_path,
            hashlib.sha256(media_path.read_bytes()).hexdigest(),
            "IMAGE",
            True,
        )
        result = self.render(
            replace(
                self.request,
                run_id="alias-media",
                scenes=(Scene("", 2000, "IMAGE", "image"),),
                media=(image,),
            )
        )
        self.assertEqual(result.status, "FAILED")
        self.assertIsNone(result.output_path)

    def test_bad_probe_and_encoder_failure_do_not_publish(self):
        self.tools.bad_output = True
        result = self.render()
        self.assertEqual(result.status, "FAILED")
        self.assertFalse(list(self.request.output_directory.iterdir()))
        self.tools.bad_output = False
        self.tools.fail_encode = True
        result = self.render(replace(self.request, run_id="fail2"))
        self.assertEqual(result.status, "FAILED")
        self.assertFalse(list(self.request.output_directory.iterdir()))

    def test_missing_reuse_rights_and_wrong_digest_fail_closed(self):
        source = self.root / "image.png"
        source.write_bytes(b"synthetic image")
        image = VerifiedMedia(
            "image",
            "asset",
            source,
            hashlib.sha256(source.read_bytes()).hexdigest(),
            "IMAGE",
            False,
        )
        request = replace(
            self.request, scenes=(Scene("", 2000, "IMAGE", "image"),), media=(image,)
        )
        result = self.render(request)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(self.tools.calls, [])
        request = replace(
            request, media=(replace(image, reuse_allowed=True, sha256="0" * 64),)
        )
        result = self.render(request)
        self.assertEqual(result.status, "FAILED")
        self.assertIn("Digest", result.error)
        self.assertFalse(any("-vf" in args for _tool, args in self.tools.calls))

    def test_clip_trim_voiceover_and_vietnamese_subtitles_are_explicit_local_args(self):
        clip = self.root / "video.mp4"
        audio = self.root / "voice.wav"
        clip.write_bytes(b"synthetic clip")
        audio.write_bytes(b"synthetic audio")
        media = tuple(
            VerifiedMedia(
                name,
                name + "asset",
                path,
                hashlib.sha256(path.read_bytes()).hexdigest(),
                kind,
                True,
            )
            for name, path, kind in (("clip", clip, "VIDEO"), ("voice", audio, "AUDIO"))
        )
        self.tools.duration_ms = 3000
        self.tools.output_frames = 48
        # Input probe has 3s; final output probe must describe the 2s request.
        original = self.tools.run

        def run(tool, arguments, **kwargs):
            self.tools.duration_ms = 2000 if ".partial." in arguments[-1] else 3000
            return original(tool, arguments, **kwargs)

        self.tools.run = run
        text = "Tiếng Việt: sự thật %{expr:bad}; {literal}"
        request = replace(
            self.request,
            scenes=(Scene("", 2000, "VIDEO", "clip", 500),),
            media=media,
            subtitles=(Subtitle(0, 2000, text),),
            voiceover_file_id="voice",
        )
        result = self.render(request)
        self.assertEqual(result.status, "SUCCEEDED", result.error)
        calls = self.tools.calls
        self.assertTrue(any("-ss" in args and "0.500" in args for _tool, args in calls))
        self.assertTrue(
            any("1:a:0" in args and str(audio) in args for _tool, args in calls)
        )
        self.assertFalse(any(text in " ".join(args) for _tool, args in calls))
        self.assertTrue(
            any("expansion=none" in " ".join(args) for _tool, args in calls)
        )
        self.assertIn(
            text, " ".join(result.output_path.read_text(encoding="utf-8").split())
        )

    def test_changed_media_bytes_are_not_accepted_as_verified_input(self):
        source = self.root / "same-path.png"
        source.write_bytes(b"original")
        image = VerifiedMedia(
            "image",
            "asset",
            source,
            hashlib.sha256(b"original").hexdigest(),
            "IMAGE",
            True,
        )
        source.write_bytes(b"changed")
        request = replace(
            self.request, scenes=(Scene("", 2000, "IMAGE", "image"),), media=(image,)
        )
        result = self.render(request)
        self.assertEqual(result.status, "FAILED")
        self.assertIsNone(result.output_path)

    def test_invalid_run_or_timeline_fails_before_tool_execution(self):
        result = self.render(replace(self.request, run_id="../other"))
        self.assertEqual(result.status, "FAILED")
        result = self.render(replace(self.request, preset=RenderPreset(width=361)))
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(self.tools.calls, [])


if __name__ == "__main__":
    unittest.main()
