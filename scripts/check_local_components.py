"""Exact candidate artifact preflight using fake, external, unowned files."""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from creator_loop.database import SCHEMA_VERSION, initialize
from creator_loop.owned_process import OwnedWindowsProcess
from creator_loop.paths import ensure_data_root


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
    reviewed = None
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
            reviewed = payload["review_fingerprint"]
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
    selected_root = root / "selection-user-data"
    ensure_data_root(selected_root)
    source = selected_root / "creator_loop.sqlite3"
    initialize(source)
    with closing(sqlite3.connect(source)) as db:
        domain_before = list(db.iterdump())
    registry_path = selected_root / "manifests/storage-roots.json"
    registry_before = {
        "manifest_version": 1,
        "data_root_id": uuid4().hex,
        "schema_version": SCHEMA_VERSION,
        "storage_roots": [],
        "component_installations": [{"name": "KEEP UNOWNED", "owned": False}],
    }
    registry_path.write_text(json.dumps(registry_before), encoding="utf-8")
    selected_env = {**environment, "CREATOR_LOOP_DATA_ROOT": str(selected_root)}
    assert isinstance(reviewed, str)
    request = root / "valid.json"
    cases = [
        (
            "save-reviewed",
            ["--select-components", str(request), "--reviewed-components", reviewed],
            0,
        ),
        ("inspect", ["--inspect-components"], 0),
        ("ui", ["--components", "--ui-smoke"], 0),
        (
            "refuse-stale",
            ["--select-components", str(request), "--reviewed-components", "0" * 64],
            1,
        ),
    ]
    saved = None
    for name, command, expected in cases:
        logs = root / f"{name}-logs"
        with OwnedWindowsProcess(
            args.executable,
            [*prefix, *command],
            logs,
            component_version="fake-component-selection-v1",
            environment=selected_env,
            memory_limit_bytes=512 * 1024**2,
        ) as process:
            outcome = process.wait(15)
        if outcome.timed_out or outcome.exit_code != expected:
            raise RuntimeError(f"Exact selection {name} failed: {outcome}")
        if name != "ui":
            body = json.loads((logs / "stdout.log").read_bytes())
            if name == "save-reviewed" and (
                body["selection"] != "SAVED"
                or body["runtime_compatibility_verified"] is not False
            ):
                raise RuntimeError("Candidate did not save artifact-only selection")
            if name == "inspect" and (
                body["selection"] != "HISTORY" or body["freshly_verified"] is not False
            ):
                raise RuntimeError(
                    "Candidate treated historical selection as fresh runtime proof"
                )
            if name == "refuse-stale" and body["selection"] != "FAILED":
                raise RuntimeError("Candidate accepted stale review")
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
        if {
            k: v for k, v in payload.items() if k != "component_selection"
        } != registry_before:
            raise RuntimeError("Candidate changed root/ownership registry")
        if saved is not None and payload != saved:
            raise RuntimeError("Inspect/UI/stale rejection changed saved selection")
        saved = payload
        if external.read_bytes() != content:
            raise RuntimeError("Candidate changed unowned model bytes")
        with closing(sqlite3.connect(source)) as db:
            if list(db.iterdump()) != domain_before:
                raise RuntimeError("Candidate changed domain DB")
        print(
            json.dumps(
                {
                    "case": name,
                    "exit_code": outcome.exit_code,
                    "elapsed_seconds": outcome.elapsed_seconds,
                }
            )
        )
    print(
        "Exact local component checks/selection/history/UI smoke PASS; engine/model runtime unverified"
    )


if __name__ == "__main__":
    main()
