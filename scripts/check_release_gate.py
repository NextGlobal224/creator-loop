"""Reject development bytes before publication; never substitute for V1 tests."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creator_loop.installation_stage import (
    MAX_EXPANDED_BYTES,
    MAX_MANIFEST_BYTES,
    load_release_manifest,
)

from scripts.package_product_docs import DOCUMENTS


def _accepted_prerequisites(path: Path) -> None:
    rows: dict[int, tuple[str, str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| Gate "):
            continue
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) != 8:
            raise ValueError("Malformed product acceptance row")
        number = re.match(r"^(\d{2})\s+—", cells[2])
        if number is None or int(number[1]) in rows:
            raise ValueError("Missing or duplicate product acceptance ID")
        rows[int(number[1])] = (cells[3], cells[4], cells[5])
    if set(rows) != set(range(1, 25)):
        raise ValueError("All 24 product requirements must remain represented")
    # Requirement24 includes publication itself. Its preceding guide/build
    # prerequisites are checked against the artifact below; do not mark that
    # row accepted before the release actually exists.
    incomplete = [
        i
        for i in range(1, 24)
        if rows[i][0] != "ĐÃ NGHIỆM THU"
        or not rows[i][1]
        or rows[i][2] not in ("", "—")
    ]
    if incomplete:
        raise ValueError(
            "V1 prerequisites are not accepted: " + ",".join(map(str, incomplete))
        )


def check_release_gate(
    archive: Path,
    manifest_path: Path,
    acceptance: Path,
    *,
    expected_commit: str,
    expected_run_url: str,
    tag: str,
) -> None:
    manifest = load_release_manifest(manifest_path)
    if (
        manifest["git_commit"] != expected_commit
        or tag != "v" + manifest["app_version"]
    ):
        raise ValueError("Tag/commit does not match the tested artifact manifest")
    compatibility = manifest["component_compatibility"]
    if compatibility.get("status") != "VERIFIED_V1":
        raise ValueError("Engine/model compatibility is unresolved or unverified")
    for kind in ("engine", "model"):
        component = compatibility.get(kind)
        if not isinstance(component, dict) or any(
            not isinstance(component.get(key), str) or not component[key].strip()
            for key in ("name", "version", "source", "license")
        ):
            raise ValueError("Verified component identity/source/license is required")
        if not re.fullmatch(r"[0-9a-f]{64}", str(component.get("sha256", ""))):
            raise ValueError("Verified component digest is required")
        if component.get("local_use_allowed") is not True:
            raise ValueError("Component local-use permission is unverified")
    notes = (manifest["release_notes"] + " " + manifest["recovery_notes"]).casefold()
    if (
        "development checkpoint" in notes
        or "gate remains incomplete" in notes
        or "validation remains required" in notes
    ):
        raise ValueError("Development/incomplete release notes cannot be published")
    provenance = manifest["provenance"]
    if (
        provenance.get("repository") != "NextGlobal224/creator-loop"
        or provenance.get("ci_run") != expected_run_url
        or not re.fullmatch(
            r"https://github\.com/NextGlobal224/creator-loop/actions/runs/[0-9]+",
            str(provenance.get("ci_run", "")),
        )
        or not str(provenance.get("method", "")).startswith(
            "GitHub Actions Windows build;"
        )
    ):
        raise ValueError("A matching Windows CI build provenance is required")
    _accepted_prerequisites(acceptance)
    files = manifest["files"]
    guidance = {
        "CreatorLoop/USER_GUIDE.html"
        if name == "USER_GUIDE.md"
        else "CreatorLoop/docs/" + Path(name).stem + ".html"
        for name in DOCUMENTS
    }
    if (
        not {"CreatorLoop/CreatorLoop.exe", "CreatorLoop/build-info.json", *guidance}
        <= files.keys()
    ):
        raise ValueError(
            "The tested artifact must include launcher and offline guide identity"
        )
    with archive.open("rb") as stream:
        if (
            hashlib.file_digest(stream, "sha256").hexdigest()
            != manifest["artifact_sha256"]
        ):
            raise ValueError("Published ZIP differs from the tested manifest")
    with zipfile.ZipFile(archive) as package:
        members = [m for m in package.infolist() if not m.is_dir()]
        if len(members) != len(files) or {m.filename for m in members} != set(files):
            raise ValueError("Published ZIP inventory differs from the tested manifest")
        if sum(m.file_size for m in members) > MAX_EXPANDED_BYTES:
            raise ValueError("Published ZIP exceeds its expanded budget")
        for member in members:
            expected = files[member.filename]
            with package.open(member) as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if member.file_size != expected["size"] or digest != expected["sha256"]:
                raise ValueError("Published ZIP member differs from tested bytes")
        if files["CreatorLoop/build-info.json"]["size"] > MAX_MANIFEST_BYTES:
            raise ValueError("Offline guide identity exceeds metadata budget")
        identity = json.loads(package.read("CreatorLoop/build-info.json"))
        if (
            identity.get("git_commit") != expected_commit
            or identity.get("target") != "win-x64"
        ):
            raise ValueError("Offline guide identity does not match the tested build")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--acceptance", type=Path, default=Path("docs/PRODUCT_ACCEPTANCE.md")
    )
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-run-url", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    try:
        check_release_gate(
            args.artifact,
            args.manifest,
            args.acceptance,
            expected_commit=args.expected_commit,
            expected_run_url=args.expected_run_url,
            tag=args.tag,
        )
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print("Release gate refused: " + str(exc), file=sys.stderr)
        return 2
    print(
        "Release preconditions/byte binding PASS; publication and human V1 evidence remain separately accountable"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
