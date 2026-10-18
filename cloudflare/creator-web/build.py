"""Assemble first-party Python and the exact pinned SDK; generated files ignored."""

import argparse
import base64
import csv
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent
repo = root.parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--sdk-dir", type=Path, default=root / ".build-sdk")
args = parser.parse_args()
sdk = args.sdk_dir.resolve(strict=True)
metadata = sdk / "workers_runtime_sdk-1.9.4.dist-info"
assert "Version: 1.9.4" in (metadata / "METADATA").read_text("utf-8")
target = root / "python_modules"
target.mkdir(exist_ok=True)
files = []


def put(relative, raw):
    dest = target / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    files.append(
        {
            "path": str(relative).replace("\\", "/"),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    )


for name, checksum, _ in csv.reader((metadata / "RECORD").read_text().splitlines()):
    if not checksum:
        continue
    raw = (sdk / name).read_bytes()
    assert checksum == "sha256=" + base64.urlsafe_b64encode(
        hashlib.sha256(raw).digest()
    ).decode().rstrip("=")
    put(name, raw)
for source in sorted((repo / "app/creator_loop").rglob("*.py")):
    put(source.relative_to(repo / "app"), source.read_bytes())
for source in sorted((repo / "migrations").glob("*.sql")):
    destination = root / "src/migrations" / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
script = "require('esbuild').buildSync({entryPoints:[process.argv[1]],outfile:process.argv[2],bundle:true,format:'esm',platform:'browser',target:'es2022',minify:true,logLevel:'warning'})"
subprocess.run(
    [
        "node",
        "-e",
        script,
        str(root / "src/ai-bridge.js"),
        str(root / "src/creator_ai.mjs"),
    ],
    cwd=root.parent / "creator-ai",
    check=True,
    timeout=120,
)
files.append(
    {
        "path": "creator_ai.mjs",
        "sha256": hashlib.sha256(
            (root / "src/creator_ai.mjs").read_bytes()
        ).hexdigest(),
    }
)
(root / "bundle-manifest.json").write_text(
    json.dumps(
        {
            "sdk": "workers-runtime-sdk@1.9.4",
            "ai_sdk": "workers-ai-provider@4.0.0/ai@7.0.142",
            "files": files,
        },
        indent=2,
    ),
    "utf-8",
)
print(f"Assembled {len(files)} verified source/SDK files; no credentials or user data")
