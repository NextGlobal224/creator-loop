import unittest

from app.creator_loop.locator import validate_locator


class TextSnapshotDigestMismatchTests(unittest.TestCase):
    def test_text_range_rejects_digest_that_does_not_match_snapshot(self):
        snapshot = "Đà Nẵng"

        locator = (
            '{"start":0,"end":4,'
            '"text_digest":"sha256:'
            '0000000000000000000000000000000000000000000000000000000000000000"}'
        )

        with self.assertRaisesRegex(
            ValueError,
            "Text digest does not match NFC snapshot",
        ):
            validate_locator(
                "TEXT_RANGE",
                locator,
                text_snapshot=snapshot,
            )


if __name__ == "__main__":
    unittest.main()
