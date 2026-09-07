import importlib.util
import os
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "app.py"


def load_app(tmp_path):
    os.environ["OVERLAY_DATA_DIR"] = str(tmp_path)
    os.environ["OVERLAY_DB"] = str(tmp_path / "test.db")
    spec = importlib.util.spec_from_file_location("kiwix_overlay_app", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    module.init_db()
    return module


def register(client):
    return client.post("/account/register", data={"username": "researcher", "password": "test-pass"})


def test_account_edit_history_diff_and_talk(monkeypatch, tmp_path):
    module = load_app(tmp_path)
    monkeypatch.setattr(module, "upstream_html", lambda _book, _title: "<p>Offline baseline.</p>")
    client = module.app.test_client()

    assert register(client).status_code == 302
    response = client.post(
        "/wiki/demo/Schelling_Point/edit",
        data={"html": "<p>Edited article.</p>", "summary": "Added a concise description"},
    )
    assert response.status_code == 302
    assert b"Edited article" in client.get("/wiki/demo/Schelling_Point").data
    history = client.get("/wiki/demo/Schelling_Point/history")
    assert b"Kiwix baseline" in history.data
    assert b"Added a concise description" in history.data
    assert b"There are no discussions" in client.get("/wiki/demo/Schelling_Point/talk").data

    with module.app.app_context():
        revision = module.db().execute("SELECT max(id) FROM revisions").fetchone()[0]
    diff = client.get(f"/wiki/demo/Schelling_Point/diff?new={revision}")
    assert diff.status_code == 200
    assert b"Newer revision" in diff.data
    assert b"Added a concise description" in diff.data


def test_edit_requires_login(tmp_path):
    module = load_app(tmp_path)
    response = module.app.test_client().get("/wiki/demo/Page/edit")
    assert response.status_code == 302
    assert "/account/login" in response.headers["Location"]


def test_recent_changes_is_html_only(monkeypatch, tmp_path):
    module = load_app(tmp_path)
    monkeypatch.setattr(module, "upstream_html", lambda _book, _title: "<p>Baseline.</p>")
    client = module.app.test_client()
    register(client)
    client.post("/wiki/demo/Page/edit", data={"html": "<p>One.</p>", "summary": "First"})
    response = client.get("/changes")
    assert response.content_type.startswith("text/html")
    assert b"researcher" in response.data


def test_kiwix_links_and_assets_are_routed_through_overlay(tmp_path):
    module = load_app(tmp_path)
    with module.app.test_request_context():
        parser = module.ArticleExtractor("http://kiwix:8080/content/demo/Page", "demo")
        parser.feed('<div id="mw-content-text"><a href="Other">Other</a><img src="../I/photo.jpg"></div>')
    content = "".join(parser.parts)
    assert 'href="/wiki/demo/Other"' in content
    assert 'src="/source/content/I/photo.jpg"' in content
