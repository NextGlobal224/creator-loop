"""Exact Windows EXE decodes synthetic fixtures through inherited locked files."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.windows_owned_file import OwnedWindowsFile
from PySide6.QtGui import QImage


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--component-version", required=True)
    parser.add_argument("--source", action="store_true")
    args = parser.parse_args()
    args.work_root.mkdir()  # fixture directory is new and owned by this probe
    environment = os.environ.copy()
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["CREATOR_LOOP_DATA_ROOT"] = str(args.work_root / "must-not-initialize")
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
    prefix = ["-m", "creator_loop"] if args.source else []
    cases = (
        ("video", "video-red-blue.mp4", "video", 600, None, 0),
        ("audio", "video-with-tone.mp4", "audio", 200, 600, 0),
        ("no-audio", "video-red-blue.mp4", "audio", 200, 600, 2),
    )
    for name, filename, mode, start, end, expected_exit in cases:
        work = args.work_root / name
        work.mkdir()
        original = work / "locked original.mp4"
        request = work / "request.json"
        request.write_text(
            json.dumps(
                {
                    "format": 1,
                    "path": str(original.resolve()),
                    "mode": mode,
                    "start_ms": start,
                    "end_ms": end,
                    "timeout_seconds": 8,
                    "require_audio": False,
                }
            ),
            encoding="utf-8",
        )
        fixture = Path(__file__).resolve().parents[1] / "tests/fixtures" / filename
        expected_bytes = fixture.read_bytes()
        with OwnedWindowsFile.create_new(original).stream as source:
            source.write(expected_bytes)
            source.flush()
            source.seek(0)
            with OwnedWindowsProcess(
                args.executable,
                [*prefix, "--decode-media", str(request.resolve())],
                work / "ownership",
                component_version=args.component_version,
                environment=environment,
                capture_output=False,
                stdin_source=source,
                memory_limit_bytes=512 * 1024 * 1024,
            ) as process:
                outcome = process.wait(15)
            if outcome.timed_out or outcome.exit_code != expected_exit:
                raise RuntimeError(f"Exact decoder {name} failed: {outcome}")
            source.seek(0)
            if source.read() != expected_bytes:
                raise RuntimeError("Exact decoder altered the original")
        body = json.loads((work / "response.json").read_text(encoding="utf-8"))
        if expected_exit:
            if "audio track" not in body.get("error", ""):
                raise RuntimeError(
                    "Missing audio must be refused by actual decoded metadata"
                )
        elif mode == "video":
            frame = work / "frame.png"
            if hashlib.sha256(frame.read_bytes()).hexdigest() != body["frame_sha256"]:
                raise RuntimeError("Exact decoded frame digest mismatch")
            image = QImage(str(frame))
            color = image.pixelColor(0, 0)
            if (
                image.isNull()
                or color.blue() <= color.red()
                or not 600 <= body["frame_time_ms"] < body["duration_ms"]
            ):
                raise RuntimeError(
                    "Exact video locator did not decode the blue fixture frame"
                )
        elif not (
            body["buffer_start_ms"] < 600
            and body["buffer_end_ms"] > 200
            and body["duration_ms"] >= 600
        ):
            raise RuntimeError(
                "Exact audio locator lacks an actual overlapping sample buffer"
            )
        print(
            json.dumps(
                {
                    "case": name,
                    "exit_code": outcome.exit_code,
                    "elapsed_seconds": outcome.elapsed_seconds,
                }
            )
        )
    if (args.work_root / "must-not-initialize").exists():
        raise RuntimeError("Decoder initialized user data")
    print("Exact inherited-handle video/audio decoder probes PASS")


if __name__ == "__main__":
    main()
