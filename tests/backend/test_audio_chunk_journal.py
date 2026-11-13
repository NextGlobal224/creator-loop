"""Offline SQLite journal and fake response tests; no provider or cloud calls."""

import asyncio
import copy
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

from creator_loop.backend.knowledge.audio_chunk_journal import (
    create_chunk_journal,
    dispatch_next_chunk,
    merge_chunk_journal,
    record_chunk_response,
    reserve_next_chunk,
    stop_chunk_journal,
)
from creator_loop.backend.knowledge.cloud_audio import probe_pcm_wav

from tests.backend.test_audio_playback import m4a, mp3


def pcm(seconds=120):
    out = io.BytesIO()
    with wave.open(out, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(1)
        writer.setframerate(8000)
        writer.writeframes(b"\x80" * (8000 * seconds))
    return out.getvalue()


def fake_response(packet):
    return {
        "request_id": packet["request_id"],
        "source_sha256": packet["source_sha256"],
        "input_sha256": packet["input_sha256"],
        "model": packet["model"],
        "deployment": packet["deployment"],
        "native_usage": None,
        "native_response": {
            "text": "Lời nói tại ranh giới.",
            "transcription_info": {"duration": 120, "language": "vi"},
            "segments": [{"start": 0, "end": 120, "text": "Lời nói tại ranh giới."}],
        },
    }


class AudioChunkJournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "control.sqlite"
        self.db = sqlite3.connect(self.path)
        self.db.execute(
            "CREATE TABLE control(id TEXT PRIMARY KEY, record TEXT NOT NULL)"
        )
        self.addCleanup(lambda: self.db.close())
        self.run_id = "a" * 32
        self.chunk = pcm()

    def create(self, source):
        return create_chunk_journal(
            self.db,
            run_id=self.run_id,
            source=source,
            decoded_chunks=(self.chunk,) * 5,
            duration_ms=600_000,
            decoder="fake decoder; provenance not verified",
            model="fake-whisper-model",
            deployment="fixture-version",
        )

    def reopen(self):
        self.db.close()
        self.db = sqlite3.connect(self.path)

    def test_five_responses_resume_and_merge_with_source_timestamps(self):
        source = mp3()
        self.create(source)
        packets = []

        async def fake_provider(packet, raw):
            self.assertEqual(raw, self.chunk)
            packets.append(packet)
            with closing(sqlite3.connect(self.path)) as other:
                with self.assertRaisesRegex(ValueError, "unknown"):
                    reserve_next_chunk(
                        other, run_id=self.run_id, source=source, chunk=self.chunk
                    )
            return fake_response(packet)

        for index in range(5):
            self.assertEqual(
                asyncio.run(
                    dispatch_next_chunk(
                        self.db,
                        run_id=self.run_id,
                        source=source,
                        chunk=self.chunk,
                        provider=fake_provider,
                    )
                ),
                "RECEIVED",
            )
            packet = packets[-1]
            self.assertEqual(packet["start_ms"], index * 120_000)
            self.reopen()
            response = fake_response(packet)
            record_chunk_response(self.db, run_id=self.run_id, envelope=response)
        self.assertEqual(len({p["request_id"] for p in packets}), 5)
        merged = merge_chunk_journal(self.db, run_id=self.run_id, source=source)
        self.assertEqual(
            [(s.start_ms, s.end_ms) for s in merged.segments],
            [(i * 120_000, (i + 1) * 120_000) for i in range(5)],
        )
        self.assertEqual(merged.text.count("Lời nói tại ranh giới."), 5)
        self.assertIn("CHUNK_BOUNDARIES_REQUIRE_REVIEW", merged.warnings)
        self.assertEqual(merged.review_state, "PENDING")
        record = json.loads(self.db.execute("SELECT record FROM control").fetchone()[0])
        self.assertEqual([c["state"] for c in record["chunks"]], ["RECEIVED"] * 5)
        self.assertTrue(
            all(c["envelope"]["native_usage"] is None for c in record["chunks"])
        )

    def test_fail_closed_on_changed_bytes_wrong_response_and_partial_transaction(self):
        source = m4a()
        self.create(source)
        with self.assertRaisesRegex(ValueError, "Original"):
            reserve_next_chunk(
                self.db, run_id=self.run_id, source=source + b"x", chunk=self.chunk
            )
        with self.assertRaisesRegex(ValueError, "chunk changed"):
            reserve_next_chunk(self.db, run_id=self.run_id, source=source, chunk=pcm(2))
        self.db.execute("BEGIN")
        with self.assertRaisesRegex(ValueError, "committed transaction"):
            reserve_next_chunk(
                self.db, run_id=self.run_id, source=source, chunk=self.chunk
            )
        self.db.rollback()
        packet = reserve_next_chunk(
            self.db, run_id=self.run_id, source=source, chunk=self.chunk
        )
        bad = fake_response(packet)
        bad["input_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            record_chunk_response(self.db, run_id=self.run_id, envelope=bad)
        for name in ("model", "deployment"):
            bad = fake_response(packet)
            bad[name] = "different"
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                record_chunk_response(self.db, run_id=self.run_id, envelope=bad)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            merge_chunk_journal(self.db, run_id=self.run_id, source=source)
        response = fake_response(packet)
        record_chunk_response(self.db, run_id=self.run_id, envelope=response)
        conflict = copy.deepcopy(response)
        conflict["native_response"]["text"] = "Khác"
        with self.assertRaisesRegex(ValueError, "conflicts"):
            record_chunk_response(self.db, run_id=self.run_id, envelope=conflict)

    def test_late_response_after_stop_retained_but_never_merged_or_retried(self):
        source = mp3()
        self.create(source)
        packet = reserve_next_chunk(
            self.db, run_id=self.run_id, source=source, chunk=self.chunk
        )
        stop_chunk_journal(self.db, run_id=self.run_id)
        self.reopen()
        self.assertEqual(
            record_chunk_response(
                self.db, run_id=self.run_id, envelope=fake_response(packet)
            ),
            "CANCELLED_RECEIPT_RETAINED",
        )
        record = json.loads(self.db.execute("SELECT record FROM control").fetchone()[0])
        self.assertEqual(record["state"], "CANCELLED")
        self.assertEqual(record["chunks"][0]["state"], "RECEIVED")
        with self.assertRaisesRegex(ValueError, "stopped"):
            reserve_next_chunk(
                self.db, run_id=self.run_id, source=source, chunk=self.chunk
            )
        with self.assertRaisesRegex(ValueError, "stopped"):
            merge_chunk_journal(self.db, run_id=self.run_id, source=source)

    def test_invalid_native_response_is_retained_and_blocks_further_dispatch(self):
        source = mp3()
        self.create(source)
        packet = reserve_next_chunk(
            self.db, run_id=self.run_id, source=source, chunk=self.chunk
        )
        response = fake_response(packet)
        response["native_response"]["segments"][0]["end"] = 121
        self.assertEqual(
            record_chunk_response(self.db, run_id=self.run_id, envelope=response),
            "INVALID",
        )
        self.reopen()
        record = json.loads(self.db.execute("SELECT record FROM control").fetchone()[0])
        self.assertEqual(record["chunks"][0]["envelope"], response)
        with self.assertRaisesRegex(ValueError, "unknown"):
            reserve_next_chunk(
                self.db, run_id=self.run_id, source=source, chunk=self.chunk
            )
        with self.assertRaisesRegex(ValueError, "incomplete"):
            merge_chunk_journal(self.db, run_id=self.run_id, source=source)

    def test_stop_while_provider_runs_returns_cancelled_with_receipt(self):
        source = mp3()
        self.create(source)

        async def stopped_provider(packet, raw):
            stop_chunk_journal(self.db, run_id=self.run_id)
            return fake_response(packet)

        result = asyncio.run(
            dispatch_next_chunk(
                self.db,
                run_id=self.run_id,
                source=source,
                chunk=self.chunk,
                provider=stopped_provider,
            )
        )
        self.assertEqual(result, "CANCELLED_RECEIPT_RETAINED")
        self.reopen()
        record = json.loads(self.db.execute("SELECT record FROM control").fetchone()[0])
        self.assertEqual(record["state"], "CANCELLED")
        self.assertEqual(record["chunks"][0]["state"], "RECEIVED")

    def test_two_local_sessions_cannot_reserve_the_same_chunk(self):
        source = mp3()
        self.create(source)
        barrier = Barrier(2)

        def reserve():
            with closing(sqlite3.connect(self.path, timeout=10)) as connection:
                barrier.wait(timeout=5)
                try:
                    reserve_next_chunk(
                        connection, run_id=self.run_id, source=source, chunk=self.chunk
                    )
                    return "reserved"
                except ValueError:
                    return "blocked"

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(
                sorted(executor.map(lambda _: reserve(), range(2))),
                ["blocked", "reserved"],
            )

    def test_actual_dispatch_sequencer_with_fake_provider_and_interruption(self):
        source = m4a()
        self.create(source)
        calls = []

        async def fake_provider(packet, raw):
            self.assertEqual(raw, self.chunk)
            calls.append(packet["request_id"])
            return fake_response(packet)

        self.assertEqual(
            asyncio.run(
                dispatch_next_chunk(
                    self.db,
                    run_id=self.run_id,
                    source=source,
                    chunk=self.chunk,
                    provider=fake_provider,
                )
            ),
            "RECEIVED",
        )
        self.reopen()

        async def interrupted(packet, raw):
            calls.append(packet["request_id"])
            raise TimeoutError("fixture interruption")

        with self.assertRaises(TimeoutError):
            asyncio.run(
                dispatch_next_chunk(
                    self.db,
                    run_id=self.run_id,
                    source=source,
                    chunk=self.chunk,
                    provider=interrupted,
                )
            )
        self.reopen()
        with self.assertRaisesRegex(ValueError, "unknown"):
            asyncio.run(
                dispatch_next_chunk(
                    self.db,
                    run_id=self.run_id,
                    source=source,
                    chunk=self.chunk,
                    provider=fake_provider,
                )
            )
        self.assertEqual(len(calls), 2)

    def test_incomplete_duration_and_duplicate_run_fail_closed(self):
        source = mp3()
        with self.assertRaisesRegex(ValueError, "do not cover"):
            create_chunk_journal(
                self.db,
                run_id=self.run_id,
                source=source,
                decoded_chunks=(self.chunk,) * 5,
                duration_ms=600_001,
                decoder="fake decoder",
                model="fake-whisper-model",
                deployment="fixture-version",
            )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM control").fetchone()[0], 0
        )
        self.create(source)
        with self.assertRaises(sqlite3.IntegrityError):
            self.create(source)

    def test_fractional_milliseconds_and_mixed_formats_are_rejected(self):
        out = io.BytesIO()
        with wave.open(out, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(1)
            writer.setframerate(8000)
            writer.writeframes(b"\x80" * 8002)
        with self.assertRaisesRegex(ValueError, "fractional milliseconds"):
            create_chunk_journal(
                self.db,
                run_id=self.run_id,
                source=mp3(),
                decoded_chunks=(out.getvalue(), pcm(2)),
                duration_ms=3001,
                decoder="fake",
                model="fake",
                deployment="fixture",
            )
        last = create_chunk_journal(
            self.db,
            run_id=self.run_id,
            source=mp3(),
            decoded_chunks=(out.getvalue(),),
            duration_ms=1001,
            decoder="fake",
            model="fake",
            deployment="fixture",
        )
        self.assertTrue(last["final_duration_ceiled"])
        packet = reserve_next_chunk(
            self.db, run_id=self.run_id, source=mp3(), chunk=out.getvalue()
        )
        response = fake_response(packet)
        response["native_response"]["transcription_info"]["duration"] = 1.001
        response["native_response"]["segments"][0]["end"] = 1
        record_chunk_response(self.db, run_id=self.run_id, envelope=response)
        merged = merge_chunk_journal(self.db, run_id=self.run_id, source=mp3())
        self.assertIn("FINAL_DURATION_CEILED_TO_MILLISECOND", merged.warnings)
        from tests.backend.test_cloud_audio import wav

        with self.assertRaisesRegex(ValueError, "format changed"):
            create_chunk_journal(
                self.db,
                run_id="b" * 32,
                source=mp3(),
                decoded_chunks=(pcm(2), wav()),
                duration_ms=4000,
                decoder="fake",
                model="fake",
                deployment="fixture",
            )

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg unavailable")
    def test_generated_ten_minute_audio_decodes_to_chunks_and_fake_run(self):
        # Synthetic silence and fake transcription; no speech quality claim.
        for index, (extension, codec) in enumerate(
            (("mp3", "libmp3lame"), ("m4a", "aac"))
        ):
            directory = Path(self.tmp.name) / extension
            directory.mkdir()
            original = directory / ("original." + extension)
            decoded = directory / "decoded.pcm"
            subprocess.run(
                [
                    shutil.which("ffmpeg"),
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "anullsrc=r=16000:cl=mono",
                    "-t",
                    "600",
                    "-threads",
                    "1",
                    "-c:a",
                    codec,
                    "-b:a",
                    "32k",
                    str(original),
                ],
                check=True,
                capture_output=True,
                timeout=90,
            )
            subprocess.run(
                [
                    shutil.which("ffmpeg"),
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    str(original),
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "8000",
                    "-c:a",
                    "pcm_s16le",
                    "-f",
                    "s16le",
                    str(decoded),
                ],
                check=True,
                capture_output=True,
                timeout=90,
            )
            pcm_bytes = decoded.read_bytes()
            self.assertEqual(len(pcm_bytes) % 2, 0)
            chunk_list = []
            for offset in range(0, len(pcm_bytes), 119 * 8000 * 2):
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as writer:
                    writer.setnchannels(1)
                    writer.setsampwidth(2)
                    writer.setframerate(8000)
                    writer.writeframes(pcm_bytes[offset : offset + 119 * 8000 * 2])
                chunk_list.append(buffer.getvalue())
            chunks = tuple(chunk_list)
            duration = sum(probe_pcm_wav(raw).duration_ms for raw in chunks)
            self.assertTrue(599_000 <= duration <= 602_000)
            source, run_id = original.read_bytes(), str(index + 1) * 32
            plan = create_chunk_journal(
                self.db,
                run_id=run_id,
                source=source,
                decoded_chunks=chunks,
                duration_ms=duration,
                decoder="local ffmpeg fixture",
                model="fake-whisper-model",
                deployment="fixture-version",
            )
            duration_by_request = {
                item["request_id"]: item["probe"]["duration_ms"]
                for item in plan["chunks"]
            }

            async def fake_provider(packet, raw):
                response = fake_response(packet)
                duration_ms = duration_by_request[packet["request_id"]]
                response["native_response"]["transcription_info"]["duration"] = (
                    duration_ms / 1000
                )
                response["native_response"]["segments"][0]["end"] = (
                    duration_ms - 1
                ) / 1000
                return response

            for chunk in chunks:
                self.assertEqual(
                    asyncio.run(
                        dispatch_next_chunk(
                            self.db,
                            run_id=run_id,
                            source=source,
                            chunk=chunk,
                            provider=fake_provider,
                        )
                    ),
                    "RECEIVED",
                )
                self.reopen()
            merged = merge_chunk_journal(self.db, run_id=run_id, source=source)
            self.assertEqual(len(merged.segments), len(chunks))
            self.assertEqual(merged.segments[0].start_ms, 0)
            for item, segment in zip(plan["chunks"], merged.segments):
                self.assertEqual(segment.start_ms, item["start_ms"])
                self.assertLessEqual(
                    segment.end_ms, item["start_ms"] + item["probe"]["duration_ms"]
                )
            self.assertGreater(merged.segments[-1].end_ms, 599_000)
            self.assertEqual(merged.review_state, "PENDING")
            self.assertIn("CHUNK_BOUNDARIES_REQUIRE_REVIEW", merged.warnings)


if __name__ == "__main__":
    unittest.main()
