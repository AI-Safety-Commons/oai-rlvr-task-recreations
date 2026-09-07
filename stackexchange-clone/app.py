#!/usr/bin/env python3
"""A deliberately cheap, server-rendered Stack Exchange network clone."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import Lock

from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("STACK_DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("STACK_DB", DATA_DIR / "stackexchange.db"))
EXPORT_PATH = Path(os.environ.get("STACK_EXPORT", DATA_DIR / "agent-activity.json"))
DEFAULT_SITE = os.environ.get("STACK_DEFAULT_SITE", "stackoverflow.com")
TAG_RE = re.compile(r"<([^<>]+)>")
EXPORT_LOCK = Lock()
LOCAL_CONTENT_LICENSE = "CC BY-SA 4.0"
LICENSE_URLS = {
    "CC BY-SA 2.5": "https://creativecommons.org/licenses/by-sa/2.5/",
    "CC BY-SA 3.0": "https://creativecommons.org/licenses/by-sa/3.0/",
    "CC BY-SA 4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
}

app = Flask(
    __name__,
    template_folder=str(APP_DIR / "templates"),
    static_folder=str(APP_DIR / "static"),
)
app.secret_key = os.environ.get("STACK_SECRET", "cheap-local-development-secret")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def db() -> sqlite3.Connection:
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = connect()
    return g.db


@app.teardown_appcontext
def close_db(_error: object = None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  source_file TEXT, imported_at TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL,
  password_hash TEXT NOT NULL, api_token TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS historical_users (
  site_id INTEGER NOT NULL REFERENCES sites(id), external_id INTEGER NOT NULL,
  display_name TEXT NOT NULL, reputation INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(site_id, external_id)
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY, site_id INTEGER NOT NULL REFERENCES sites(id),
  external_id INTEGER, post_type TEXT NOT NULL CHECK(post_type IN ('question','answer')),
  parent_id INTEGER REFERENCES posts(id), user_id INTEGER REFERENCES users(id),
  owner_external_id INTEGER, owner_name TEXT, title TEXT, body TEXT NOT NULL,
  score INTEGER NOT NULL DEFAULT 0, accepted_answer_external_id INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  content_license TEXT NOT NULL DEFAULT 'CC BY-SA 4.0',
  UNIQUE(site_id, external_id)
);
CREATE INDEX IF NOT EXISTS posts_site_new ON posts(site_id, id DESC);
CREATE INDEX IF NOT EXISTS posts_parent ON posts(parent_id, id);
CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY, site_id INTEGER NOT NULL REFERENCES sites(id),
  name TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, UNIQUE(site_id, name)
);
CREATE TABLE IF NOT EXISTS post_tags (
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tags(id), PRIMARY KEY(post_id, tag_id)
);
CREATE TABLE IF NOT EXISTS votes (
  post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id), value INTEGER NOT NULL CHECK(value IN (-1,1)),
  created_at TEXT NOT NULL, PRIMARY KEY(post_id, user_id)
);
CREATE TABLE IF NOT EXISTS comments (
  id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
  user_id INTEGER REFERENCES users(id), owner_name TEXT, body TEXT NOT NULL,
  score INTEGER NOT NULL DEFAULT 0, external_id INTEGER, created_at TEXT NOT NULL,
  content_license TEXT NOT NULL DEFAULT 'CC BY-SA 4.0'
);
CREATE UNIQUE INDEX IF NOT EXISTS comments_external
  ON comments(post_id, external_id) WHERE external_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, site_slug TEXT NOT NULL,
  actor TEXT NOT NULL, object_type TEXT NOT NULL, object_id INTEGER NOT NULL,
  summary TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.executescript(SCHEMA)
        post_columns = {row[1] for row in connection.execute("PRAGMA table_info(posts)")}
        if "content_license" not in post_columns:
            connection.execute(
                "ALTER TABLE posts ADD COLUMN content_license TEXT NOT NULL DEFAULT 'CC BY-SA 4.0'"
            )
        comment_columns = {row[1] for row in connection.execute("PRAGMA table_info(comments)")}
        if "content_license" not in comment_columns:
            connection.execute(
                "ALTER TABLE comments ADD COLUMN content_license TEXT NOT NULL DEFAULT 'CC BY-SA 4.0'"
            )
        connection.execute(
            "INSERT OR IGNORE INTO sites(slug,name) VALUES(?,?)",
            (
                DEFAULT_SITE,
                "Stack Overflow"
                if DEFAULT_SITE == "stackoverflow.com"
                else DEFAULT_SITE,
            ),
        )
    export_activity()


def license_url(name: str) -> str:
    return LICENSE_URLS.get(name, LICENSE_URLS[LOCAL_CONTENT_LICENSE])


def source_url(item: sqlite3.Row | dict) -> str | None:
    external_id = item["external_id"]
    if not external_id:
        return None
    kind = item["post_type"]
    path = "questions" if kind == "question" else "a"
    return f"https://{item['site_slug']}/{path}/{external_id}"


def attributed_post(item: sqlite3.Row) -> dict:
    result = dict(item)
    result["source_url"] = source_url(result)
    result["license_url"] = license_url(result["content_license"])
    result["modified"] = bool(result["external_id"])
    return result


app.jinja_env.globals.update(license_url=license_url, source_url=source_url)


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return f"{salt.hex()}:{digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split(":", 1)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), 120_000
        ).hex()
        return hmac.compare_digest(actual, expected)
    except ValueError:
        return False


def export_activity() -> None:
    """Atomically mirror agent mutations into the bind-mounted host directory."""
    if not DB_PATH.exists():
        return
    with EXPORT_LOCK:
        with connect() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY id").fetchall()
        payload = [dict(row) for row in rows]
        EXPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = EXPORT_PATH.with_suffix(EXPORT_PATH.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        temporary.replace(EXPORT_PATH)


def record_event(
    connection: sqlite3.Connection,
    kind: str,
    site: str,
    actor: str,
    object_type: str,
    object_id: int,
    summary: str,
) -> None:
    connection.execute(
        "INSERT INTO events(kind,site_slug,actor,object_type,object_id,summary,created_at) VALUES(?,?,?,?,?,?,?)",
        (kind, site, actor, object_type, object_id, summary[:500], now()),
    )


@app.before_request
def load_user() -> None:
    user_id = session.get("user_id")
    token = request.headers.get("Authorization", "")
    if token.lower().startswith("bearer "):
        g.user = (
            db()
            .execute("SELECT * FROM users WHERE api_token=?", (token[7:].strip(),))
            .fetchone()
        )
    elif user_id:
        g.user = db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    else:
        g.user = None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Bearer token required"), 401
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def site_or_404(slug: str) -> sqlite3.Row:
    row = db().execute("SELECT * FROM sites WHERE slug=?", (slug,)).fetchone()
    if row is None:
        abort(404)
    return row


def post_or_404(post_id: int) -> sqlite3.Row:
    row = (
        db()
        .execute(
            "SELECT p.*,s.slug site_slug,s.name site_name,COALESCE(u.username,p.owner_name,'community') author "
            "FROM posts p JOIN sites s ON s.id=p.site_id LEFT JOIN users u ON u.id=p.user_id WHERE p.id=?",
            (post_id,),
        )
        .fetchone()
    )
    if row is None:
        abort(404)
    return row


def post_tags(post_id: int) -> list[str]:
    return [
        r["name"]
        for r in db().execute(
            "SELECT t.name FROM tags t JOIN post_tags pt ON pt.tag_id=t.id WHERE pt.post_id=? ORDER BY t.name",
            (post_id,),
        )
    ]


def add_tags(
    connection: sqlite3.Connection, site_id: int, post_id: int, raw: str | list[str]
) -> None:
    names = (
        raw
        if isinstance(raw, list)
        else ([m.group(1) for m in TAG_RE.finditer(raw)] or raw.split())
    )
    for name in dict.fromkeys(
        str(x).strip().lower()[:35] for x in names if str(x).strip()
    ):
        connection.execute(
            "INSERT OR IGNORE INTO tags(site_id,name) VALUES(?,?)", (site_id, name)
        )
        tag_id = connection.execute(
            "SELECT id FROM tags WHERE site_id=? AND name=?", (site_id, name)
        ).fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO post_tags(post_id,tag_id) VALUES(?,?)",
            (post_id, tag_id),
        )
        connection.execute(
            "UPDATE tags SET count=(SELECT COUNT(*) FROM post_tags WHERE tag_id=?) WHERE id=?",
            (tag_id, tag_id),
        )


@app.route("/")
def index():
    sites = (
        db()
        .execute(
            "SELECT s.*,COUNT(p.id) question_count FROM sites s LEFT JOIN posts p ON p.site_id=s.id AND p.post_type='question' GROUP BY s.id ORDER BY question_count DESC,s.slug"
        )
        .fetchall()
    )
    return render_template("sites.html", sites=sites, title="All sites")


@app.route("/s/<slug>")
def questions(slug: str):
    site = site_or_404(slug)
    tag = request.args.get("tag", "").strip().lower()
    search = request.args.get("q", "").strip()
    sql = "SELECT p.*,COALESCE(u.username,p.owner_name,'community') author,(SELECT COUNT(*) FROM posts a WHERE a.parent_id=p.id) answer_count FROM posts p LEFT JOIN users u ON u.id=p.user_id WHERE p.site_id=? AND p.post_type='question'"
    args: list[object] = [site["id"]]
    if tag:
        sql += " AND EXISTS(SELECT 1 FROM post_tags pt JOIN tags t ON t.id=pt.tag_id WHERE pt.post_id=p.id AND t.name=?)"
        args.append(tag)
    if search:
        sql += " AND (p.title LIKE ? OR p.body LIKE ?)"
        args.extend([f"%{search}%", f"%{search}%"])
    sql += " ORDER BY p.id DESC LIMIT 100"
    rows = db().execute(sql, args).fetchall()
    items = [(row, post_tags(row["id"])) for row in rows]
    return render_template(
        "questions.html",
        site=site,
        items=items,
        tag=tag,
        search=search,
        title=site["name"],
    )


@app.route("/q/<int:post_id>")
def post(post_id: int):
    question = post_or_404(post_id)
    if question["post_type"] != "question":
        return redirect(url_for("post", post_id=question["parent_id"]))
    answers = (
        db()
        .execute(
            "SELECT p.*,COALESCE(u.username,p.owner_name,'community') author FROM posts p LEFT JOIN users u ON u.id=p.user_id WHERE p.parent_id=? ORDER BY p.score DESC,p.id",
            (post_id,),
        )
        .fetchall()
    )
    ids = [post_id, *[r["id"] for r in answers]]
    comments: dict[int, list[sqlite3.Row]] = {item: [] for item in ids}
    if ids:
        placeholders = ",".join("?" for _ in ids)
        for row in db().execute(
            f"SELECT c.*,COALESCE(u.username,c.owner_name,'community') author FROM comments c LEFT JOIN users u ON u.id=c.user_id WHERE c.post_id IN ({placeholders}) ORDER BY c.id",
            ids,
        ):
            comments[row["post_id"]].append(row)
    return render_template(
        "post.html",
        question=question,
        answers=answers,
        comments=comments,
        tags=post_tags(post_id),
        title=question["title"],
    )


def payload() -> dict:
    return request.get_json(silent=True) or request.form.to_dict(flat=True)


def create_question(slug: str, data: dict) -> sqlite3.Row:
    site = site_or_404(slug)
    title = str(data.get("title", "")).strip()
    body = str(data.get("body", "")).strip()
    if not title or not body:
        abort(400, "title and body are required")
    stamp = now()
    cursor = db().execute(
        "INSERT INTO posts(site_id,post_type,user_id,title,body,created_at,updated_at,content_license) VALUES(?,'question',?,?,?,?,?,?)",
        (site["id"], g.user["id"], title[:300], body, stamp, stamp, LOCAL_CONTENT_LICENSE),
    )
    add_tags(db(), site["id"], cursor.lastrowid, data.get("tags", ""))
    record_event(
        db(), "question", slug, g.user["username"], "post", cursor.lastrowid, title
    )
    db().commit()
    export_activity()
    return post_or_404(cursor.lastrowid)


@app.route("/s/<slug>/ask", methods=["GET", "POST"])
@login_required
def ask(slug: str):
    site = site_or_404(slug)
    if request.method == "POST":
        created = create_question(slug, payload())
        return redirect(url_for("post", post_id=created["id"]))
    return render_template("ask.html", site=site, title="Ask a question")


@app.post("/api/posts/<int:question_id>/answers")
@app.post("/q/<int:question_id>/answers")
@login_required
def answer(question_id: int):
    question = post_or_404(question_id)
    body = str(payload().get("body", "")).strip()
    if question["post_type"] != "question" or not body:
        abort(400, "answer body required")
    stamp = now()
    cursor = db().execute(
        "INSERT INTO posts(site_id,post_type,parent_id,user_id,body,created_at,updated_at,content_license) VALUES(?,'answer',?,?,?,?,?,?)",
        (question["site_id"], question_id, g.user["id"], body, stamp, stamp, LOCAL_CONTENT_LICENSE),
    )
    record_event(
        db(),
        "answer",
        question["site_slug"],
        g.user["username"],
        "post",
        cursor.lastrowid,
        body,
    )
    db().commit()
    export_activity()
    if request.path.startswith("/api/") or request.is_json:
        return jsonify(post=dict(post_or_404(cursor.lastrowid))), 201
    return redirect(url_for("post", post_id=question_id))


@app.post("/api/posts/<int:post_id>/comments")
@app.post("/p/<int:post_id>/comments")
@login_required
def comment(post_id: int):
    target = post_or_404(post_id)
    body = str(payload().get("body", "")).strip()
    if not body:
        abort(400, "comment body required")
    cursor = db().execute(
        "INSERT INTO comments(post_id,user_id,body,created_at,content_license) VALUES(?,?,?,?,?)",
        (post_id, g.user["id"], body[:1000], now(), LOCAL_CONTENT_LICENSE),
    )
    record_event(
        db(),
        "comment",
        target["site_slug"],
        g.user["username"],
        "comment",
        cursor.lastrowid,
        body,
    )
    db().commit()
    export_activity()
    if request.path.startswith("/api/") or request.is_json:
        return jsonify(
            comment={"id": cursor.lastrowid, "post_id": post_id, "body": body[:1000]}
        ), 201
    return redirect(url_for("post", post_id=target["parent_id"] or target["id"]))


@app.post("/api/posts/<int:post_id>/vote")
@app.post("/p/<int:post_id>/vote")
@login_required
def vote(post_id: int):
    target = post_or_404(post_id)
    try:
        value = int(payload().get("value", 0))
    except (TypeError, ValueError):
        value = 0
    if value not in (-1, 1):
        abort(400, "value must be -1 or 1")
    old = (
        db()
        .execute(
            "SELECT value FROM votes WHERE post_id=? AND user_id=?",
            (post_id, g.user["id"]),
        )
        .fetchone()
    )
    delta = value - (old["value"] if old else 0)
    db().execute(
        "INSERT INTO votes(post_id,user_id,value,created_at) VALUES(?,?,?,?) ON CONFLICT(post_id,user_id) DO UPDATE SET value=excluded.value,created_at=excluded.created_at",
        (post_id, g.user["id"], value, now()),
    )
    db().execute("UPDATE posts SET score=score+? WHERE id=?", (delta, post_id))
    record_event(
        db(),
        "vote",
        target["site_slug"],
        g.user["username"],
        "post",
        post_id,
        str(value),
    )
    db().commit()
    export_activity()
    if request.path.startswith("/api/") or request.is_json:
        score = (
            db().execute("SELECT score FROM posts WHERE id=?", (post_id,)).fetchone()[0]
        )
        return jsonify(post_id=post_id, score=score, vote=value)
    return redirect(url_for("post", post_id=target["parent_id"] or target["id"]))


@app.route("/account/register", methods=["GET", "POST"])
def register():
    error = ""
    if request.method == "POST":
        data = payload()
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,32}", username):
            error = "Use 2–32 letters, numbers, underscores, or hyphens."
        elif len(password) < 4:
            error = "Password must be at least four characters."
        else:
            try:
                token = secrets.token_urlsafe(24)
                cursor = db().execute(
                    "INSERT INTO users(username,password_hash,api_token,created_at) VALUES(?,?,?,?)",
                    (username, password_hash(password), token, now()),
                )
                db().commit()
                session["user_id"] = cursor.lastrowid
                if request.is_json:
                    return jsonify(username=username, token=token), 201
                return redirect(request.form.get("next") or url_for("index"))
            except sqlite3.IntegrityError:
                error = "That username is already in use."
    if request.is_json:
        return jsonify(error=error), 400
    return render_template(
        "account.html", mode="register", error=error, title="Create account"
    )


@app.route("/account/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        data = payload()
        user = (
            db()
            .execute(
                "SELECT * FROM users WHERE username=?",
                (str(data.get("username", "")).strip(),),
            )
            .fetchone()
        )
        if user and password_matches(
            str(data.get("password", "")), user["password_hash"]
        ):
            session["user_id"] = user["id"]
            if request.is_json:
                return jsonify(username=user["username"], token=user["api_token"])
            return redirect(request.form.get("next") or url_for("index"))
        error = "Incorrect username or password."
    if request.is_json:
        return jsonify(error=error), 401
    return render_template("account.html", mode="login", error=error, title="Log in")


@app.post("/account/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.get("/api/sites")
def api_sites():
    return jsonify(
        sites=[dict(x) for x in db().execute("SELECT * FROM sites ORDER BY slug")]
    )


@app.route("/api/sites/<slug>/questions", methods=["GET", "POST"])
def api_questions(slug: str):
    if request.method == "POST":
        if g.user is None:
            return jsonify(error="Bearer token required"), 401
        return jsonify(post=attributed_post(create_question(slug, payload()))), 201
    site = site_or_404(slug)
    rows = (
        db()
        .execute(
            "SELECT p.*,s.slug site_slug,COALESCE(u.username,p.owner_name,'community') author FROM posts p JOIN sites s ON s.id=p.site_id LEFT JOIN users u ON u.id=p.user_id WHERE p.site_id=? AND p.post_type='question' ORDER BY p.id DESC LIMIT 100",
            (site["id"],),
        )
        .fetchall()
    )
    return jsonify(questions=[attributed_post(x) for x in rows])


@app.get("/api/posts/<int:post_id>")
def api_post(post_id: int):
    item = attributed_post(post_or_404(post_id))
    item["tags"] = post_tags(post_id)
    item["answers"] = [
        attributed_post(x)
        for x in db().execute(
            "SELECT p.*,s.slug site_slug,COALESCE(u.username,p.owner_name,'community') author FROM posts p JOIN sites s ON s.id=p.site_id LEFT JOIN users u ON u.id=p.user_id WHERE p.parent_id=? ORDER BY p.score DESC,p.id",
            (post_id,),
        )
    ]
    item["comments"] = [
        dict(x)
        for x in db().execute(
            "SELECT * FROM comments WHERE post_id=? ORDER BY id", (post_id,)
        )
    ]
    return jsonify(post=item)


@app.get("/content-license")
def content_license():
    return render_template("content_license.html", title="Content licensing")


@app.get("/api/events")
def api_events():
    after = request.args.get("after", 0, type=int)
    rows = (
        db()
        .execute("SELECT * FROM events WHERE id>? ORDER BY id LIMIT 1000", (after,))
        .fetchall()
    )
    return jsonify(events=[dict(x) for x in rows])


@app.get("/health")
def health():
    return jsonify(ok=True)


if __name__ == "__main__":
    init_db()
    app.run(
        host=os.environ.get("STACK_HOST", "0.0.0.0"),
        port=int(os.environ.get("STACK_PORT", "3020")),
        threaded=True,
    )
