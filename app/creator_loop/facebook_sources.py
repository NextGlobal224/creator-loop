# Atomic Source + real TEXT snapshot, never media download.
from __future__ import annotations

import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from creator_loop.database import _connect_write, open_readonly
from creator_loop.library import LibraryRepository, Source
from creator_loop.text_intake import ImportedOriginal, _intake_original
from creator_loop.windows_paths import file_io_path

HOSTS = frozenset(
    {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}
)
ID = r"(?:[0-9]+|pfbid[A-Za-z0-9]+)"


@dataclass(frozen=True)
class PostLink:
    url: str
    external_id: str


def post_link(value: object) -> PostLink | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        u = urlparse(value)
        if (
            u.scheme != "https"
            or u.hostname not in HOSTS
            or u.username
            or u.password
            or u.port
        ):
            return None
    except ValueError:
        return None
    path = u.path.rstrip("/")
    match = re.fullmatch(r"/([A-Za-z0-9._-]+)/(posts|videos)/(" + ID + r")", path)
    if match:
        return PostLink("https://www.facebook.com" + path, match[3])
    match = re.fullmatch(r"/groups/([0-9]+)/posts/(" + ID + r")", path)
    if match:
        return PostLink("https://www.facebook.com" + path, match[2])
    match = re.fullmatch(r"/reel/(" + ID + r")", path)
    if match:
        return PostLink("https://www.facebook.com" + path, match[1])
    q = parse_qs(u.query)
    if path == "/watch" and len(q.get("v", [])) == 1 and re.fullmatch(ID, q["v"][0]):
        return PostLink("https://www.facebook.com/watch/?v=" + q["v"][0], q["v"][0])
    if (
        path in ("/permalink.php", "/story.php")
        and len(q.get("story_fbid", [])) == len(q.get("id", [])) == 1
    ):
        post, owner = q["story_fbid"][0], q["id"][0]
        if re.fullmatch(ID, post) and re.fullmatch(r"[0-9]+", owner):
            return PostLink(
                "https://www.facebook.com/permalink.php?story_fbid="
                + post
                + "&id="
                + owner,
                post,
            )
    return None


class DuplicateCapture(Exception):
    pass


def saved_sources(root: Path) -> list[dict[str, object]]:
    with closing(open_readonly(root / "creator_loop.sqlite3")) as db:
        db.row_factory = sqlite3.Row
        return [
            dict(r)
            for r in db.execute("""SELECT s.source_id,s.canonical_url,s.publisher_name,s.external_id,
        c.keyword,c.captured_at,c.excerpt,c.snapshot_file_id FROM sources s JOIN source_captures c USING(source_id)
        ORDER BY c.captured_at DESC""")
        ]


def save_capture(
    root: Path,
    *,
    keyword: str,
    url: str,
    excerpt: str | None,
    publisher: str | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> tuple[str, bool]:
    if cancelled():
        raise InterruptedError("Facebook capture cancelled")
    link = post_link(url)
    keyword = unicodedata.normalize("NFC", keyword.strip())
    if link is None or not 1 <= len(keyword) <= 200:
        raise ValueError("Valid keyword and Facebook post permalink required")
    excerpt = unicodedata.normalize("NFC", excerpt.strip())[:8192] if excerpt else None
    excerpt = excerpt or None
    source_id = uuid4().hex
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def existing(db: sqlite3.Connection) -> str | None:
        row = db.execute(
            "SELECT s.source_id FROM sources s JOIN source_captures c USING(source_id) WHERE s.platform='FB' AND s.external_id=?",
            (link.external_id,),
        ).fetchone()
        return str(row[0]) if row else None

    with closing(open_readonly(root / "creator_loop.sqlite3")) as check:
        found = existing(check)
        if found:
            return found, False

    def register(db: sqlite3.Connection, imported: ImportedOriginal | None) -> None:
        nonlocal source_id
        if cancelled():
            raise InterruptedError("Facebook capture cancelled")
        if existing(db):
            raise DuplicateCapture()
        repo = LibraryRepository(db)
        prior = db.execute(
            "SELECT source_id FROM sources WHERE platform='FB' AND external_id=?",
            (link.external_id,),
        ).fetchone()
        if prior:
            source_id = str(prior[0])
        else:
            repo.create_source(
                Source(
                    source_id,
                    "FB",
                    link.url,
                    link.external_id,
                    (publisher or "").strip()[:200] or None,
                    None,
                    now,
                    "UNKNOWN",
                    now,
                )
            )
        if imported:
            repo.link_source_asset(
                source_id=source_id,
                asset_id=imported.asset_id,
                relationship_type="REFERENCE",
                verification_status="UNREVIEWED",
                recorded_at=now,
            )
        db.execute(
            "INSERT INTO source_captures VALUES (?,?,?,?,?)",
            (source_id, keyword, now, excerpt, imported.file_id if imported else None),
        )

    temp: Path | None = None
    try:
        if excerpt:
            temp = root / "runtime" / ("facebook-capture-" + uuid4().hex + ".txt")
            body = (
                "Facebook browser capture\nURL: "
                + link.url
                + "\nKeyword: "
                + keyword
                + "\nCaptured UTC: "
                + now
                + "\n\n"
                + excerpt
                + "\n"
            )
            with file_io_path(temp).open("xb") as f:
                f.write(body.encode("utf-8"))
            _intake_original(
                temp,
                root=root,
                classify=lambda _: ("TEXT", "text/plain"),
                register=lambda db, item: register(db, item),
                cancel=cancelled,
            )
        else:
            with closing(_connect_write(root / "creator_loop.sqlite3")) as db:
                try:
                    db.execute("BEGIN IMMEDIATE")
                    register(db, None)
                    if cancelled():
                        raise InterruptedError(
                            "Facebook capture cancelled before commit"
                        )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
    except (DuplicateCapture, sqlite3.IntegrityError):
        with closing(open_readonly(root / "creator_loop.sqlite3")) as check:
            found = existing(check)
            if found:
                return found, False
        raise
    finally:
        if temp is not None:
            file_io_path(temp).unlink(missing_ok=True)
    return source_id, True
