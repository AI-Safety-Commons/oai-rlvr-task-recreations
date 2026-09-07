from __future__ import annotations

import html
import json
import shutil
import subprocess
import tarfile
import tempfile
from collections import deque
from pathlib import Path
from typing import Any

from .http import atomic_write, request


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    executable = command[0]
    if shutil.which(executable) is None:
        raise RuntimeError(f"required executable is not installed: {executable}")
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _download(url: str, destination: Path) -> None:
    if destination.exists():
        return
    with request(url, timeout=300) as response:
        atomic_write(destination, response.read())


def _apk_records(index_path: Path) -> list[dict[str, str]]:
    with tarfile.open(index_path, "r:gz") as archive:
        member = archive.extractfile("APKINDEX")
        if member is None:
            raise ValueError(f"APKINDEX member missing from {index_path}")
        text = member.read().decode()
    records: list[dict[str, str]] = []
    for paragraph in text.split("\n\n"):
        record = {}
        for line in paragraph.splitlines():
            if len(line) >= 2 and line[1] == ":":
                record[line[0]] = line[2:]
        if "P" in record and "V" in record:
            records.append(record)
    return records


def _apk_dep_name(requirement: str) -> str:
    requirement = requirement.split("@", 1)[0]
    for operator in (">=", "<=", "~=", "=", ">", "<", "~"):
        requirement = requirement.split(operator, 1)[0]
    return requirement.lstrip("!")


def resolve_apk_packages(
    records_by_repo: dict[str, list[dict[str, str]]], seeds: list[str]
) -> list[tuple[str, dict[str, str]]]:
    providers: dict[str, tuple[str, dict[str, str]]] = {}
    for repository, records in records_by_repo.items():
        for record in records:
            item = (repository, record)
            providers.setdefault(record["P"], item)
            for provided in record.get("p", "").split():
                providers.setdefault(_apk_dep_name(provided), item)

    selected: dict[str, tuple[str, dict[str, str]]] = {}
    pending = deque(seeds)
    while pending:
        requirement = pending.popleft()
        name = _apk_dep_name(requirement)
        if not name or name in selected:
            continue
        provider = providers.get(name)
        if provider is None:
            # Alpine indexes contain a few dependency constraints fulfilled by
            # the base system or solver internals; apk will validate them later.
            continue
        package_name = provider[1]["P"]
        if package_name in selected:
            continue
        selected[package_name] = provider
        pending.extend(provider[1].get("D", "").split())
    return sorted(selected.values(), key=lambda item: item[1]["P"])


def fetch_alpine(config: dict[str, Any], output: Path) -> None:
    release = config["release"]
    architecture = config.get("architecture", "x86_64")
    base_url = config.get("base_url", "https://dl-cdn.alpinelinux.org/alpine")
    repositories = list(config.get("repositories", ["main", "community"]))
    records_by_repo: dict[str, list[dict[str, str]]] = {}
    for repository in repositories:
        repository_dir = output / release / repository / architecture
        index_path = repository_dir / "APKINDEX.tar.gz"
        _download(
            f"{base_url}/{release}/{repository}/{architecture}/APKINDEX.tar.gz",
            index_path,
        )
        records_by_repo[repository] = _apk_records(index_path)

    if config.get("mirror_all", False):
        selected = sorted(
            (
                (repository, record)
                for repository, records in records_by_repo.items()
                for record in records
            ),
            key=lambda item: (item[0], item[1]["P"]),
        )
    else:
        selected = resolve_apk_packages(
            records_by_repo, list(config.get("packages", []))
        )
    for repository, record in selected:
        filename = f"{record['P']}-{record['V']}.apk"
        destination = output / release / repository / architecture / filename
        _download(
            f"{base_url}/{release}/{repository}/{architecture}/{filename}", destination
        )
    manifest = [
        {"repository": repository, "name": record["P"], "version": record["V"]}
        for repository, record in selected
    ]
    atomic_write(output / "manifest.json", json.dumps(manifest, indent=2).encode())


def pypi_download_commands(config: dict[str, Any], output: Path) -> list[list[str]]:
    targets = list(config.get("targets", []))
    if not targets:
        targets = [{"name": "host"}]
    commands = []
    for target in targets:
        artifacts = output / "artifacts" / target["name"]
        artifacts.mkdir(parents=True, exist_ok=True)
        command = [
            config.get("python", "python3"),
            "-m",
            "pip",
            "download",
            "--dest",
            str(artifacts.resolve()),
            "--disable-pip-version-check",
        ]
        if platform := target.get("platform"):
            command.extend(["--platform", platform])
            command.extend(["--python-version", str(target["python_version"])])
            command.extend(["--implementation", target.get("implementation", "cp")])
            for abi in target.get("abis", []):
                command.extend(["--abi", abi])
            command.append("--only-binary=:all:")
        command.extend(list(config.get("requirements", [])))
        commands.append(command)
    return commands


def fetch_pypi(config: dict[str, Any], output: Path) -> None:
    for command in pypi_download_commands(config, output):
        try:
            _run(command, cwd=output)
        except subprocess.CalledProcessError:
            # One package may have no wheel for the requested Alpine
            # architecture (for example matplotlib on musllinux/aarch64).
            # Retry requirements individually so compatible packages survive.
            requirements = list(config.get("requirements", []))
            prefix = (
                command[: command.index(requirements[0])] if requirements else command
            )
            for requirement in requirements:
                try:
                    _run(prefix + [requirement], cwd=output)
                except subprocess.CalledProcessError:
                    print(f"Warning: no compatible PyPI artifact for {requirement}")
    artifacts = output / "artifacts"
    links = "\n".join(
        f'<a href="{html.escape(path.relative_to(output).as_posix())}">'
        f"{html.escape(path.name)}</a><br>"
        for path in sorted(artifacts.rglob("*"))
        if path.is_file()
    )
    atomic_write(
        output / "index.html",
        f"<!doctype html><html><body>{links}</body></html>".encode(),
    )


def fetch_cargo(config: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    dependencies = config.get("dependencies", {})
    with tempfile.TemporaryDirectory(dir=output) as temporary_name:
        temporary = Path(temporary_name)
        dependency_lines = "\n".join(
            f'{name} = "{version}"' for name, version in dependencies.items()
        )
        (temporary / "Cargo.toml").write_text(
            '[package]\nname = "offline-corpus-seed"\nversion = "0.0.0"\n'
            'edition = "2021"\n\n[dependencies]\n'
            f"{dependency_lines}\n"
        )
        (temporary / "src").mkdir()
        (temporary / "src" / "lib.rs").write_text("")
        _run(["cargo", "generate-lockfile"], cwd=temporary)
        _run(
            ["cargo", "vendor", "--locked", str((output / "vendor").resolve())],
            cwd=temporary,
        )
        shutil.copy2(temporary / "Cargo.lock", output / "Cargo.lock")
    atomic_write(
        output / "config.toml",
        b'[source.crates-io]\nreplace-with = "vendored-sources"\n\n'
        b'[source.vendored-sources]\ndirectory = "vendor"\n',
    )


def fetch_npm(config: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    cache = (output / "cache").resolve()
    with tempfile.TemporaryDirectory(dir=output) as temporary_name:
        temporary = Path(temporary_name)
        atomic_write(
            temporary / "package.json",
            json.dumps({"private": True, "dependencies": {}}).encode(),
        )
        _run(
            [
                "npm",
                "install",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--cache",
                str(cache),
                *list(config.get("packages", [])),
            ],
            cwd=temporary,
        )
        shutil.copy2(temporary / "package.json", output / "package.json")
        shutil.copy2(temporary / "package-lock.json", output / "package-lock.json")


def fetch_go(config: dict[str, Any], output: Path) -> None:
    import os

    output.mkdir(parents=True, exist_ok=True)
    modules = config.get("modules", {})
    requirements = "\n".join(f"\t{name} {version}" for name, version in modules.items())
    atomic_write(
        output / "go.mod",
        f"module offline-corpus-seed\n\ngo 1.22\n\nrequire (\n{requirements}\n)\n".encode(),
    )
    environment = {**os.environ, "GOMODCACHE": str((output / "modcache").resolve())}
    _run(["go", "mod", "download", "all"], cwd=output, env=environment)
