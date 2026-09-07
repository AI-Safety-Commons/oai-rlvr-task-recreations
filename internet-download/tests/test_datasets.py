import json
from pathlib import Path

from internet_download.datasets import (
    DatasetFile,
    _datausa_files,
    _ilostat_files,
    _oecd_files,
    fetch_dataset_plan,
    read_dataset_plan,
    write_dataset_plan,
)


def test_ilostat_catalog_expands_all_indicator_rows(
    monkeypatch, tmp_path: Path
) -> None:
    body = (
        b"id,indicator.label\n"
        b"EAP_2WAP_SEX_AGE_RT_A,Labour force participation rate\n"
        b"EMP_TEMP_SEX_STE_NB_A.csv.gz,Temporary employees\n"
    )
    monkeypatch.setattr("internet_download.datasets._read", lambda *args: body)
    files = _ilostat_files({"dictionaries": ["indicator_en.csv"]}, tmp_path)

    assert [item.path for item in files] == [
        "ilostat/indicator/EAP_2WAP_SEX_AGE_RT_A.csv.gz",
        "ilostat/indicator/EMP_TEMP_SEX_STE_NB_A.csv.gz",
        "ilostat/dic/indicator_en.csv",
    ]
    assert (tmp_path / "catalogs/ilostat-indicators.csv").read_bytes() == body


def test_oecd_catalog_expands_data_and_structure(monkeypatch, tmp_path: Path) -> None:
    body = b"""<?xml version="1.0"?>
    <mes:Structure xmlns:mes="urn:sdmx:org.sdmx.infomodel.message:2.0"
      xmlns:str="urn:sdmx:org.sdmx.infomodel.structure:2.0">
      <mes:Structures><str:Dataflows>
        <str:Dataflow agencyID="OECD.SDD" id="DSD_TEST@DF_ONE" version="1.2"/>
      </str:Dataflows></mes:Structures>
    </mes:Structure>"""
    monkeypatch.setattr("internet_download.datasets._read", lambda *args: body)
    files = _oecd_files({}, tmp_path)

    assert len(files) == 2
    assert "/data/OECD.SDD,DSD_TEST@DF_ONE,1.2/all?" in files[0].url
    assert files[0].path.endswith("DSD_TEST@DF_ONE--1.2.csv")
    assert files[1].kind == "structure"


def test_datausa_catalog_plans_every_cube_schema(monkeypatch, tmp_path: Path) -> None:
    body = json.dumps({"cubes": [{"name": "acs"}, {"name": "ipeds"}]}).encode()
    monkeypatch.setattr("internet_download.datasets._read", lambda *args: body)
    files = _datausa_files({}, tmp_path)
    assert [item.path for item in files] == [
        "datausa/cubes/acs.json",
        "datausa/cubes/ipeds.json",
    ]


def test_plan_includes_configured_exact_responses(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("internet_download.datasets._ilostat_files", lambda *args: [])
    monkeypatch.setattr("internet_download.datasets._oecd_files", lambda *args: [])
    monkeypatch.setattr("internet_download.datasets._datausa_files", lambda *args: [])
    config = {
        "files": [
            {
                "provider": "worldbank",
                "kind": "benchmark-response",
                "url": "https://data.worldbank.org/indicator/EXAMPLE",
                "path": "worldbank/benchmark/example.html",
            }
        ]
    }
    count, providers = write_dataset_plan(config, tmp_path)
    assert count == 1
    assert providers == {"worldbank": 1}
    assert read_dataset_plan(tmp_path / "plan.jsonl") == [
        DatasetFile(
            "worldbank",
            "benchmark-response",
            "https://data.worldbank.org/indicator/EXAMPLE",
            "worldbank/benchmark/example.html",
            "",
        )
    ]


def test_plan_keeps_other_providers_when_one_catalog_fails(
    monkeypatch, tmp_path: Path
) -> None:
    def fail(*args):
        raise RuntimeError("temporarily unavailable")

    monkeypatch.setattr("internet_download.datasets._ilostat_files", fail)
    monkeypatch.setattr("internet_download.datasets._oecd_files", lambda *args: [])
    monkeypatch.setattr("internet_download.datasets._datausa_files", lambda *args: [])
    count, _ = write_dataset_plan({"files": []}, tmp_path)
    assert count == 0
    errors = json.loads((tmp_path / "catalog-errors.json").read_text())
    assert errors == [{"provider": "ilostat", "error": "temporarily unavailable"}]


def test_fetch_cap_counts_existing_dataset_files(monkeypatch, tmp_path: Path) -> None:
    output = tmp_path / "dataset"
    output.mkdir()
    (output / "already-present.bin").write_bytes(b"x" * 6500)
    entries = [
        DatasetFile("test", "data", f"https://example.test/{name}", f"test/{name}")
        for name in ("one", "two")
    ]
    (output / "plan.jsonl").write_text(
        "".join(json.dumps(item.__dict__) + "\n" for item in entries)
    )
    calls = []

    def fake_download(entry, root, _cap):
        calls.append(entry.path)
        destination = root / "files" / entry.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"y" * 1000)
        return 1000, "digest"

    monkeypatch.setattr("internet_download.datasets._download", fake_download)
    fetch_dataset_plan(
        output / "plan.jsonl",
        output,
        max_total_bytes=9000,
        max_file_bytes=None,
    )
    assert calls == ["test/one"]
    assert (
        sum(path.stat().st_size for path in output.rglob("*") if path.is_file()) < 9000
    )
