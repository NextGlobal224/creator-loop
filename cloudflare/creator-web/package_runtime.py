"""Create a CI-origin immutable runtime archive from explicit build outputs."""

import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path

web = Path(__file__).resolve().parent
repo = web.parents[1]
commit = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=repo, text=True, timeout=15
).strip()
files = {}
bundle = json.loads((web / "bundle-manifest.json").read_text("utf-8"))
for item in bundle["files"]:
    relative = item["path"]
    source = (
        web / ("src" if relative == "creator_ai.mjs" else "python_modules") / relative
    )
    raw = source.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == item["sha256"]
    files["worker/" + source.relative_to(web).as_posix()] = raw
for source in [
    web / "wrangler.jsonc",
    web / "bundle-manifest.json",
    web / "src/entry.py",
    *sorted((web / "src/migrations").glob("*.sql")),
    *(
        web / "public" / name
        for name in (
            "index.html",
            "login.html",
            "login.js",
            "style.css",
            "app.js",
            "pending.mjs",
            "operator-grant.mjs",
            "_headers",
        )
    ),
]:
    assert source.is_file(), f"Missing required runtime file: {source.name}"
    name = source.relative_to(web).as_posix()
    if any(part.startswith(".") for part in Path(name).parts) or source.suffix in (
        ".pyc",
        ".log",
    ):
        continue
    files["worker/" + name] = source.read_bytes()
for source in sorted((web.parent / "creator-ai/licenses").glob("*.txt")):
    files["worker/licenses/" + source.name] = source.read_bytes()
source = repo / "licenses/workers-runtime-sdk-1.9.4-MIT.txt"
files["worker/licenses/" + source.name] = source.read_bytes()
assert "worker/src/entry.py" in files and "worker/src/creator_ai.mjs" in files
assert "worker/python_modules/workers/__init__.py" in files
for name, raw in files.items():
    if name.endswith(".html"):
        for dependency in re.findall(r'(?:src|href)="/([^"?#]+)"', raw.decode()):
            if dependency.startswith("api/"):
                continue
            assert "worker/public/" + dependency in files, (
                f"Missing page dependency: {dependency}"
            )
    if name.startswith("worker/public/") and name.endswith((".js", ".mjs")):
        for dependency in re.findall(r"from\s*['\"]\./([^'\"]+)['\"]", raw.decode()):
            assert "worker/public/" + dependency in files, (
                f"Missing module dependency: {dependency}"
            )
expected_migrations = sorted((repo / "migrations").glob("*.sql"))
assert expected_migrations
for source in expected_migrations:
    assert files["worker/src/migrations/" + source.name] == source.read_bytes()
manifest = {
    "format": "B_WEB_CI_RUNTIME_V1",
    "commit": commit,
    "files": {
        name: hashlib.sha256(raw).hexdigest() for name, raw in sorted(files.items())
    },
}
output = web / "b-web-runtime.zip"
with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for name, raw in sorted(files.items()):
        info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, raw)
    archive.writestr("runtime.json", json.dumps(manifest, sort_keys=True))
print("CI runtime", commit, len(files), hashlib.sha256(output.read_bytes()).hexdigest())
