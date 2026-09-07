from __future__ import annotations

import json
from pathlib import Path

from server.app import DatasetService


def _write(path: Path, value: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding="utf-8")


def _fixture(tmp_path: Path) -> DatasetService:
    entries = [
        {
            "provider": "worldbank-common",
            "kind": "common-data",
            "label": "Population, total",
            "url": (
                "https://api.worldbank.org/v2/country/all/indicator/"
                "SP.POP.TOTL?format=json&per_page=20000"
            ),
            "path": "worldbank/common/SP.POP.TOTL.json",
        },
        {
            "provider": "datausa",
            "kind": "query-data",
            "label": "State population",
            "url": (
                "https://api.datausa.io/tesseract/data.jsonrecords?"
                "cube=acs_yg_total_population_5&drilldowns=State,Year&"
                "measures=Population&limit=100000,0"
            ),
            "path": "datausa/data/acs-population-by-state.jsonrecords",
        },
        {
            "provider": "oecd-common",
            "kind": "common-data",
            "label": "Monthly unemployment rates",
            "url": (
                "https://sdmx.oecd.org/public/rest/data/OECD.SDD.TPS,"
                "DSD_LFS@DF_IALFS_UNE_M,1.0/all?startPeriod=2020&"
                "format=csvfilewithlabels"
            ),
            "path": "oecd/common/monthly-unemployment.csv",
        },
    ]
    _write(
        tmp_path / "plan.jsonl",
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
    )
    _write(
        tmp_path / "files/worldbank/common/SP.POP.TOTL.json",
        json.dumps(
            [
                {"page": 1, "pages": 1, "per_page": 20000, "total": 3},
                [
                    {
                        "country": {"id": "US", "value": "United States"},
                        "countryiso3code": "USA",
                        "date": "2022",
                        "value": 333_000_000,
                    },
                    {
                        "country": {"id": "US", "value": "United States"},
                        "countryiso3code": "USA",
                        "date": "2021",
                        "value": 332_000_000,
                    },
                    {
                        "country": {"id": "CA", "value": "Canada"},
                        "countryiso3code": "CAN",
                        "date": "2022",
                        "value": 39_000_000,
                    },
                ],
            ]
        ),
    )
    _write(
        tmp_path / "files/datausa/data/acs-population-by-state.jsonrecords",
        json.dumps(
            {
                "annotations": {"dataset_name": "ACS 5-year Estimate"},
                "page": {"limit": 100000, "offset": 0, "total": 2},
                "columns": ["State ID", "State", "Year", "Population"],
                "data": [
                    {
                        "State ID": "04000US04",
                        "State": "Arizona",
                        "Year": 2022,
                        "Population": 7_100_000,
                    },
                    {
                        "State ID": "04000US13",
                        "State": "Georgia",
                        "Year": 2022,
                        "Population": 10_900_000,
                    },
                ],
            }
        ),
    )
    _write(
        tmp_path / "files/oecd/common/monthly-unemployment.csv",
        "REF_AREA,TIME_PERIOD,OBS_VALUE\nUSA,2022-01,4.0\n",
    )
    _write(tmp_path / "catalogs/datausa-cubes.json", '{"cubes":[]}')
    _write(tmp_path / "catalogs/oecd-dataflows.xml", "<Structure/>")
    _write(tmp_path / "catalog-errors.json", "[]")
    return DatasetService(tmp_path)


def _body(response) -> bytes:
    if isinstance(response.body, Path):
        return response.body.read_bytes()
    return response.body


def test_world_bank_route_filters_country_and_date(tmp_path: Path) -> None:
    service = _fixture(tmp_path)
    response = service.route(
        "GET",
        "/v2/country/USA/indicator/SP.POP.TOTL?format=json&date=2022",
        "api.worldbank.org",
    )
    payload = json.loads(_body(response))
    assert response.status == 200
    assert payload[0]["total"] == 1
    assert payload[1][0]["countryiso3code"] == "USA"
    assert payload[1][0]["date"] == "2022"


def test_world_bank_indicator_page_uses_provider_route(tmp_path: Path) -> None:
    service = _fixture(tmp_path)
    response = service.route("GET", "/indicator/SP.POP.TOTL", "data.worldbank.org")
    body = _body(response).decode()
    assert response.status == 200
    assert "World Bank Data" in body
    assert "/v2/country/all/indicator/SP.POP.TOTL" in body


def test_datausa_route_filters_geography(tmp_path: Path) -> None:
    service = _fixture(tmp_path)
    response = service.route(
        "GET",
        (
            "/api/data?cube=acs_yg_total_population_5&drilldowns=State,Year&"
            "measures=Population&Geography=04000US04&limit=10,0"
        ),
        "datausa.io",
    )
    payload = json.loads(_body(response))
    assert response.status == 200
    assert payload["page"]["total"] == 1
    assert payload["data"][0]["State"] == "Arizona"


def test_oecd_data_and_catalog_routes(tmp_path: Path) -> None:
    service = _fixture(tmp_path)
    data = service.route(
        "GET",
        "/public/rest/data/OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0/all",
        "sdmx.oecd.org",
    )
    catalog = service.route(
        "GET", "/public/rest/dataflow/all/all/latest", "sdmx.oecd.org"
    )
    assert data.status == 200
    assert b"USA,2022-01,4.0" in _body(data)
    assert catalog.content_type.startswith("application/xml")


def test_catalog_reports_availability_and_blocks_traversal(tmp_path: Path) -> None:
    service = _fixture(tmp_path)
    catalog = json.loads(_body(service.route("GET", "/catalog.json")))
    traversal = service.route("GET", "/files/../../plan.jsonl")
    csv_download = service.route(
        "GET", "/files/oecd/common/monthly-unemployment.csv"
    )
    archive_path = tmp_path / "files/benchmark-extra/archive.zip"
    _write(archive_path, b"PK\x03\x04")
    archive_download = service.route("GET", "/download/benchmark-extra/archive.zip")
    assert catalog["count"] == 3
    assert all(item["available"] for item in catalog["results"])
    oecd_entry = next(
        item
        for item in catalog["results"]
        if item["path"] == "oecd/common/monthly-unemployment.csv"
    )
    assert oecd_entry["download_url"] is None
    assert csv_download.status == 404
    assert archive_download.status == 404
    assert traversal.status == 404


def test_per_container_dataset_deny_list(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(
        "DATA_SERVICE_DENY",
        "worldbank:SP.POP.TOTL,datausa:acs_yg_total_population_5",
    )
    service = _fixture(tmp_path)
    world_bank = service.route(
        "GET",
        "/v2/country/USA/indicator/SP.POP.TOTL?format=json",
        "api.worldbank.org",
    )
    datausa = service.route(
        "GET",
        (
            "/api/data?cube=acs_yg_total_population_5&drilldowns=State&"
            "measures=Population"
        ),
        "datausa.io",
    )
    health = service.route("GET", "/health")
    raw = service.route("GET", "/files/worldbank/common/SP.POP.TOTL.json")
    catalog = json.loads(_body(service.route("GET", "/catalog.json")))
    population_entry = next(
        item
        for item in catalog["results"]
        if item["path"] == "worldbank/common/SP.POP.TOTL.json"
    )
    assert world_bank.status == 404
    assert datausa.status == 404
    assert raw.status == 404
    assert population_entry["available"] is False
    assert population_entry["download_url"] is None
    assert health.status == 200
