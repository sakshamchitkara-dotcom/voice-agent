import functools

import httpx
import pytest

from app import web_tools
from tests.conftest import FIXTURES


@pytest.fixture
def mock_http(monkeypatch):
    """Route web_tools' httpx clients through a handler: mock_http(fn)."""
    def install(handler):
        real = httpx.AsyncClient
        monkeypatch.setattr(web_tools.httpx, "AsyncClient",
                            functools.partial(real, transport=httpx.MockTransport(handler)))
    return install


async def test_weather_formats_open_meteo(mock_http):
    def handler(req):
        if "geocoding" in req.url.host:
            return httpx.Response(200, json={"results": [
                {"name": "Paris", "country": "France", "latitude": 48.85, "longitude": 2.35}]})
        return httpx.Response(200, json={
            "current": {"temperature_2m": 18.4, "apparent_temperature": 17.6,
                        "weather_code": 61, "wind_speed_10m": 9.2},
            "daily": {"temperature_2m_max": [21.0], "temperature_2m_min": [12.0],
                      "precipitation_probability_max": [70]}})
    mock_http(handler)
    out = await web_tools.get_weather("Paris")
    assert out == ("Paris, France: light rain, 18°C (feels like 18°C), wind 9 km/h. "
                   "Today 12 to 21°C, 70% chance of rain.")


async def test_weather_unknown_place(mock_http):
    mock_http(lambda req: httpx.Response(200, json={}))
    assert "couldn't find" in await web_tools.get_weather("Nowhere")


def test_parse_ddg_skips_ads_and_unwraps_redirects():
    page = (FIXTURES / "ddg_results.html").read_text()
    results = web_tools.parse_ddg(page)
    assert results == [
        {"title": "Vapi - Build Advanced Voice AI Agents", "url": "https://vapi.ai/",
         "snippet": "Build, test, and deploy advanced voice AI agents in minutes with Vapi."},
        {"title": "Vapi & Docs", "url": "https://docs.vapi.ai/quickstart", "snippet": ""},
    ]
    assert web_tools.format_results(results).startswith("1. Vapi - Build")
    assert web_tools.format_results([]) == "No results found."


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/", "http://169.254.169.254/latest",
                                 "http://[::1]/", "file:///etc/passwd", "ftp://example.com/"])
async def test_fetch_refuses_private_and_non_http(url):
    with pytest.raises(ValueError):
        await web_tools.fetch_text(url)


async def test_fetch_revalidates_redirect_targets(mock_http, monkeypatch):
    seen = []

    async def fake_check(url):
        seen.append(url)
        if "10.0.0.5" in url:
            raise ValueError("that address is not publicly reachable")
    monkeypatch.setattr(web_tools, "_assert_public", fake_check)
    mock_http(lambda req: httpx.Response(302, headers={"location": "http://10.0.0.5/admin"}))
    with pytest.raises(ValueError, match="not publicly reachable"):
        await web_tools.fetch_text("https://public.example/start")
    assert seen == ["https://public.example/start", "http://10.0.0.5/admin"]


def test_html_to_text_drops_scripts_and_tags():
    page = "<html><head><title>x</title></head><body><script>evil()</script><p>Hi &amp; bye</p></body>"
    assert web_tools.html_to_text(page) == "Hi & bye"


async def test_ttl_cache_normalises_keys_expires_and_skips_errors(monkeypatch):
    calls = []

    @web_tools.ttl_cache(60)
    async def lookup(q):
        calls.append(q)
        if q == "boom":
            raise RuntimeError("upstream down")
        return q.upper()

    assert await lookup("Paris") == "PARIS"
    assert await lookup("  paris ") == "PARIS"  # same key after normalising
    assert calls == ["Paris"]
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await lookup("boom")
    assert calls.count("boom") == 2  # failures are retried, not cached
    now = web_tools.time.monotonic()
    monkeypatch.setattr(web_tools.time, "monotonic", lambda: now + 61)
    await lookup("paris")
    assert calls[-1] == "paris"


async def test_weather_is_cached(mock_http):
    hits = []

    def handler(req):
        hits.append(req.url.host)
        return httpx.Response(200, json={})
    mock_http(handler)
    await web_tools.get_weather("Atlantis")
    await web_tools.get_weather("atlantis")
    assert len(hits) == 1


async def test_headlines_from_bbc_rss(mock_http):
    urls = []

    def handler(req):
        urls.append(str(req.url))
        return httpx.Response(200, text=(FIXTURES / "bbc_rss.xml").read_text())
    mock_http(handler)
    out = await web_tools.headlines("technology")
    assert out == ("BBC technology headlines: 1. Chipmaker unveils faster AI processor. "
                   "2. Can satellite broadband reach remote islands?")
    assert urls == ["https://feeds.bbci.co.uk/news/technology/rss.xml"]
    assert await web_tools.headlines("technology", "chip performance") == \
        "BBC technology headlines: 1. Chipmaker unveils faster AI processor."
    assert await web_tools.headlines("technology", "elections") == \
        "No technology headlines mention elections."
    assert len(urls) == 1  # feed is cached
    with pytest.raises(ValueError, match="Pick a news topic"):
        await web_tools.headlines("gossip")


async def test_wikipedia_lookup(mock_http):
    params = []

    def handler(req):
        params.append(dict(req.url.params))
        if req.url.params["gsrsearch"] == "zzqx":
            return httpx.Response(200, json={"batchcomplete": True})
        return httpx.Response(200, json={"query": {"pages": [{
            "pageid": 974, "title": "Ada Lovelace",
            "extract": "Augusta Ada King, Countess of Lovelace, was an English\nmathematician."}]}})
    mock_http(handler)
    assert await web_tools.wikipedia("ada lovelace") == (
        "From Wikipedia, Ada Lovelace: Augusta Ada King, Countess of Lovelace, was an English mathematician.")
    assert params[0]["generator"] == "search" and params[0]["exintro"] == "1"
    assert await web_tools.wikipedia("zzqx") == "Wikipedia has no article matching zzqx."
