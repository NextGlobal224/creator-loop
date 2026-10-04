"""Exercise actual supplied CLI Qt flow on new private fixtures and inspect its DB."""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

from PySide6.QtGui import QImage


def snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    executable = args.executable.resolve(strict=True)
    root = args.work_root.absolute()
    for path in (root, *root.parents):
        if path.is_symlink() or path.is_junction():
            raise ValueError("Linked probe root is forbidden")
    root.mkdir()  # exclusive: preserve any older evidence
    inputs = root / "inputs"
    inputs.mkdir()
    (inputs / "Original.txt").write_text("Cafe\u0301 ở Huế", encoding="utf-8")
    image = QImage(12, 8, QImage.Format.Format_RGB32)
    image.fill(0xFF0088AA)
    for x in range(6, 12):
        for y in range(8):
            image.setPixel(x, y, 0xFFEE2222)
    if not image.save(str(inputs / "Original.png")):
        raise RuntimeError("Cannot create synthetic image")
    repository = Path(__file__).resolve().parents[1]
    (inputs / "Original.mp4").write_bytes(
        (repository / "tests/fixtures/video-with-tone.mp4").read_bytes()
    )
    foreign = root / "unselected default"
    foreign.mkdir()
    (foreign / "keep.bin").write_bytes(b"Never initialize or mutate unselected data")
    env = {**os.environ, "CREATOR_LOOP_DATA_ROOT": str(foreign)}
    python = executable.name.lower().startswith("python")
    prefix = [str(executable), "-m", "creator_loop"] if python else [str(executable)]
    command = prefix + ["--product-flow-smoke", str(root)]
    result = subprocess.run(command, env=env, capture_output=True, timeout=90)
    (root / "stdout.log").write_bytes(result.stdout)
    (root / "stderr.log").write_bytes(result.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"Product flow failed exit{result.returncode}; see private stdout/stderr logs"
        )
    receipt = json.loads(result.stdout)
    if (
        type(receipt.get("frozen")) is not bool
        or receipt["frozen"] != (not python)
        or type(receipt.get("original_count")) is not int
        or receipt["original_count"] != 3
        or any(
            receipt.get(key) is not True
            for key in (
                "reopened",
                "revoked_history_retained",
                "zero_distinct_from_missing",
            )
        )
        or Path(receipt["data_root"]) != root / "data"
        or any(
            re.fullmatch(r"[0-9a-f]{32}", receipt[key]) is None
            for key in ("package_id", "post_id")
        )
    ):
        raise RuntimeError("Malformed product flow receipt")
    data = root / "data"
    with closing(
        sqlite3.connect((data / "creator_loop.sqlite3").as_uri() + "?mode=ro", uri=True)
    ) as db:
        if (
            db.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
            or db.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise RuntimeError("Private flow database integrity/FK failed")
        if db.execute("SELECT count(*) FROM assets").fetchone()[0] != 3:
            raise RuntimeError("Originals missing")
        if db.execute(
            "SELECT locator_type,count(*) FROM evidence_versions GROUP BY locator_type ORDER BY locator_type"
        ).fetchall() != [
            ("IMAGE_REGION", 2),
            ("TEXT_RANGE", 1),
            ("TIME_RANGE", 4),
            ("WHOLE_ASSET", 3),
        ]:
            raise RuntimeError("Three-media Evidence/append-only history missing")
        for kind in ("image", "video", "audio"):
            ids = receipt["media_evidence"]["histories"][kind]
            if (
                not isinstance(ids, list)
                or len(ids) != 2
                or any(
                    not isinstance(i, str) or re.fullmatch(r"[0-9a-f]{32}", i) is None
                    for i in ids
                )
            ):
                raise RuntimeError("Malformed Evidence history receipt")
            rows = db.execute(
                "SELECT evidence_id,version_no,locator_data FROM evidence_versions WHERE evidence_version_id IN (?,?) ORDER BY version_no",
                ids,
            ).fetchall()
            if (
                len(rows) != 2
                or rows[0][0] != rows[1][0]
                or [r[1] for r in rows] != [1, 2]
            ):
                raise RuntimeError("Old/new Evidence identity or version changed")
            expected = (
                [
                    {"x": 0.25, "y": 0.25, "width": 0.5, "height": 0.5},
                    {"x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0},
                ]
                if kind == "image"
                else [
                    {"start_ms": 203, "end_ms": 607, "track": kind},
                    {"start_ms": 701, "end_ms": 901, "track": kind},
                ]
            )
            if [json.loads(r[2]) for r in rows] != expected:
                raise RuntimeError("Old/new saved image/time locator changed")
            for identifier, action in zip(ids, ("CORRECT", "ACCEPT"), strict=True):
                if db.execute(
                    "SELECT action FROM review_events WHERE evidence_version_id=?",
                    (identifier,),
                ).fetchall() != [(action,)]:
                    raise RuntimeError("Exact Evidence review history changed")
        if db.execute(
            "SELECT a.media_type FROM evidence_versions v JOIN asset_files f ON f.file_id=v.anchor_file_id JOIN assets a ON a.asset_id=f.asset_id WHERE v.locator_type='WHOLE_ASSET' ORDER BY a.media_type"
        ).fetchall() != [("IMAGE",), ("TEXT",), ("VIDEO",)]:
            raise RuntimeError("WHOLE_ASSET did not cover all three actual sources")
        playback = receipt["playback"]
        if (
            not isinstance(playback, list)
            or len(playback) != 5
            or any(
                type(p.get("frames")) is not int
                or type(p.get("pcm_buffers")) is not int
                or type(p.get("whole")) is not bool
                or type(p.get("audio_only")) is not bool
                or (not p["audio_only"] and p["frames"] <= 0)
                or ((p["whole"] or p["audio_only"]) and p["pcm_buffers"] <= 0)
                for p in playback
            )
        ):
            raise RuntimeError("Actual three-media playback receipt incomplete")
        if db.execute("SELECT decision FROM approvals ORDER BY rowid").fetchall() != [
            ("APPROVED",),
            ("REVOKED",),
        ]:
            raise RuntimeError("Approval history not retained")
        if db.execute("SELECT post_id,status FROM posts").fetchall() != [
            (receipt["post_id"], "PUBLISHED")
        ]:
            raise RuntimeError("Post history not retained")
        if db.execute(
            "SELECT metric_key,numeric_value,raw_value FROM observation_metrics"
        ).fetchall() != [("views", 0.0, "0")]:
            raise RuntimeError("Observation zero/missing provenance changed")
        for key, digest in db.execute("SELECT storage_key,sha256 FROM asset_files"):
            if hashlib.sha256((data / key).read_bytes()).hexdigest() != digest:
                raise RuntimeError("Original bytes changed")
    protected = {
        name: snapshot(path)
        for name, path in (("data", data), ("inputs", inputs), ("foreign", foreign))
    }
    for label, extra, expected in (
        ("existing-root", [], 4),
        ("mixed-flags", ["--backup"], 2),
    ):
        refused = subprocess.run(
            command + extra, env=env, capture_output=True, timeout=15
        )
        (root / f"{label}-stdout.log").write_bytes(refused.stdout)
        (root / f"{label}-stderr.log").write_bytes(refused.stderr)
        if refused.returncode != expected:
            raise RuntimeError(f"{label} did not refuse")
        for name, path in (("data", data), ("inputs", inputs), ("foreign", foreign)):
            if snapshot(path) != protected[name]:
                raise RuntimeError(f"{label} mutated {name}")
    # Normal supplied launcher must reopen the generated history and exit too.
    reopened = subprocess.run(
        prefix + ["--compatible-only", "--ui-smoke"],
        env={**env, "CREATOR_LOOP_DATA_ROOT": str(data)},
        capture_output=True,
        timeout=60,
    )
    (root / "normal-reopen-stdout.log").write_bytes(reopened.stdout)
    (root / "normal-reopen-stderr.log").write_bytes(reopened.stderr)
    if reopened.returncode != 0:
        raise RuntimeError("Normal packaged Library reopen failed")
    if list(foreign.iterdir()) != [foreign / "keep.bin"]:
        raise RuntimeError("Unselected default root was initialized")
    print(
        json.dumps(
            {
                "frozen": receipt["frozen"],
                "scope": "actual supplied programmatic Qt widgets/writers/private fixture/normal Library reopen; not manual walkthrough, engine/model, hardware or release",
                "work_root": str(root),
            },
            ensure_ascii=True,
        )
    )


if __name__ == "__main__":
    main()
