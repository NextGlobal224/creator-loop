"""RAW remains machine output; invalid anchors cannot be silently clipped."""

import json
import unittest

from creator_loop.whisper_adapter import parse_whisper_raw


class WhisperRawTests(unittest.TestCase):
    def _body(self):
        return {
            "model": {"type": "base", "multilingual": True},
            "params": {"language": "vi", "translate": False},
            "result": {"language": "vi"},
            "transcription": [
                {"offsets": {"from": 0, "to": 1000}, "text": " lời gốc\r\n"}
            ],
        }

    def _parse(self, body):
        return parse_whisper_raw(
            json.dumps(body).encode(), duration_ms=1000, language="vi"
        )

    def test_raw_text_is_preserved_without_review_or_normalization(self):
        raw = json.dumps(self._body(), ensure_ascii=False).encode("utf-8")
        before = bytes(raw)
        segments = parse_whisper_raw(raw, duration_ms=1000, language="vi")
        self.assertEqual(raw, before)
        self.assertEqual(segments[0].text, " lời gốc\r\n")
        self.assertEqual((segments[0].start_ms, segments[0].end_ms), (0, 1000))

    def test_silence_can_have_empty_output_without_creating_evidence(self):
        body = self._body()
        body["transcription"] = []
        self.assertEqual(self._parse(body), ())

    def test_outside_reversed_float_boolean_and_overlapping_offsets_refuse(self):
        for start, end in ((-1, 1000), (0, 1001), (10, 10), (0.0, 1000), (False, 1000)):
            with self.subTest(start=start, end=end):
                body = self._body()
                body["transcription"][0]["offsets"] = {"from": start, "to": end}
                with self.assertRaisesRegex(ValueError, "offset"):
                    self._parse(body)
        body = self._body()
        body["transcription"].append(
            {"offsets": {"from": 500, "to": 1000}, "text": "overlap"}
        )
        with self.assertRaisesRegex(ValueError, "offset"):
            self._parse(body)

    def test_wrong_model_translation_language_or_structure_refuses(self):
        for section, key, value in (
            ("model", "type", "tiny"),
            ("model", "multilingual", False),
            ("params", "translate", True),
            ("params", "language", "en"),
            ("result", "language", "en"),
        ):
            with self.subTest(section=section, key=key):
                body = self._body()
                body[section][key] = value
                with self.assertRaisesRegex(ValueError, "model/language"):
                    self._parse(body)

    def test_duplicate_keys_invalid_utf8_nul_and_surrogate_text_refuse(self):
        for raw in (b'{"model":{},"model":{}}', b"\xff", b"{"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_whisper_raw(raw, duration_ms=1000, language="vi")
        for text in ("\x00", "\ud800"):
            body = self._body()
            body["transcription"][0]["text"] = text
            with self.assertRaisesRegex(ValueError, "text"):
                self._parse(body)
