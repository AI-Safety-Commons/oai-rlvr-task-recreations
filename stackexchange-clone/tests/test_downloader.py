import logging

import download_archive


class Response:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def read(self, _size=-1):
        return next(self.chunks, b"")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_nested_archive_name_is_flattened(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="stackunderflow.download")
    monkeypatch.setattr(
        download_archive,
        "file_list",
        lambda _item: [{"name": "release/superuser.com.7z", "size": "3"}],
    )
    seen = []

    def fake_open(request, timeout):
        seen.append(request.full_url)
        return Response([b"abc"])

    monkeypatch.setattr(download_archive, "urlopen", fake_open)
    download_archive.download("dump", tmp_path, set(), False)
    assert (tmp_path / "superuser.com.7z").read_bytes() == b"abc"
    assert seen == ["https://archive.org/download/dump/release/superuser.com.7z"]
    assert "Selected 1 archives" in caplog.text
    assert "Finished superuser.com.7z" in caplog.text


def test_configure_logging_writes_file(tmp_path):
    log_file = tmp_path / "download.log"
    download_archive.configure_logging("INFO", log_file)
    download_archive.LOGGER.info("test record")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "test record" in log_file.read_text()


def test_configure_logging_uses_stdout(capsys):
    download_archive.configure_logging("INFO", None)
    download_archive.LOGGER.info("stdout record")
    assert "stdout record" in capsys.readouterr().out
