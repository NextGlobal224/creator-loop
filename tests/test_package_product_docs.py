"""Offline guide contents/link closure and refusal preserve package/user bytes."""

import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from scripts.package_product_docs import DOCUMENTS, package_docs


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.targets.extend(value for key, value in attrs if key == "href")


class ProductDocsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve(strict=True) / "Bản cài đặt Huế"
        self.root.mkdir()
        (self.root / "CreatorLoop.exe").write_bytes(
            b"synthetic package marker, never executed"
        )
        (self.root / "_internal").mkdir()
        (self.root / "_internal/keep.bin").write_bytes(b"Unchanged application bytes")
        self.commit = "a" * 40

    def snapshot(self):
        return {
            p.relative_to(self.root).as_posix(): hashlib.sha256(
                p.read_bytes()
            ).hexdigest()
            for p in self.root.rglob("*")
            if p.is_file()
        }

    def test_existing_guidance_refuses_before_mutation(self):
        (self.root / "USER_GUIDE.html").write_bytes(b"Keep previous guidance")
        before = self.snapshot()
        with self.assertRaises(FileExistsError):
            package_docs(self.root, self.commit)
        self.assertEqual(before, self.snapshot())

    def test_invalid_source_or_incomplete_package_refuses_before_mutation(self):
        before = self.snapshot()
        with self.assertRaises(ValueError):
            package_docs(self.root, "not-a-source-commit")
        self.assertEqual(before, self.snapshot())
        (self.root / "CreatorLoop.exe").unlink()
        before = self.snapshot()
        with self.assertRaises(ValueError):
            package_docs(self.root, self.commit)
        self.assertEqual(before, self.snapshot())

    @unittest.skipUnless(
        importlib.util.find_spec("PySide6") is not None and os.name == "nt",
        "requires Windows Qt build host",
    )
    def test_offline_html_link_closure_no_private_payload_and_original_bytes(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        before = self.snapshot()
        info = package_docs(self.root, self.commit)
        self.assertEqual(info["git_commit"], self.commit)
        self.assertEqual(info["product_documents"], len(DOCUMENTS))
        self.assertEqual(
            json.loads((self.root / "build-info.json").read_text(encoding="utf-8")),
            info,
        )
        files = list(self.root.rglob("*.html"))
        self.assertEqual(len(files), len(DOCUMENTS))
        guide = (self.root / "USER_GUIDE.html").read_text(encoding="utf-8")
        for text in (
            "Creator Loop",
            "Evidence",
            "Package",
            "Observation",
            "sao lưu",
            "khôi phục",
        ):
            self.assertIn(text, guide)
        for p in files:
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("file://", text)
            self.assertNotIn("HANDOFF.md", text)
            self.assertNotIn("Codex-Kit-v2", text)
            parser = Links()
            parser.feed(text)
            for target in parser.targets:
                url = urlparse(target)
                if url.scheme:
                    continue
                dest = (p.parent / unquote(url.path)).resolve()
                dest.relative_to(self.root)
                self.assertTrue(dest.exists(), (p, target))
        after = self.snapshot()
        for name, digest in before.items():
            self.assertEqual(after[name], digest)
        self.assertFalse((self.root / "docs/HANDOFF.html").exists())
        self.assertFalse((self.root / "docs/history").exists())


if __name__ == "__main__":
    unittest.main()
