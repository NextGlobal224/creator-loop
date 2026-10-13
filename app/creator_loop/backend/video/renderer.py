"""Actual local MP4 rendering of an exact Draft Version, with no cloud calls."""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from creator_loop.backend.publication.publication_media import _open_read_lock
from creator_loop.backend.runtime.local_components import ComponentSpec
from creator_loop.backend.runtime.runtime_ownership import create_workspace_marker
from creator_loop.backend.runtime.windows_paths import file_io_path, resolve_file_path
from creator_loop.backend.video.audio import SILENCE_INPUT, audio_output_args
from creator_loop.backend.video.subtitles import (
    subtitle_filters,
    text_filter,
    write_scene_text,
    write_srt,
)
from creator_loop.backend.video.timeline import (
    TimedScene,
    Timeline,
    VideoPlanError,
    build_timeline,
    media_kind,
)
from creator_loop.backend.video.tools import (
    LocalToolError,
    LocalVideoTools,
    ProbeResult,
    ToolName,
    VideoTools,
    parse_probe,
    probe_arguments,
)
from creator_loop.shared.workflow import (
    Progress,
    RenderRequest,
    RenderResult,
    RenderStatus,
    VerifiedMedia,
)

MAX_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_INPUT_BYTES = 4 * 1024**3
ENCODER_ARGS = [
    "-c:v",
    "libx264",
    "-preset",
    "ultrafast",
    "-crf",
    "28",
    "-maxrate",
    "2M",
    "-bufsize",
    "4M",
    "-pix_fmt",
    "yuv420p",
    "-threads",
    "2",
]
BASE_ARGS = [
    "-hide_banner",
    "-v",
    "error",
    "-nostdin",
    "-y",
    "-max_alloc",
    "67108864",
    "-filter_threads",
    "1",
    "-filter_complex_threads",
    "1",
]


def _canonical_file(path: Path, *, allow_font_hardlinks: bool = False) -> Path:
    canonical = resolve_file_path(path, strict=True)
    if os.path.normcase(str(path.absolute())) != os.path.normcase(str(canonical)):
        raise VideoPlanError("File render phải dùng đường dẫn chuẩn, không qua alias.")
    for item in (canonical, *canonical.parents):
        info = file_io_path(item).lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise VideoPlanError(
                "Không render file qua symlink/junction/reparse point."
            )
    info = file_io_path(canonical).stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or (info.st_nlink != 1 and not allow_font_hardlinks)
        or not 0 < info.st_size <= MAX_INPUT_BYTES
    ):
        raise VideoPlanError(
            "Cần file thường, không có alias và nằm trong giới hạn dung lượng."
        )
    return canonical


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _font() -> Path:
    # Existing system fonts only; never download a font/model when importing/opening.
    choices = (
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    )
    for choice in choices:
        if choice.is_file():
            return choice.resolve(strict=True)
    raise VideoPlanError(
        "Chưa có font local hỗ trợ tiếng Việt; hãy chọn font TrueType."
    )


class LocalFFmpegRenderer:
    """Construct safely on the UI thread; call render in an owned app worker.

    `specs` may contain unrelated selected components. Only ffmpeg-exe and
    ffprobe-exe are leased/executed. No Whisper, model or Facebook operation runs.
    `tools` is the explicit fake-adapter seam for fast tests, not a cloud fallback.
    """

    def __init__(
        self,
        data_root: Path,
        specs: tuple[ComponentSpec, ...] = (),
        *,
        worker_memory_limit: int = 512 * 1024 * 1024,
        timeout_seconds: float = 180,
        font_path: Path | None = None,
        tools: VideoTools | None = None,
    ) -> None:
        if not math.isfinite(timeout_seconds) or not 1 <= timeout_seconds <= 600:
            raise ValueError("Render deadline phải trong khoảng 1–600 giây.")
        self.root = data_root.resolve(strict=True)
        self.timeout_seconds = timeout_seconds
        self.font_path = font_path
        self.tools: VideoTools = tools or LocalVideoTools(
            self.root, specs, worker_memory_limit=worker_memory_limit
        )
        self.configuration: dict[str, object] = {
            "video_codec": "libx264",
            "audio_codec": "aac",
            "pixel_format": "yuv420p",
            "encoder_preset": "ultrafast",
            "crf": 28,
            "maxrate": "2M",
            "threads": 2,
            "worker_memory_limit": worker_memory_limit,
            "timeout_seconds": timeout_seconds,
            "components": {
                spec.component_id: {"version": spec.version, "sha256": spec.sha256}
                for spec in specs
                if spec.component_id in ("ffmpeg-exe", "ffprobe-exe")
            },
        }

    def render(
        self,
        request: RenderRequest,
        progress: Callable[[Progress], None],
        cancelled: Callable[[], bool],
    ) -> RenderResult:
        partial: Path | None = None
        deadline = time.monotonic() + self.timeout_seconds
        last_progress = 0

        def check() -> None:
            if cancelled():
                raise InterruptedError("Đã hủy render; chưa có MP4 hoàn tất.")
            if time.monotonic() >= deadline:
                raise TimeoutError("Render đã vượt deadline; chưa có MP4 hoàn tất.")

        def report(value: int, message: str, status: RenderStatus = "RUNNING") -> None:
            nonlocal last_progress
            check()
            last_progress = max(last_progress, min(1000, value))
            progress(Progress(request.run_id, status, last_progress, 1000, message))

        def run(
            tool: ToolName,
            arguments: list[str],
            workspace: Path,
            *,
            progress_file: Path | None = None,
            callback: Callable[[int], None] | None = None,
        ) -> str:
            check()
            result = self.tools.run(
                tool,
                arguments,
                cwd=workspace,
                timeout_seconds=max(0.001, deadline - time.monotonic()),
                cancelled=cancelled,
                progress_file=progress_file,
                on_progress=callback,
            )
            check()
            if result.exit_code:
                raise LocalToolError(f"{tool} thất bại (exit {result.exit_code}).")
            return result.stdout

        try:
            check()
            if (
                re.fullmatch(r"[A-Za-z0-9_-]{1,96}", request.run_id) is None
                or not request.draft.draft_version_id
                or request.draft.format.upper() != "VIDEO"
            ):
                raise VideoPlanError(
                    "Render cần run ID chuẩn và exact Draft Version video."
                )
            timeline = build_timeline(
                request.scenes,
                request.preset,
                request.media,
                request.subtitles,
                request.voiceover_file_id,
            )
            output = self._output_directory(request.output_directory)
            final = output / f"render-{request.run_id}.mp4"
            if file_io_path(final).exists():
                raise VideoPlanError(
                    "Run này đã có file; render mới phải dùng run ID mới."
                )
            partial = output / f".{request.run_id}-{uuid4().hex}.partial.mp4"
            report(0, "Kiểm tra component local và quyền dùng tư liệu")
            runtime = self.root / "runtime"
            if runtime.is_symlink() or runtime.is_junction():
                raise VideoPlanError("Renderer cần thư mục runtime thật.")
            runtime.mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="render-", dir=runtime) as work:
                workspace = Path(work)
                if isinstance(self.tools, LocalVideoTools):
                    create_workspace_marker(
                        self.root,
                        workspace,
                        f"ffmpeg/{self.tools.version}",
                        kind="LOCAL_RENDER_WORKSPACE",
                    )
                with self.tools.lease(
                    cancelled, max(0.001, deadline - time.monotonic())
                ):
                    self._check_tools(lambda tool, args: run(tool, args, workspace))
                    with ExitStack() as held:
                        media = self._hold_inputs(request, held, check)
                        # Windows system fonts commonly have a WinSxS hardlink.
                        # This is a read-only font copy, never an imported Asset.
                        font = _canonical_file(
                            self.font_path or _font(), allow_font_hardlinks=True
                        )
                        if file_io_path(font).stat().st_size > 20 * 1024 * 1024:
                            raise VideoPlanError("Font local vượt giới hạn 20 MiB.")
                        font_stream = held.enter_context(_open_read_lock(font))
                        raw_font = font_stream.read(20 * 1024 * 1024 + 1)
                        self.configuration["font_sha256"] = hashlib.sha256(
                            raw_font
                        ).hexdigest()
                        (workspace / "font.ttf").write_bytes(raw_font)
                        probes = {
                            key: parse_probe(
                                run("ffprobe", probe_arguments(item.path), workspace)
                            )
                            for key, item in media.items()
                        }
                        report(80, "Lập timeline từ kịch bản và file đã xác minh")
                        for index, timed in enumerate(timeline.scenes):
                            report(
                                100 + index * 600 // len(timeline.scenes),
                                f"Render cảnh {index + 1}/{len(timeline.scenes)}",
                            )
                            arguments = self._scene_arguments(
                                request, timed, index, workspace, media, probes
                            )
                            position = workspace / f"progress-scene-{index:03d}.txt"

                            def scene_progress(
                                ms: int,
                                n: int = index,
                                duration: int = timed.duration_ms,
                            ) -> None:
                                report(
                                    100
                                    + round(
                                        (n + min(ms / max(1, duration), 1))
                                        * 600
                                        / len(timeline.scenes)
                                    ),
                                    f"Render cảnh {n + 1}/{len(timeline.scenes)}",
                                )

                            run(
                                "ffmpeg",
                                [
                                    *arguments[:-1],
                                    "-progress",
                                    str(position),
                                    arguments[-1],
                                ],
                                workspace,
                                progress_file=position,
                                callback=scene_progress,
                            )
                            segment = workspace / f"segment-{index:03d}.mp4"
                            segment_info = parse_probe(
                                run("ffprobe", probe_arguments(segment), workspace)
                            )
                            if (
                                segment_info.frame_count != timed.frames
                                or segment_info.width != request.preset.width
                                or segment_info.height != request.preset.height
                                or segment_info.video_codec != "h264"
                                or segment_info.audio_codec != "aac"
                            ):
                                raise LocalToolError(
                                    "Cảnh render không có đủ frame/track theo timeline."
                                )
                        (workspace / "concat.txt").write_text(
                            "\n".join(
                                f"file 'segment-{i:03d}.mp4'"
                                for i in range(len(timeline.scenes))
                            )
                            + "\n",
                            encoding="ascii",
                        )
                        write_srt(workspace / "captions.srt", request.subtitles)
                        filters = subtitle_filters(
                            workspace, request.subtitles, request.preset
                        )
                        voiceover = media.get(request.voiceover_file_id or "")
                        if (
                            voiceover is not None
                            and probes[voiceover.file_id].audio_codec is None
                        ):
                            raise VideoPlanError(
                                "File lời đọc không có audio track thực tế."
                            )
                        report(700, "Ghép cảnh, phụ đề tiếng Việt và âm thanh local")
                        final_args = self._final_arguments(
                            request, timeline, partial, filters, voiceover
                        )
                        final_progress = workspace / "progress-final.txt"
                        run(
                            "ffmpeg",
                            [
                                *final_args[:-1],
                                "-progress",
                                str(final_progress),
                                final_args[-1],
                            ],
                            workspace,
                            progress_file=final_progress,
                            callback=lambda ms: report(
                                700
                                + min(
                                    180, round(ms * 180 / max(1, timeline.duration_ms))
                                ),
                                "Ghép MP4 preview",
                            ),
                        )
                        report(
                            900, "Kiểm tra MP4, codec, thời lượng và khả năng giải mã"
                        )
                        details = parse_probe(
                            run("ffprobe", probe_arguments(partial), workspace)
                        )
                        self._validate_output(partial, details, request, timeline)
                        run(
                            "ffmpeg",
                            [
                                *BASE_ARGS,
                                "-protocol_whitelist",
                                "file,pipe",
                                "-i",
                                str(partial),
                                "-map",
                                "0:v:0",
                                "-map",
                                "0:a:0",
                                "-f",
                                "null",
                                "-",
                            ],
                            workspace,
                        )
                        digest = self._hash_file(partial, check)
                        check()
                        # Same-directory hardlink publication refuses overwrite on all OSes.
                        os.link(file_io_path(partial), file_io_path(final))
                        file_io_path(partial).unlink()
                        partial = None
                        size = file_io_path(final).stat().st_size
                        result = RenderResult(
                            request.run_id,
                            request.draft.draft_version_id,
                            "SUCCEEDED",
                            final,
                            digest,
                            size,
                            details.duration_ms,
                            details.width,
                            details.height,
                            "ffmpeg",
                            self.tools.version,
                        )
                        # No cancellation window after publishing success; service commits this result.
                        progress(
                            Progress(
                                request.run_id,
                                "SUCCEEDED",
                                1000,
                                1000,
                                "MP4 local hoàn tất, chờ review",
                            )
                        )
                        return result
        except InterruptedError as exc:
            progress(
                Progress(
                    request.run_id,
                    "CANCELLED",
                    last_progress,
                    1000,
                    "Đã hủy render; không có output hoàn tất",
                )
            )
            return RenderResult(
                request.run_id,
                request.draft.draft_version_id,
                "CANCELLED",
                tool_version=self.tools.version,
                error=str(exc),
            )
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            progress(
                Progress(
                    request.run_id,
                    "FAILED",
                    last_progress,
                    1000,
                    "Render thất bại; giữ bản render cũ",
                )
            )
            return RenderResult(
                request.run_id,
                request.draft.draft_version_id,
                "FAILED",
                tool_version=self.tools.version,
                error=str(exc)[:320],
            )
        finally:
            if partial is not None and file_io_path(partial).exists():
                file_io_path(partial).unlink()

    def _output_directory(self, directory: Path) -> Path:
        if not directory.is_absolute():
            raise VideoPlanError(
                "Thư mục output phải là đường dẫn tuyệt đối do service chọn."
            )
        directory.mkdir(parents=True, exist_ok=True)
        canonical = resolve_file_path(directory, strict=True)
        if os.path.normcase(str(directory.absolute())) != os.path.normcase(
            str(canonical)
        ):
            raise VideoPlanError("Thư mục output không được qua alias.")
        for part in (canonical, *canonical.parents):
            info = file_io_path(part).lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & 0x400
            ):
                raise VideoPlanError("Thư mục output không được qua reparse point.")
        return canonical

    def _check_tools(self, run: Callable[[ToolName, list[str]], str]) -> None:
        for tool in ("ffmpeg", "ffprobe"):
            version = run(tool, ["-version"])
            first = version.splitlines()[0] if version.splitlines() else ""
            prefix = f"{tool} version {self.tools.version}"
            if not first.startswith(prefix) or (
                len(first) > len(prefix) and first[len(prefix)] not in " -"
            ):
                raise LocalToolError(
                    "Version thực tế của FFmpeg/ffprobe không khớp component đã chọn."
                )
            if "--enable-nonfree" in version:
                raise LocalToolError("Không dùng build FFmpeg có component nonfree.")
            if tool == "ffmpeg" and not all(
                flag in version for flag in ("--enable-libx264", "--enable-libfreetype")
            ):
                raise LocalToolError(
                    "Build FFmpeg thiếu H.264 hoặc bộ chữ tiếng Việt local."
                )

    def _hold_inputs(
        self, request: RenderRequest, held: ExitStack, check: Callable[[], None]
    ) -> dict[str, VerifiedMedia]:
        used = {item.file_id for item in request.scenes if item.file_id is not None}
        if request.voiceover_file_id is not None:
            used.add(request.voiceover_file_id)
        result: dict[str, VerifiedMedia] = {}
        for item in request.media:
            if item.file_id not in used:
                continue
            check()
            if re.fullmatch(r"[0-9a-f]{64}", item.sha256) is None:
                raise VideoPlanError("File tư liệu thiếu digest SHA-256 chuẩn.")
            canonical = _canonical_file(item.path)
            before = file_io_path(canonical).stat()
            stream = held.enter_context(_open_read_lock(canonical))
            opened = os.fstat(stream.fileno())
            if _identity(before) != _identity(opened):
                raise VideoPlanError("File tư liệu đã đổi identity trước render.")
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024):
                check()
                digest.update(chunk)
            check()
            if digest.hexdigest() != item.sha256 or _identity(
                os.fstat(stream.fileno())
            ) != _identity(opened):
                raise VideoPlanError(
                    "Digest file tư liệu không còn khớp; cần render từ version đúng."
                )
            stream.seek(0)
            result[item.file_id] = item
        return result

    def _scene_arguments(
        self,
        request: RenderRequest,
        timed: TimedScene,
        index: int,
        workspace: Path,
        media: dict[str, VerifiedMedia],
        probes: dict[str, ProbeResult],
    ) -> list[str]:
        scene, preset = timed.scene, request.preset
        duration = timed.frames / preset.fps
        args = list(BASE_ARGS)
        audio_stream = "1:a:0"
        if scene.kind == "TITLE":
            args += [
                "-f",
                "lavfi",
                "-i",
                f"color=c=0x101827:s={preset.width}x{preset.height}:r={preset.fps}:d={duration:.6f}",
                *SILENCE_INPUT,
            ]
        else:
            item = media[scene.file_id or ""]
            probe = probes[item.file_id]
            if (
                probe.width is None
                or probe.width <= 0
                or probe.height is None
                or probe.height <= 0
                or probe.width * probe.height > 16_777_216
            ):
                raise VideoPlanError(
                    "File ảnh/video không có kích thước hợp lệ hoặc vượt 16 MP."
                )
            args += ["-protocol_whitelist", "file,pipe"]
            if media_kind(item) == "IMAGE":
                args += [
                    "-loop",
                    "1",
                    "-framerate",
                    str(preset.fps),
                    "-i",
                    str(item.path),
                    *SILENCE_INPUT,
                ]
            else:
                if (
                    probe.duration_ms is None
                    or scene.clip_start_ms + timed.duration_ms > probe.duration_ms + 50
                ):
                    raise VideoPlanError(
                        "Đoạn cắt vượt thời lượng video thực tế hoặc chưa biết thời lượng."
                    )
                args += [
                    "-ss",
                    f"{scene.clip_start_ms / 1000:.3f}",
                    "-i",
                    str(item.path),
                ]
                if probe.audio_codec is None:
                    args += list(SILENCE_INPUT)
                else:
                    audio_stream = "0:a:0"
        vf = f"scale={preset.width}:{preset.height}:force_original_aspect_ratio=decrease,pad={preset.width}:{preset.height}:(ow-iw)/2:(oh-ih)/2:color=0x101827,setsar=1,fps={preset.fps},setpts=PTS-STARTPTS"
        if scene.text.strip():
            filename, size = write_scene_text(workspace, index, scene.text, preset)
            vf += "," + text_filter(filename, preset, title=True, font_size=size)
        args += [
            "-map",
            "0:v:0",
            "-map",
            audio_stream,
            "-vf",
            vf,
            "-t",
            f"{duration:.6f}",
            "-frames:v",
            str(timed.frames),
            *ENCODER_ARGS,
            *audio_output_args(timed.duration_ms),
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            f"segment-{index:03d}.mp4",
        ]
        return args

    def _final_arguments(
        self,
        request: RenderRequest,
        timeline: Timeline,
        partial: Path,
        filters: tuple[str, ...],
        voiceover: VerifiedMedia | None,
    ) -> list[str]:
        args = [
            *BASE_ARGS,
            "-protocol_whitelist",
            "file,pipe",
            "-f",
            "concat",
            "-safe",
            "1",
            "-i",
            "concat.txt",
        ]
        if voiceover is not None:
            args += ["-protocol_whitelist", "file,pipe", "-i", str(voiceover.path)]
        args += ["-map", "0:v:0", "-map", "1:a:0" if voiceover else "0:a:0"]
        # AAC priming can shift concat demux timestamps by one frame. Rebuild
        # the video clock from the verified scene frame sequence, not audio PTS.
        args += [
            "-vf",
            ",".join((f"setpts=N/({request.preset.fps}*TB)", *filters)),
            "-r",
            str(request.preset.fps),
        ]
        args += [
            "-t",
            f"{timeline.frames / request.preset.fps:.6f}",
            "-frames:v",
            str(timeline.frames),
            *ENCODER_ARGS,
            *audio_output_args(timeline.duration_ms),
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(partial),
        ]
        return args

    def _validate_output(
        self,
        path: Path,
        details: ProbeResult,
        request: RenderRequest,
        timeline: Timeline,
    ) -> None:
        info = file_io_path(path).stat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or not 0 < info.st_size <= MAX_OUTPUT_BYTES
            or details.byte_size != info.st_size
            or details.video_codec != "h264"
            or details.audio_codec != "aac"
            or details.width != request.preset.width
            or details.height != request.preset.height
            or details.duration_ms is None
            or abs(details.duration_ms - timeline.duration_ms)
            > max(100, 2000 // request.preset.fps)
            or details.frame_count != timeline.frames
            or "mp4" not in details.format_name.split(",")
        ):
            raise LocalToolError(
                "MP4 không khớp timeline, codec, frame count hoặc output size."
            )

    @staticmethod
    def _hash_file(path: Path, check: Callable[[], None]) -> str:
        digest = hashlib.sha256()
        with file_io_path(path).open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                check()
                digest.update(chunk)
        return digest.hexdigest()
