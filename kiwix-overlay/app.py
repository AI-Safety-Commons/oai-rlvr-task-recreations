#!/usr/bin/env python3
"""A deliberately small, server-rendered editing layer for Kiwix wikis."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import secrets
import sqlite3
import ssl
import threading
from datetime import datetime, timezone
from functools import wraps
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.request import Request, urlopen

from flask import Flask, Response, abort, g, redirect, render_template, request, session, url_for
from markupsafe import Markup, escape
from werkzeug.serving import make_server


DATA_DIR = Path(os.environ.get("OVERLAY_DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("OVERLAY_DB", DATA_DIR / "overlay.db"))
APP_DIR = Path(__file__).resolve().parent
KIWIX_URL = os.environ.get("KIWIX_URL", "http://kiwix:8080").rstrip("/")
ARTICLE_TEMPLATE = os.environ.get("KIWIX_ARTICLE_TEMPLATE", "/content/{book}/{title}")
DEFAULT_BOOK = os.environ.get("KIWIX_BOOK", "wikipedia_en_all_maxi")
try:
    HOST_BOOKS = {
        host.lower(): book
        for host, book in json.loads(os.environ.get("KIWIX_HOST_BOOKS", "{}")).items()
    }
except (TypeError, ValueError):
    HOST_BOOKS = {}

app = Flask(
    __name__,
    template_folder=str(APP_DIR / "templates"),
    static_folder=str(APP_DIR / "static"),
)
app.secret_key = os.environ.get("OVERLAY_SECRET", "airgapped-development-only")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def db() -> sqlite3.Connection:
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error: object = None) -> None:
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS revisions (
            id INTEGER PRIMARY KEY, book TEXT NOT NULL, title TEXT NOT NULL,
            html TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            username TEXT NOT NULL, created_at TEXT NOT NULL,
            parent_id INTEGER, is_snapshot INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS revisions_page
            ON revisions(book, title, id DESC);
        """
    )
    connection.close()


def password_hash(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000)
    return f"{salt}:{digest.hex()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split(":", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 100_000).hex()
        return secrets.compare_digest(actual, expected)
    except ValueError:
        return False


@app.before_request
def load_user() -> None:
    user_id = session.get("user_id")
    g.user = db().execute("SELECT id, username FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


class ArticleExtractor(HTMLParser):
    """Extract Kiwix's article body without depending on MediaWiki internals."""

    def __init__(self, source_url: str, book: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source_url = source_url
        self.book = book
        self.depth = 0
        self.parts: list[str] = []
        self.capture = False

    def rendered_attrs(self, tag, attrs) -> str:
        rendered = []
        for key, value in attrs:
            if value and key in {"href", "src", "poster"} and not value.startswith(("#", "data:", "mailto:", "javascript:")):
                absolute = urljoin(self.source_url, value)
                parsed = urlsplit(absolute)
                origin = urlsplit(KIWIX_URL)
                if (parsed.scheme, parsed.netloc) == (origin.scheme, origin.netloc):
                    article_prefix = f"/content/{self.book}/"
                    if tag == "a" and parsed.path.startswith(article_prefix):
                        target = unquote(parsed.path[len(article_prefix):])
                        value = url_for("article", book=self.book, title=target)
                        if parsed.fragment:
                            value += f"#{parsed.fragment}"
                    else:
                        value = url_for("source_asset", path=parsed.path.lstrip("/"))
                        if parsed.query:
                            value += f"?{parsed.query}"
            rendered.append(f' {escape(key)}="{escape(value or "")}"')
        return "".join(rendered)

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if not self.capture and (attributes.get("id") == "mw-content-text" or "mw-parser-output" in attributes.get("class", "").split()):
            self.capture = True
            self.depth = 1
            return
        if self.capture:
            self.depth += 1
            self.parts.append(f"<{tag}{self.rendered_attrs(tag, attrs)}>")

    def handle_startendtag(self, tag, attrs):
        if self.capture:
            self.parts.append(f"<{tag}{self.rendered_attrs(tag, attrs)}>")

    def handle_endtag(self, tag):
        if self.capture:
            self.depth -= 1
            if self.depth == 0:
                self.capture = False
            else:
                self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        if self.capture:
            self.parts.append(str(escape(data)))

    def handle_entityref(self, name):
        if self.capture:
            self.parts.append(f"&{name};")

    def handle_charref(self, name):
        if self.capture:
            self.parts.append(f"&#{name};")


def upstream_html(book: str, title: str) -> str:
    path = ARTICLE_TEMPLATE.format(book=quote(book, safe=""), title=quote(title.replace(" ", "_"), safe="/"))
    source_url = f"{KIWIX_URL}{path}"
    try:
        with urlopen(Request(source_url, headers={"User-Agent": "KiwixOverlay/1.0"}), timeout=8) as response:
            raw = response.read().decode(response.headers.get_content_charset() or "utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError):
        return (
            '<div class="notice"><strong>Kiwix article unavailable.</strong> '
            "This page can still be created in the overlay by choosing Edit.</div>"
        )
    parser = ArticleExtractor(source_url, book)
    parser.feed(raw)
    return "".join(parser.parts) if parser.parts else raw


def latest_revision(book: str, title: str):
    return db().execute(
        "SELECT * FROM revisions WHERE book = ? AND title = ? ORDER BY id DESC LIMIT 1", (book, title)
    ).fetchone()


def page_context(book: str, title: str, active: str) -> dict:
    return {"book": book, "title": title, "active": active, "encoded_title": quote(title.replace(" ", "_"), safe="")}


def request_book() -> str:
    """Select the archive represented by the requested fake-internet host."""
    host = request.host.rsplit(":", 1)[0].lower()
    return HOST_BOOKS.get(host, DEFAULT_BOOK)


@app.route("/")
def index():
    return redirect(url_for("article", book=request_book(), title="Main_Page"))


@app.route("/source/<path:path>")
def source_asset(path: str):
    """Pass through images and other static resources referenced by Kiwix HTML."""
    try:
        query = request.query_string.decode("ascii", errors="ignore")
        source_url = f"{KIWIX_URL}/{quote(path, safe='/')}" + (f"?{query}" if query else "")
        with urlopen(Request(source_url, headers={"User-Agent": "KiwixOverlay/1.0"}), timeout=8) as upstream:
            return Response(upstream.read(), content_type=upstream.headers.get("Content-Type", "application/octet-stream"))
    except (HTTPError, URLError, TimeoutError):
        abort(502)


@app.route("/wiki/<book>/<path:title>")
def article(book: str, title: str):
    title = unquote(title).replace("_", " ")
    revision = latest_revision(book, title)
    content = revision["html"] if revision else upstream_html(book, title)
    return render_template(
        "article.html", content=Markup(content), revision=revision, **page_context(book, title, "article")
    )


@app.route("/wiki/<path:title>")
def host_article(title: str):
    """Accept the usual wiki URL shape used by archived Wikimedia sites."""
    return article(request_book(), title)


@app.route("/content/<book>/<path:title>")
def content_article(book: str, title: str):
    """Translate native Kiwix article URLs into editable overlay URLs."""
    return redirect(url_for("article", book=book, title=title))


@app.route("/wiki/<book>/<path:title>/edit", methods=["GET", "POST"])
@login_required
def edit(book: str, title: str):
    title = unquote(title).replace("_", " ")
    current = latest_revision(book, title)
    if current is None:
        source = upstream_html(book, title)
        cursor = db().execute(
            "INSERT INTO revisions(book,title,html,summary,username,created_at,is_snapshot) VALUES(?,?,?,?,?,?,1)",
            (book, title, source, "Imported from Kiwix", "Kiwix snapshot", now()),
        )
        db().commit()
        current = db().execute("SELECT * FROM revisions WHERE id = ?", (cursor.lastrowid,)).fetchone()
    if request.method == "POST":
        content = request.form.get("html", "")
        summary = request.form.get("summary", "").strip()[:200]
        if not content.strip():
            return render_template("edit.html", source=content, error="Article HTML cannot be empty.", **page_context(book, title, "edit")), 400
        parent_id = current["id"] if current else None
        db().execute(
            "INSERT INTO revisions(book,title,html,summary,username,created_at,parent_id) VALUES(?,?,?,?,?,?,?)",
            (book, title, content, summary, g.user["username"], now(), parent_id),
        )
        db().commit()
        app.logger.info("edit_saved user=%s book=%s title=%r parent=%s", g.user["username"], book, title, parent_id)
        return redirect(url_for("article", book=book, title=title.replace(" ", "_")))

    source = current["html"]
    return render_template("edit.html", source=source, error="", revision=current, **page_context(book, title, "edit"))


@app.route("/wiki/<book>/<path:title>/history")
def history(book: str, title: str):
    title = unquote(title).replace("_", " ")
    revisions = db().execute(
        "SELECT * FROM revisions WHERE book = ? AND title = ? ORDER BY id DESC", (book, title)
    ).fetchall()
    return render_template("history.html", revisions=revisions, **page_context(book, title, "history"))


@app.route("/wiki/<book>/<path:title>/diff")
def diff(book: str, title: str):
    title = unquote(title).replace("_", " ")
    new_id = request.args.get("new", type=int)
    old_id = request.args.get("old", type=int)
    newest = db().execute("SELECT * FROM revisions WHERE id = ? AND book = ? AND title = ?", (new_id, book, title)).fetchone()
    if newest is None:
        abort(404)
    older = db().execute("SELECT * FROM revisions WHERE id = ? AND book = ? AND title = ?", (old_id or newest["parent_id"], book, title)).fetchone()
    old_lines = (older["html"] if older else "").splitlines()
    new_lines = newest["html"].splitlines()
    table = difflib.HtmlDiff(wrapcolumn=92).make_table(old_lines, new_lines, fromdesc=f"Revision {older['id'] if older else 'empty'}", todesc=f"Revision {newest['id']}", context=True, numlines=3)
    return render_template("diff.html", older=older, newest=newest, diff_table=Markup(table), **page_context(book, title, "history"))


@app.route("/wiki/<book>/<path:title>/talk")
def talk(book: str, title: str):
    title = unquote(title).replace("_", " ")
    return render_template("talk.html", **page_context(book, title, "talk"))


@app.route("/changes")
def changes():
    revisions = db().execute("SELECT * FROM revisions WHERE is_snapshot = 0 ORDER BY id DESC LIMIT 250").fetchall()
    return render_template("changes.html", revisions=revisions, active="changes", title="Recent changes", book=DEFAULT_BOOK)


@app.route("/account/register", methods=["GET", "POST"])
def register():
    error = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not 2 <= len(username) <= 32 or not username.replace("_", "").replace("-", "").isalnum():
            error = "Use 2–32 letters, numbers, underscores, or hyphens."
        elif len(password) < 4:
            error = "Password must be at least four characters."
        else:
            try:
                cursor = db().execute(
                    "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                    (username, password_hash(password), now()),
                )
                db().commit()
                session.clear()
                session["user_id"] = cursor.lastrowid
                app.logger.info("account_created user=%s", username)
                return redirect(request.form.get("next") or url_for("index"))
            except sqlite3.IntegrityError:
                error = "That username is already in use."
    return render_template("account.html", mode="register", error=error, next=request.args.get("next", ""), active="account", title="Create account", book=DEFAULT_BOOK)


@app.route("/account/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        user = db().execute("SELECT * FROM users WHERE username = ?", (request.form.get("username", "").strip(),)).fetchone()
        if user and password_matches(request.form.get("password", ""), user["password_hash"]):
            session.clear()
            session["user_id"] = user["id"]
            return redirect(request.form.get("next") or url_for("index"))
        error = "Incorrect username or password."
    return render_template("account.html", mode="login", error=error, next=request.args.get("next", ""), active="account", title="Log in", book=DEFAULT_BOOK)


@app.post("/account/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


@app.route("/health")
def health():
    return "<!doctype html><title>OK</title><p>OK</p>"


@app.errorhandler(404)
def not_found(_error):
    return render_template("simple.html", heading="Page not found", message="The requested overlay page does not exist.", active="", title="Not found", book=DEFAULT_BOOK), 404


if __name__ == "__main__":
    init_db()
    host = os.environ.get("OVERLAY_HOST", "0.0.0.0")
    server = make_server(host, int(os.environ.get("OVERLAY_PORT", "3010")), app, threaded=True)
    tls_server = make_server(host, int(os.environ.get("OVERLAY_TLS_PORT", "443")), app, threaded=True)
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls_context.load_cert_chain(
        os.environ.get("OVERLAY_TLS_CERT", "/tls/server.crt"),
        os.environ.get("OVERLAY_TLS_KEY", "/tls/server.key"),
    )
    tls_server.socket = tls_context.wrap_socket(tls_server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tls_server.serve_forever()
