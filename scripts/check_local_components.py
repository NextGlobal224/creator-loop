"""Exact candidate artifact preflight using fake, external, unowned files."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from creator_loop.owned_process import OwnedWindowsProcess


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--source", action="store_true")
    args = parser.parse_args()
    args.work_root.mkdir()  # caller selects a new private fixture folder
    root = args.work_root.resolve(strict=True)
    external = root / "unowned selected model 非配布.fixture"
    content = b"FAKE_COMPONENT_PRIVATE_PAYLOAD_NEVER_LOG_OR_EXECUTE\x00\xff"
    external.write_bytes(content)
    user_root = root / "user-data-keep"
    user_root.mkdir()
    database = user_root / "creator_loop.sqlite3"
    database.write_bytes(b"NOT A DB: preflight must not open or initialize this")
    registry = user_root / "storage-roots.json"
    registry.write_bytes(b"KEEP COMPONENT OWNERSHIP AND ROOT IDS UNCHANGED")
    before = {p.name: p.read_bytes() for p in user_root.iterdir()}
    environment = {**os.environ, "CREATOR_LOOP_DATA_ROOT": str(user_root)}
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "app")
    prefix = ["-m", "creator_loop"] if args.source else []
    entry = {
        "component_id": "fake-model",
        "kind": "MODEL",
        "path": str(external),
        "version": "fake-v1",
        "sha256": hashlib.sha256(content).hexdigest(),
        "byte_size": len(content),
        "source_url": "https://example.invalid/fixture",
        "license": "FAKE ONLY: not real component license evidence",
        "license_url": "https://example.invalid/license",
        "local_use_allowed": True,
        "owned": False,
        "worker_memory_bytes": 512 * 1024**2,
    }
    for name, change, expected in (
        ("valid", {}, 0),
        ("digest-mismatch", {"sha256": "0" * 64}, 1),
        ("ownership-refused", {"owned": True}, 1),
    ):
        request = root / f"{name}.json"
        request.write_text(
            json.dumps({"manifest_version": 1, "components": [{**entry, **change}]}),
            encoding="utf-8",
        )
        logs = root / f"{name}-logs"
        with OwnedWindowsProcess(
            args.executable,
            [*prefix, "--check-components", str(request)],
            logs,
            component_version="fake-component-preflight-v1",
            environment=environment,
            memory_limit_bytes=512 * 1024**2,
        ) as process:
            outcome = process.wait(15)
        if outcome.timed_out or outcome.exit_code != expected:
            raise RuntimeError(f"Exact component check {name} failed: {outcome}")
        raw = (logs / "stdout.log").read_bytes()
        if not 0 < len(raw) <= 16 * 1024:
            raise RuntimeError("Component summary exceeded metadata output budget")
        payload = json.loads(raw)
        if expected == 0:
            if (
                payload["check"] != "LOCAL_ARTIFACTS_VERIFIED"
                or payload["runtime_compatibility_verified"] is not False
                or payload["components"][0]["owned"] is not False
                or payload["components"][0]["sha256"] != entry["sha256"]
            ):
                raise RuntimeError(
                    "Exact candidate claimed execution, ownership or wrong artifact"
                )
        elif payload["check"] != "FAILED":
            raise RuntimeError("Exact candidate accepted invalid component")
        if content in raw + (logs / "stderr.log").read_bytes():
            raise RuntimeError("Component payload leaked to logs")
        if external.read_bytes() != content:
            raise RuntimeError("Exact candidate changed unowned component")
        if before != {p.name: p.read_bytes() for p in user_root.iterdir()}:
            raise RuntimeError("Exact component preflight changed user data")
        print(
            json.dumps(
                {
                    "case": name,
                    "exit_code": outcome.exit_code,
                    "elapsed_seconds": outcome.elapsed_seconds,
                }
            )
        )
    print("Exact local component artifact checks PASS; engine/model runtime unverified")


if __name__ == "__main__":
    main()
