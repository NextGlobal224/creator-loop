import hashlib
import json
import unicodedata
import unittest

from creator_loop.locator import validate_locator


class TextSnapshotLocatorTests(unittest.TestCase):
    def test_text_range_uses_nfc_snapshot_code_points_and_digest(self):
        # Same visible text, but this input contains decomposed "e + combining acute".
        raw_snapshot = "Cafe\u0301 Đà Nẵng"
        nfc_snapshot = unicodedata.normalize("NFC", raw_snapshot)

        self.assertNotEqual(raw_snapshot, nfc_snapshot)
        self.assertEqual(nfc_snapshot, "Café Đà Nẵng")

        digest = "sha256:" + hashlib.sha256(
            nfc_snapshot.encode("utf-8")
        ).hexdigest()

        locator = json.dumps(
            {
                "start": 0,
                "end": 4,
                "text_digest": digest,
            },
            ensure_ascii=False,
        )

        result = validate_locator(
            "TEXT_RANGE",
            locator,
            text_snapshot=raw_snapshot,
        )

        self.assertEqual(result["start"], 0)
        self.assertEqual(result["end"], 4)
        self.assertEqual(result["text_digest"], digest)


if __name__ == "__main__":
    unittest.main()
