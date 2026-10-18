"""The deployable archive must retain login/module closure and migrations."""

import hashlib
import json
import runpy
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch


class WebRuntimePackageTests(unittest.TestCase):
    def fixture(self, directory):
        repo = Path(directory)
        web = repo / "cloudflare/creator-web"
        files = {
            "cloudflare/creator-web/wrangler.jsonc": "{}",
            "cloudflare/creator-web/src/entry.py": "# fixture",
            "cloudflare/creator-web/src/creator_ai.mjs": "// fixture",
            "cloudflare/creator-web/src/migrations/001.sql": "SELECT 1;",
            "migrations/001.sql": "SELECT 1;",
            "cloudflare/creator-web/python_modules/workers/__init__.py": "# fixture",
            "cloudflare/creator-web/public/index.html": '<script src="/app.js"></script>',
            "cloudflare/creator-web/public/login.html": '<script src="/login.js"></script>',
            "cloudflare/creator-web/public/login.js": "// fixture",
            "cloudflare/creator-web/public/app.js": "import {x} from './pending.mjs';",
            "cloudflare/creator-web/public/pending.mjs": "export const x=1;",
            "cloudflare/creator-web/public/style.css": "body{}",
            "cloudflare/creator-web/public/_headers": "/*",
            "licenses/workers-runtime-sdk-1.9.4-MIT.txt": "fixture license",
        }
        for name, text in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, "utf-8")
        (web / "bundle-manifest.json").write_text(
            json.dumps(
                {
                    "files": [
                        {
                            "path": "workers/__init__.py",
                            "sha256": hashlib.sha256(b"# fixture").hexdigest(),
                        },
                        {
                            "path": "creator_ai.mjs",
                            "sha256": hashlib.sha256(b"// fixture").hexdigest(),
                        },
                    ]
                }
            ),
            "utf-8",
        )
        source = (
            Path(__file__).resolve().parents[1]
            / "cloudflare/creator-web/package_runtime.py"
        )
        (web / "package_runtime.py").write_bytes(source.read_bytes())
        return web

    def package(self, web):
        with patch("subprocess.check_output", return_value="a" * 40):
            runpy.run_path(str(web / "package_runtime.py"))

    def test_login_and_modules_present_in_exact_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            web = self.fixture(directory)
            self.package(web)
            with zipfile.ZipFile(web / "b-web-runtime.zip") as archive:
                manifest = json.loads(archive.read("runtime.json"))
                self.assertIn("worker/public/login.js", manifest["files"])
                self.assertIn("worker/public/pending.mjs", manifest["files"])
                self.assertIn("worker/src/migrations/001.sql", manifest["files"])
                self.assertEqual(
                    set(archive.namelist()), set(manifest["files"]) | {"runtime.json"}
                )

    def test_missing_required_or_referenced_file_fails_closed(self):
        for name in ("login.js", "pending.mjs"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                web = self.fixture(directory)
                (web / "public" / name).unlink()
                with self.assertRaises((AssertionError, FileNotFoundError)):
                    self.package(web)
        with tempfile.TemporaryDirectory() as directory:
            web = self.fixture(directory)
            (web / "public/login.html").write_text('<script src="/absent.js"></script>')
            with self.assertRaises(AssertionError):
                self.package(web)
