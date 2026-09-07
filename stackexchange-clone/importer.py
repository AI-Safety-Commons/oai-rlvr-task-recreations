#!/usr/bin/env python3
"""Import one, many, or all Stack Exchange data-dump site archives."""

from __future__ import annotations

import argparse
import html
import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from app import DATA_DIR, SCHEMA, connect, now


def content_license(item: dict[str, str]) -> str:
    """Return the dump licence, falling back to Stack Exchange's date rules."""
    declared = item.get("ContentLicense", "").strip().upper().replace("CC BY SA", "CC BY-SA")
    if declared in {"CC BY-SA 2.5", "CC BY-SA 3.0", "CC BY-SA 4.0"}:
        return declared
    created = item.get("CreationDate", "")
    if created < "2011-04-08":
        return "CC BY-SA 2.5"
    if created < "2018-05-02":
        return "CC BY-SA 3.0"
    return "CC BY-SA 4.0"


def rows(path: Path):
    if not path.exists():
        return
    for _event, element in ET.iterparse(path, events=("end",)):
        if element.tag == "row":
            yield element.attrib
            element.clear()


def integer(value: str | None, default: int = 0) -> int:
    try:
        return int(value or default)
    except ValueError:
        return default


def display_name(slug: str) -> str:
    stem = slug.removesuffix(".stackexchange.com")
    return (
        "Stack Overflow"
        if slug == "stackoverflow.com"
        else stem.replace("-", " ").replace(".", " ").title()
    )


def import_site(
    folder: Path, slug: str, source: str = "", max_posts: int = 0
) -> dict[str, int]:
    counts = {"users": 0, "posts": 0, "comments": 0}
    with connect() as connection:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO sites(slug,name,source_file,imported_at) VALUES(?,?,?,?) ON CONFLICT(slug) DO UPDATE SET source_file=excluded.source_file,imported_at=excluded.imported_at",
            (slug, display_name(slug), source, now()),
        )
        site_id = connection.execute(
            "SELECT id FROM sites WHERE slug=?", (slug,)
        ).fetchone()[0]
        for item in rows(folder / "Users.xml") or ():
            connection.execute(
                "INSERT OR REPLACE INTO historical_users(site_id,external_id,display_name,reputation) VALUES(?,?,?,?)",
                (
                    site_id,
                    integer(item.get("Id")),
                    item.get("DisplayName", "community"),
                    integer(item.get("Reputation")),
                ),
            )
            counts["users"] += 1
        external_to_local: dict[int, int] = {}
        deferred: list[dict[str, str]] = []
        for item in rows(folder / "Posts.xml") or ():
            if max_posts and counts["posts"] >= max_posts:
                break
            kind = integer(item.get("PostTypeId"))
            if kind not in (1, 2):
                continue
            if kind == 2 and integer(item.get("ParentId")) not in external_to_local:
                deferred.append(item.copy())
                continue
            owner_id = integer(item.get("OwnerUserId")) or None
            owner = item.get("OwnerDisplayName")
            if not owner and owner_id:
                found = connection.execute(
                    "SELECT display_name FROM historical_users WHERE site_id=? AND external_id=?",
                    (site_id, owner_id),
                ).fetchone()
                owner = found[0] if found else None
            connection.execute(
                "INSERT OR IGNORE INTO posts(site_id,external_id,post_type,parent_id,owner_external_id,owner_name,title,body,score,accepted_answer_external_id,created_at,updated_at,content_license) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    site_id,
                    integer(item.get("Id")),
                    "question" if kind == 1 else "answer",
                    external_to_local.get(integer(item.get("ParentId"))),
                    owner_id,
                    owner,
                    item.get("Title"),
                    item.get("Body", ""),
                    integer(item.get("Score")),
                    integer(item.get("AcceptedAnswerId")) or None,
                    item.get("CreationDate", now()),
                    item.get("LastActivityDate", item.get("CreationDate", now())),
                    content_license(item),
                ),
            )
            local = connection.execute(
                "SELECT id FROM posts WHERE site_id=? AND external_id=?",
                (site_id, integer(item.get("Id"))),
            ).fetchone()
            if local:
                external_to_local[integer(item.get("Id"))] = local[0]
                if kind == 1:
                    for tag in __import__("re").findall(
                        r"<([^<>]+)>", html.unescape(item.get("Tags", ""))
                    ):
                        connection.execute(
                            "INSERT OR IGNORE INTO tags(site_id,name) VALUES(?,?)",
                            (site_id, tag.lower()),
                        )
                        tag_id = connection.execute(
                            "SELECT id FROM tags WHERE site_id=? AND name=?",
                            (site_id, tag.lower()),
                        ).fetchone()[0]
                        connection.execute(
                            "INSERT OR IGNORE INTO post_tags(post_id,tag_id) VALUES(?,?)",
                            (local[0], tag_id),
                        )
                counts["posts"] += 1
        for item in deferred:
            parent = external_to_local.get(integer(item.get("ParentId")))
            if not parent or (max_posts and counts["posts"] >= max_posts):
                continue
            connection.execute(
                "INSERT OR IGNORE INTO posts(site_id,external_id,post_type,parent_id,owner_external_id,owner_name,body,score,created_at,updated_at,content_license) VALUES(?,?,'answer',?,?,?,?,?,?,?,?)",
                (
                    site_id,
                    integer(item.get("Id")),
                    parent,
                    integer(item.get("OwnerUserId")) or None,
                    item.get("OwnerDisplayName"),
                    item.get("Body", ""),
                    integer(item.get("Score")),
                    item.get("CreationDate", now()),
                    item.get("LastActivityDate", item.get("CreationDate", now())),
                    content_license(item),
                ),
            )
            counts["posts"] += 1
        for item in rows(folder / "Comments.xml") or ():
            post_id = external_to_local.get(integer(item.get("PostId")))
            if not post_id:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO comments(post_id,owner_name,body,score,external_id,created_at,content_license) VALUES(?,?,?,?,?,?,?)",
                (
                    post_id,
                    item.get("UserDisplayName"),
                    item.get("Text", ""),
                    integer(item.get("Score")),
                    integer(item.get("Id")),
                    item.get("CreationDate", now()),
                    content_license(item),
                ),
            )
            counts["comments"] += 1
        connection.execute(
            "UPDATE tags SET count=(SELECT COUNT(*) FROM post_tags WHERE tag_id=tags.id) WHERE site_id=?",
            (site_id,),
        )
    return counts


def slug_for(path: Path) -> str:
    name = path.name.removesuffix(".7z")
    return name.lower().replace("_", ".")


def import_path(path: Path, max_posts: int = 0) -> dict[str, dict[str, int]]:
    results = {}
    extraction_root = Path(os.environ.get("STACK_IMPORT_TMP", DATA_DIR / ".import-tmp"))
    extraction_root.mkdir(parents=True, exist_ok=True)
    candidates = (
        sorted(path.glob("*.7z"))
        if path.is_dir() and not (path / "Posts.xml").exists()
        else [path]
    )
    for candidate in candidates:
        slug = slug_for(candidate)
        if candidate.is_dir():
            results[slug] = import_site(candidate, slug, str(candidate), max_posts)
            continue
        with tempfile.TemporaryDirectory(
            prefix="stack-import-", dir=extraction_root
        ) as temporary:
            import py7zr

            with py7zr.SevenZipFile(candidate) as archive:
                archive.extract(
                    path=temporary, targets=["Users.xml", "Posts.xml", "Comments.xml"]
                )
            results[slug] = import_site(
                Path(temporary), slug, str(candidate), max_posts
            )
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        type=Path,
        help="extracted site folder, .7z, or directory of every site .7z",
    )
    parser.add_argument(
        "--max-posts",
        type=int,
        default=0,
        help="development cap per site (0 means unlimited)",
    )
    args = parser.parse_args()
    for site, counts in import_path(args.path, args.max_posts).items():
        print(site, counts)
