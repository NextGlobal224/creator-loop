"""Real UI scenario shared with the packaged probe; independent boundary checks."""

import hashlib
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

if os.name == "nt":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication
    except ImportError:
        QApplication = None
else:
    QApplication = None

if QApplication is not None:
    from creator_loop.database import open_readonly
    from creator_loop.product_flow_smoke import run_product_flow_smoke


@unittest.skipUnless(QApplication is not None, "requires Windows and PySide6")
class ProductUIFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "Product flow Huế"
        self.root.mkdir()
        inputs = self.root / "inputs"
        inputs.mkdir()
        (inputs / "Original.txt").write_text("Cafe\u0301 ở Huế", encoding="utf-8")
        image = QImage(12, 8, QImage.Format.Format_RGB32)
        image.fill(0xFF0088AA)
        self.assertTrue(image.save(str(inputs / "Original.png")))
        (inputs / "Original.mp4").write_bytes(
            (Path(__file__).parent / "fixtures" / "video-with-tone.mp4").read_bytes()
        )

    def _snapshot(self):
        return {
            p.relative_to(self.root).as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def test_three_inputs_to_publication_observation_reopen_and_revocation(self):
        inputs_before = self._snapshot()
        receipt = run_product_flow_smoke(self.root)
        self.assertIs(receipt["frozen"], False)
        self.assertIs(receipt["reopened"], True)
        self.assertIs(receipt["revoked_history_retained"], True)
        self.assertIs(receipt["zero_distinct_from_missing"], True)
        with closing(open_readonly(self.root / "data" / "creator_loop.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM assets").fetchone()[0], 3)
            self.assertEqual(
                db.execute("SELECT count(*) FROM draft_versions").fetchone()[0], 2
            )
            self.assertEqual(
                db.execute("SELECT decision FROM approvals ORDER BY rowid").fetchall(),
                [("APPROVED",), ("REVOKED",)],
            )
            self.assertEqual(
                db.execute("SELECT post_id,status FROM posts").fetchall(),
                [(receipt["post_id"], "PUBLISHED")],
            )
            self.assertEqual(
                db.execute(
                    "SELECT metric_key,numeric_value,raw_value FROM observation_metrics"
                ).fetchall(),
                [("views", 0.0, "0")],
            )
        after = self._snapshot()
        for name, digest in inputs_before.items():
            self.assertEqual(after[name], digest)

    def test_existing_data_refused_before_any_mutation(self):
        data = self.root / "data"
        data.mkdir()
        (data / "creator_loop.sqlite3").write_bytes(b"existing DB must not be opened")
        before = self._snapshot()
        with self.assertRaises(FileExistsError):
            run_product_flow_smoke(self.root)
        self.assertEqual(self._snapshot(), before)

    def test_missing_input_refused_without_creating_data(self):
        (self.root / "inputs" / "Original.mp4").unlink()
        before = self._snapshot()
        with self.assertRaises(ValueError):
            run_product_flow_smoke(self.root)
        self.assertFalse((self.root / "data").exists())
        self.assertEqual(self._snapshot(), before)

    def test_oversized_input_refused_without_creating_data(self):
        (self.root / "inputs" / "Original.txt").write_bytes(b"x" * (1024 * 1024 + 1))
        before = self._snapshot()
        with self.assertRaises(ValueError):
            run_product_flow_smoke(self.root)
        self.assertFalse((self.root / "data").exists())
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
