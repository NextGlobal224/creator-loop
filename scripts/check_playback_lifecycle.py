"""Exercise the exact candidate's private playback entry, pixels/PCM and cleanup."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("Playback artifact probe requires Windows")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from creator_loop.isolated_decode import decode_isolated
    from creator_loop.isolated_playback import IsolatedMediaPlayer
    from creator_loop.owned_process import OwnedWindowsProcess, _api
    from creator_loop.paths import ensure_data_root
    from PySide6.QtMultimedia import QAudioBufferOutput, QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    from PySide6.QtWidgets import QApplication, QWidget

    executable = args.executable.resolve(strict=True)
    root = args.work_root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    ensure_data_root(root)
    original = (
        Path(__file__).resolve().parents[1] / "tests/fixtures/video-with-tone.mp4"
    ).read_bytes()
    source = root / "storage/originals/video-with-tone.mp4"
    source.write_bytes(original)
    app = QApplication.instance() or QApplication([])
    kernel = _api()
    source_mode = executable.name.lower().startswith("python")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")

    def candidate(_executable, arguments, logs, **kwargs):
        private = (
            arguments[arguments.index("--play-media") :]
            if "--play-media" in arguments
            else arguments[arguments.index("--decode-media") :]
        )
        kwargs["environment"] = environment
        return OwnedWindowsProcess(
            executable,
            (["-m", "creator_loop"] if source_mode else []) + private,
            logs,
            **kwargs,
        )

    with patch("creator_loop.isolated_decode.OwnedWindowsProcess", candidate):
        decoded, _image = decode_isolated(source, root, mode="video", start_ms=0)
    duration = decoded["duration_ms"]

    def wait(predicate, errors):
        deadline = time.monotonic() + 10
        while not predicate() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)
        assert predicate(), errors

    def player_for(owner):
        player = IsolatedMediaPlayer(
            owner,
            path=source,
            root=root,
            duration_ms=duration,
            size=len(original),
            sha256=hashlib.sha256(original).hexdigest(),
        )
        video = QVideoWidget(owner)
        player.setVideoOutput(video)
        player.setAudioOutput(QAudioOutput(owner))
        return player, video

    owner = QWidget()
    player, video = player_for(owner)
    colors, samples, errors = [], [], []
    player.videoSink().videoFrameChanged.connect(
        lambda frame: (
            colors.append(frame.toImage().pixelColor(0, 0)) if frame.isValid() else None
        )
    )
    buffers = QAudioBufferOutput(player)
    player.setAudioBufferOutput(buffers)
    buffers.audioBufferReceived.connect(
        lambda buffer: samples.append(buffer.frameCount()) if buffer.isValid() else None
    )
    player.errorOccurred.connect(lambda code, message: errors.append(message))
    with patch("creator_loop.isolated_playback.OwnedWindowsProcess", candidate):
        player.play()
    assert player.process is not None, errors
    assert player.process.executable == executable
    monitor = kernel.OpenProcess(0x00101000, False, player.process.record["pid"])
    assert monitor
    try:
        try:
            source.open("wb").close()
        except OSError:
            pass
        else:
            raise AssertionError("Candidate playback did not retain original lock")
        wait(lambda: player.process is None, errors)
        assert not errors, errors
        assert any(color.red() > color.blue() for color in colors)
        assert any(color.blue() > color.red() for color in colors)
        assert sum(samples) > 0
        assert player.position() > 700
        assert player.mediaStatus() == QMediaPlayer.MediaStatus.EndOfMedia
        assert kernel.WaitForSingleObject(monitor, 0) == 0
    finally:
        player._shutdown()
        kernel.CloseHandle(monitor)
    # A separate real candidate is cancelled immediately after assignment.
    cancel_owner = QWidget()
    cancelled, cancel_video = player_for(cancel_owner)
    with patch("creator_loop.isolated_playback.OwnedWindowsProcess", candidate):
        cancelled.play()
    assert cancelled.process is not None
    monitor = kernel.OpenProcess(0x00101000, False, cancelled.process.record["pid"])
    assert monitor
    try:
        started = time.monotonic()
        cancelled.stop()
        assert time.monotonic() - started < 0.2
        wait(lambda: cancelled.process is None, [])
        assert kernel.WaitForSingleObject(monitor, 0) == 0
    finally:
        cancelled._shutdown()
        kernel.CloseHandle(monitor)
    assert source.read_bytes() == original
    assert not list((root / "runtime").iterdir())
    assert not list((root / "logs").glob("*/stdout.log"))
    assert not list((root / "logs").glob("*/stderr.log"))
    assert not (root / "creator_loop.sqlite3").exists()
    owner.close()
    cancel_owner.close()
    # Keep sink widgets alive until every native writer/tree has finished.
    _ = video, cancel_video
    print(
        "Exact candidate playback real red/blue pixels + PCM + native normal/cancel exit + original/logs/no-DB PASS"
    )


if __name__ == "__main__":
    main()
