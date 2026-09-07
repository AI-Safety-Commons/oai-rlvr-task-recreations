import io
import tarfile
from pathlib import Path

from internet_download.packages import (
    _apk_dep_name,
    _apk_records,
    pypi_download_commands,
    resolve_apk_packages,
)


def test_apk_dependency_name_normalization() -> None:
    assert _apk_dep_name("python3~3.12") == "python3"
    assert _apk_dep_name("so:libc.musl-x86_64.so.1") == "so:libc.musl-x86_64.so.1"
    assert _apk_dep_name("busybox=1.2-r0") == "busybox"


def test_apk_index_parsing_and_dependency_closure(tmp_path: Path) -> None:
    index = tmp_path / "APKINDEX.tar.gz"
    contents = (
        b"P:app\nV:1.0-r0\nD:python3 so:libc.so.1\n\n"
        b"P:python3\nV:3.12-r0\nD:musl\n\n"
        b"P:musl\nV:1.2-r0\np:so:libc.so.1=1\n\n"
    )
    with tarfile.open(index, "w:gz") as archive:
        info = tarfile.TarInfo("APKINDEX")
        info.size = len(contents)
        archive.addfile(info, io.BytesIO(contents))

    records = _apk_records(index)
    selected = resolve_apk_packages({"main": records}, ["app"])
    assert [record["P"] for _, record in selected] == ["app", "musl", "python3"]


def test_pypi_foreign_alpine_target(tmp_path: Path) -> None:
    commands = pypi_download_commands(
        {
            "python": "python3",
            "requirements": ["numpy"],
            "targets": [
                {
                    "name": "alpine",
                    "platform": "musllinux_1_2_x86_64",
                    "python_version": "3.12",
                    "implementation": "cp",
                    "abis": ["cp312", "abi3"],
                }
            ],
        },
        tmp_path,
    )
    command = commands[0]
    assert command[command.index("--platform") + 1] == "musllinux_1_2_x86_64"
    assert command[command.index("--python-version") + 1] == "3.12"
    assert "--only-binary=:all:" in command
    assert command[-1] == "numpy"
