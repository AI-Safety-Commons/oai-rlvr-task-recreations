import asyncio

import pytest

from http_gateway.search import LocalSearch, SearchService


def test_empty_local_needs_no_model(tmp_path):
    assert LocalSearch({}, tmp_path / "index.db").search("query", 5) == []


def test_missing_key_preserves_local_results(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    service = SearchService({}, tmp_path / "index.db")
    monkeypatch.setattr(
        service.local,
        "search",
        lambda q, n: [
            {"url": "https://example.test/report", "text": "archive", "source": "local"}
        ],
    )
    result = asyncio.run(service.search("report", 5, "all"))
    assert result["results"][0]["text"] == "archive"
    assert "EXA_API_KEY" in result["errors"]["exa"]


def test_fusion_local_overrides_live(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setenv("EXA_API_KEY", "test-key")

    async def post(self, url, **kwargs):
        assert url == "https://api.exa.ai/search"
        assert kwargs["json"]["query"] == "report"
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://example.test/report", "text": "live"},
                    {"url": "https://other.test", "text": "other"},
                ]
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    service = SearchService({}, tmp_path / "index.db")
    monkeypatch.setattr(
        service.local,
        "search",
        lambda q, n: [
            {"url": "https://example.test/report", "text": "archive", "source": "local"}
        ],
    )
    result = asyncio.run(service.search("report", 2, "all"))
    assert len(result["results"]) == 2
    assert result["results"][0]["text"] == "archive"
    assert result["errors"] == {}


@pytest.mark.parametrize(
    "query,limit,source", [("", 5, "all"), ("x", 0, "all"), ("x", 5, "bogus")]
)
def test_invalid_input(tmp_path, query, limit, source):
    with pytest.raises(ValueError):
        asyncio.run(SearchService({}, tmp_path / "i.db").search(query, limit, source))


def test_local_ranking_cache_and_removed_pages(tmp_path, monkeypatch):
    import sys
    import types

    embedded = []

    class Vector(list):
        def tolist(self):
            return list(self)

    class Model:
        def __init__(self, **kwargs):
            pass

        def embed(self, texts):
            for text in texts:
                embedded.append(text)
                yield Vector([1, 0] if "employment" in text else [0, 1])

        def query_embed(self, texts):
            yield Vector([1, 0])

    monkeypatch.setitem(
        sys.modules, "fastembed", types.SimpleNamespace(TextEmbedding=Model)
    )
    db = tmp_path / "search.db"
    pages = {"https://a.test": "employment statistics", "https://b.test": "weather"}
    index = LocalSearch(pages, db)
    assert index.search("jobs", 1)[0]["url"] == "https://a.test"
    assert len(embedded) == 2
    assert LocalSearch(pages, db).search("jobs", 1)[0]["url"] == "https://a.test"
    assert len(embedded) == 2
    assert (
        LocalSearch({"https://b.test": "weather"}, db).search("jobs", 5)[0]["url"]
        == "https://b.test"
    )


@pytest.mark.parametrize(
    "local_weight,exa_weight,first",
    [
        ("2", "1", "local"),
        ("1", "2", "exa"),
        ("0", "1", "exa"),
        ("1", "0", "local"),
    ],
)
def test_weighted_ranking(tmp_path, monkeypatch, local_weight, exa_weight, first):
    import httpx

    monkeypatch.setenv("SEARCH_LOCAL_WEIGHT", local_weight)
    monkeypatch.setenv("SEARCH_EXA_WEIGHT", exa_weight)
    monkeypatch.setenv("EXA_API_KEY", "test-key")
    calls = []

    async def post(self, url, **kwargs):
        calls.append("exa")
        return httpx.Response(
            200,
            json={"results": [{"url": "https://web.test"}]},
            request=httpx.Request("POST", url),
        )

    def local(query, limit):
        calls.append("local")
        return [{"url": "https://local.test", "source": "local"}]

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    service = SearchService({}, tmp_path / "i.db")
    monkeypatch.setattr(service.local, "search", local)
    result = asyncio.run(service.search("query", 2, "all"))
    assert result["results"][0]["source"] == first
    assert ("local" in calls) == (local_weight != "0")
    assert ("exa" in calls) == (exa_weight != "0")
    calls.clear()
    asyncio.run(service.search("query", 2, "local"))
    assert calls == ["local"]


@pytest.mark.parametrize(
    "local,exa", [("-1", "1"), ("nan", "1"), ("1", "inf"), ("0", "0"), ("bad", "1")]
)
def test_invalid_weights(tmp_path, monkeypatch, local, exa):
    monkeypatch.setenv("SEARCH_LOCAL_WEIGHT", local)
    monkeypatch.setenv("SEARCH_EXA_WEIGHT", exa)
    with pytest.raises(ValueError):
        SearchService({}, tmp_path / "i.db")


def test_page_and_domain_weights_before_truncation(tmp_path, monkeypatch):
    config = tmp_path / "search-weights.json"
    config.write_text(
        '{"domains":{"example.test":2,"archive.example.test":3},'
        '"pages":{"https://archive.example.test/boost":4,'
        '"https://example.test/hide":0}}'
    )
    service = SearchService(
        {str(i): "text" for i in range(3)}, tmp_path / "i.db", config
    )
    assert service.page_weight("https://archive.example.test/boost") == 12
    assert service.page_weight("https://other.example.test/a") == 2
    assert service.page_weight("https://notexample.test/a") == 1
    assert service.page_weight("https://example.test/hide") == 0

    def local(query, limit):
        assert limit == 3  # Boosted page must survive candidate retrieval.
        return [
            {"url": url, "source": "local"}
            for url in [
                "https://example.test/hide",
                "https://other.test/a",
                "https://archive.example.test/boost",
            ]
        ]

    monkeypatch.setattr(service.local, "search", local)
    result = asyncio.run(service.search("query", 1, "local"))
    assert result["results"][0]["url"] == "https://archive.example.test/boost"
    assert len(result["results"]) == 1


@pytest.mark.parametrize(
    "config",
    [
        "[]",
        '{"typo":{}}',
        '{"pages":[]}',
        '{"domains":{"example.test":-1}}',
        '{"pages":{"https://a.test":true}}',
        '{"domains":{"https://a.test":2}}',
        '{"pages":{"relative":2}}',
        '{"pages":{"https://a.test":NaN}}',
    ],
)
def test_invalid_page_weight_config(tmp_path, config):
    path = tmp_path / "search-weights.json"
    path.write_text(config)
    with pytest.raises(ValueError):
        SearchService({}, tmp_path / "i.db", path)
