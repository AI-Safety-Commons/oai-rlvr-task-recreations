import importlib


def test_import_extracted_site(tmp_path, monkeypatch):
    data = tmp_path / "data"
    source = tmp_path / "superuser.com"
    source.mkdir()
    (source / "Users.xml").write_text(
        '<users><row Id="7" DisplayName="Ada" Reputation="42" /></users>'
    )
    (source / "Posts.xml").write_text(
        '<posts><row Id="10" PostTypeId="1" OwnerUserId="7" Title="Test?" Body="&lt;p&gt;Yes&lt;/p&gt;" Score="3" Tags="&lt;linux&gt;&lt;shell&gt;" CreationDate="2010-01-01" ContentLicense="CC BY-SA 2.5" /><row Id="11" PostTypeId="2" ParentId="10" OwnerDisplayName="Bob" Body="&lt;p&gt;Answer&lt;/p&gt;" CreationDate="2020-01-02" ContentLicense="CC BY-SA 4.0" /></posts>'
    )
    (source / "Comments.xml").write_text(
        '<comments><row Id="20" PostId="10" UserDisplayName="Cat" Text="Hi" CreationDate="2020-01-03" /></comments>'
    )
    monkeypatch.setenv("STACK_DATA_DIR", str(data))
    monkeypatch.setenv("STACK_DB", str(data / "db.sqlite"))
    import app
    import importer

    importlib.reload(app)
    importlib.reload(importer)
    counts = importer.import_path(source)["superuser.com"]
    assert counts == {"users": 1, "posts": 2, "comments": 1}
    with app.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT owner_name FROM posts WHERE external_id=10"
            ).fetchone()[0]
            == "Ada"
        )
        licenses = connection.execute(
            "SELECT external_id,content_license FROM posts ORDER BY external_id"
        ).fetchall()
        assert [tuple(row) for row in licenses] == [
            (10, "CC BY-SA 2.5"),
            (11, "CC BY-SA 4.0"),
        ]
    importer.import_path(source)
    with app.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM comments").fetchone()[0] == 1
