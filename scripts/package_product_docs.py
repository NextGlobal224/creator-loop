"""Add readable offline product guidance to a fresh onedir package, before ZIP."""

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

REPOSITORY = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    "USER_GUIDE.md",
    "LOCAL_COMPONENTS.md",
    "VIDEO_TRANSCRIPTION_PIPELINE.md",
    "TRANSCRIPT_EVIDENCE.md",
    "MAINTENANCE_UI.md",
    "MEDIA_CANCEL.md",
    "RUNTIME_RECOVERY.md",
    "UPDATE_BACKUP.md",
    "INSTALLATION_STAGING.md",
    "UPDATE_PREPARATION.md",
    "UPDATE_ACTIVATION.md",
    "RESTORE_ASSESSMENT.md",
    "RESTORE_APPLY.md",
    "UPDATE_RECOVERY.md",
    "UPDATE_METADATA.md",
    "DAMAGED_RESTORE_UI.md",
    "FRESH_COMPLETED_RESTORE.md",
    "CORRUPT_RESTORE_ASSESSMENT.md",
    "CORRUPT_RESTORE_INSPECTION.md",
    "CORRUPT_RESTORE_PREPARATION.md",
    "CORRUPT_RESTORE_COPY.md",
    "CORRUPT_COPY_RECOVERY.md",
    "Creator_Loop_Layout_Contract_V1.md",
)


def _real_path(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink() or item.is_junction():
            raise ValueError("Linked documentation/package path")


def _links(text: str, source: Path, output: Path, package: Path, commit: str) -> str:
    def replace(match: re.Match[str]) -> str:
        label, target = match.groups()
        target = target.strip("<>")
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
            return match.group(0)
        raw, _, fragment = target.partition("#")
        rel = (source.parent / raw).resolve().relative_to(REPOSITORY)
        if rel.as_posix() == "docs/HANDOFF.md" or rel.parts[0] == ".local-test-logs":
            destination = package / "build-info.json"
            fragment = ""
        elif rel.parent == Path("docs") and rel.name in DOCUMENTS:
            destination = (
                package / "USER_GUIDE.html"
                if rel.name == "USER_GUIDE.md"
                else package / "docs" / (rel.stem + ".html")
            )
        else:
            url = f"https://github.com/NextGlobal224/creator-loop/blob/{commit}/{quote(rel.as_posix())}"
            return f"[{label}]({url}{'#' + fragment if fragment else ''})"
        url = quote(Path(os.path.relpath(destination, output.parent)).as_posix())
        return f"[{label}]({url}{'#' + fragment if fragment else ''})"

    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", replace, text)


def package_docs(package: Path, commit: str) -> dict[str, object]:
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("Exact source commit is required")
    package = package.absolute()
    _real_path(package)
    for p in (package / "CreatorLoop.exe", package / "_internal"):
        _real_path(p)
    if (
        not (package / "CreatorLoop.exe").is_file()
        or not (package / "_internal").is_dir()
    ):
        raise ValueError("Existing onedir package is required")
    for p in (
        package / "docs",
        package / "USER_GUIDE.html",
        package / "build-info.json",
    ):
        if p.exists() or p.is_symlink() or p.is_junction():
            raise FileExistsError("Preserve existing guide/build evidence")
    inputs = {}
    for name in DOCUMENTS:
        source = REPOSITORY / "docs" / name
        _real_path(source)
        if not 0 < source.stat().st_size <= 1024 * 1024:
            raise ValueError("Product document exceeds read budget")
        inputs[name] = source.read_text(encoding="utf-8")
    # Build-time conversion only; the resulting files need no Python/Qt/browser service.
    from PySide6.QtGui import QFont, QTextDocument
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    (package / "docs").mkdir()
    for name, text in inputs.items():
        output = (
            package / "USER_GUIDE.html"
            if name == "USER_GUIDE.md"
            else package / "docs" / (Path(name).stem + ".html")
        )
        document = QTextDocument()
        document.setDefaultFont(QFont("Segoe UI", 11))
        document.setMarkdown(
            _links(text, REPOSITORY / "docs" / name, output, package, commit)
        )
        html = document.toHtml().replace(
            "</head>",
            "<style>body {max-width:64rem;margin:2rem auto;padding:0 1rem;line-height:1.5;} pre {overflow:auto;} table {border-collapse:collapse;} td,th {padding:0.3rem;}</style></head>",
        )
        with output.open("x", encoding="utf-8") as f:
            f.write(html)
    info = {
        "git_commit": commit,
        "target": "win-x64",
        "kind": "development-checkpoint",
        "user_guide": "USER_GUIDE.html",
        "product_documents": len(inputs),
        "verification": "Build identity only; consult the companion manifest/checksum and validation evidence. Not release or engine/model/hardware acceptance.",
    }
    with (package / "build-info.json").open("x", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=True, indent=2)
    app.processEvents()
    return info


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    args = parser.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    print(json.dumps(package_docs(args.package, args.commit), ensure_ascii=True))
