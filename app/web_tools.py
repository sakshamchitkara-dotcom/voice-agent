"""Keyless internet tools: Open-Meteo weather, DuckDuckGo search, BBC RSS news, Wikipedia,
URL fetch."""
from __future__ import annotations

import asyncio
import functools
import html
import ipaddress
import re
import socket
import time
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from . import metrics

UA = "Mozilla/5.0 (compatible; voice-agent/0.2; +https://github.com/sakshamchitkara-dotcom/voice-agent)"
TIMEOUT = httpx.Timeout(8.0, connect=4.0)

cache_lookups = metrics.Counter("voice_agent_cache_lookups_total", "Tool cache lookups.",
                                ("cache", "result"))
_caches: list[dict] = []
_client: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None


def http() -> httpx.AsyncClient:
    """One keep-alive client per event loop, shared by every lookup tool.

    A fresh client per request paid a TCP+TLS handshake to each upstream host every time:
    a cold weather lookup (geocode + forecast) took ~2s, and ~0.5s on a reused connection.
    """
    global _client
    loop = asyncio.get_running_loop()
    if _client is None or _client[0] is not loop or _client[1].is_closed:
        _client = (loop, httpx.AsyncClient(timeout=TIMEOUT, headers={"User-Agent": UA},
                                           limits=httpx.Limits(max_keepalive_connections=20)))
    return _client[1]


async def close_http() -> None:
    global _client
    if _client is not None:
        await _client[1].aclose()
        _client = None


def ttl_cache(seconds: float, maxsize: int = 256):
    """Cache an async function's successful results by its (normalised) string args.

    Voice tools must answer in well under a second; repeat questions within a call or
    across callers ("weather in Paris") should not hit the upstream API again. Concurrent
    misses for the same key share one upstream request (single-flight), so a burst of
    identical questions can't stampede the API into rate limiting us.
    ponytail: per-process dict with FIFO eviction; move to Redis for several instances.
    """
    def deco(fn):
        store: dict[tuple, tuple[float, object]] = {}
        inflight: dict[tuple, asyncio.Future] = {}
        _caches.append(store)

        @functools.wraps(fn)
        async def wrapper(*args):
            key = tuple(" ".join(str(a).lower().split()) for a in args)
            hit = store.get(key)
            if hit and hit[0] > time.monotonic():
                cache_lookups.inc(cache=fn.__name__, result="hit")
                return hit[1]
            if key in inflight:
                cache_lookups.inc(cache=fn.__name__, result="shared")
                return await asyncio.shield(inflight[key])
            cache_lookups.inc(cache=fn.__name__, result="miss")
            fut = inflight[key] = asyncio.get_running_loop().create_future()
            try:
                value = await fn(*args)  # exceptions are not cached
            except BaseException as e:
                fut.set_exception(e)
                fut.exception()  # mark retrieved: no "never retrieved" warning if unshared
                raise
            finally:
                inflight.pop(key, None)
            fut.set_result(value)
            if len(store) >= maxsize:
                store.pop(next(iter(store)))
            store[key] = (time.monotonic() + seconds, value)
            return value
        return wrapper
    return deco


def clear_caches() -> None:
    """Tests: drop cached values and the shared client (it may belong to a closed loop)."""
    global _client
    for store in _caches:
        store.clear()
    _client = None


# WMO weather interpretation codes used by Open-Meteo.
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
    73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers",
    81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


@ttl_cache(600)
async def get_weather(location: str) -> str:
    client = http()
    geo = await client.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": location, "count": 1, "language": "en", "format": "json"},
    )
    geo.raise_for_status()
    places = geo.json().get("results") or []
    if not places:
        return f"I couldn't find a place called {location}."
    p = places[0]
    wx = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 1,
            "timezone": "auto",
        },
    )
    wx.raise_for_status()
    d = wx.json()
    cur, day = d["current"], d["daily"]
    name = ", ".join(x for x in (p.get("name"), p.get("admin1"), p.get("country")) if x)
    rain = day["precipitation_probability_max"][0]
    return (
        f"{name}: {WMO.get(cur['weather_code'], 'unknown conditions')}, "
        f"{cur['temperature_2m']:.0f}°C (feels like {cur['apparent_temperature']:.0f}°C), "
        f"wind {cur['wind_speed_10m']:.0f} km/h. Today {day['temperature_2m_min'][0]:.0f} to "
        f"{day['temperature_2m_max'][0]:.0f}°C"
        + (f", {rain}% chance of rain." if rain is not None else ".")
    )


def strip_tags(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", fragment)).split())


def _real_url(href: str) -> str:
    # DDG sometimes wraps results as //duckduckgo.com/l/?uddg=<encoded target>
    if "duckduckgo.com/l/" in href:
        target = parse_qs(urlparse(href).query).get("uddg")
        if target:
            return unquote(target[0])
    return "https:" + href if href.startswith("//") else href


def parse_ddg(page: str, limit: int = 5) -> list[dict]:
    results = []
    for chunk in page.split('class="result__a"')[1:]:
        m = re.search(r'href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not m:
            continue
        url = _real_url(html.unescape(m.group(1)))
        if "duckduckgo.com/y.js" in url:  # sponsored result
            continue
        snip = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', chunk, re.S)
        results.append({"title": strip_tags(m.group(2)), "url": url,
                        "snippet": strip_tags(snip.group(1)) if snip else ""})
        if len(results) >= limit:
            break
    return results


@ttl_cache(300)
async def search(query: str, limit: int = 5) -> list[dict]:
    r = await http().post("https://html.duckduckgo.com/html/", data={"q": query})
    r.raise_for_status()
    if r.status_code == 202:  # DDG's bot challenge page
        raise RuntimeError("search provider is rate limiting us, try again shortly")
    return parse_ddg(r.text, limit)


def format_results(results: list[dict]) -> str:
    if not results:
        return "No results found."
    return " | ".join(f"{i}. {r['title']} ({r['url']}): {r['snippet']}"
                      for i, r in enumerate(results, 1))


NEWS_FEEDS = {
    topic: f"https://feeds.bbci.co.uk/news/{path}rss.xml"
    for topic, path in {
        "top": "", "world": "world/", "uk": "uk/", "us": "world/us_and_canada/",
        "business": "business/", "politics": "politics/", "technology": "technology/",
        "science": "science_and_environment/", "health": "health/",
        "entertainment": "entertainment_and_arts/",
    }.items()
}


def parse_rss(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    return [{"title": " ".join((item.findtext("title") or "").split()),
             "summary": " ".join((item.findtext("description") or "").split()),
             "published": item.findtext("pubDate") or ""}
            for item in root.iter("item")]


@ttl_cache(300)
async def _feed(topic: str) -> list[dict]:
    r = await http().get(NEWS_FEEDS[topic])
    r.raise_for_status()
    return parse_rss(r.text)


async def headlines(topic: str = "top", query: str = "", limit: int = 5) -> str:
    topic = (topic or "top").lower().strip()
    if topic not in NEWS_FEEDS:
        raise ValueError(f"Pick a news topic from: {', '.join(NEWS_FEEDS)}.")
    items = await _feed(topic)
    if query:
        words = query.lower().split()
        items = [i for i in items if all(w in f"{i['title']} {i['summary']}".lower() for w in words)]
    if not items:
        return f"No {topic} headlines" + (f" mention {query}." if query else " right now.")
    return f"BBC {topic} headlines: " + " ".join(
        f"{n}. {i['title'].rstrip('.')}{'' if i['title'][-1:] in '?!' else '.'}"
        for n, i in enumerate(items[:limit], 1))


@ttl_cache(3600)
async def wikipedia(topic: str) -> str:
    """Intro of the best-matching English Wikipedia article (MediaWiki action API)."""
    r = await http().get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "format": "json", "formatversion": 2, "redirects": 1,
        "generator": "search", "gsrsearch": topic, "gsrlimit": 1,
        "prop": "extracts", "exintro": 1, "explaintext": 1, "exsentences": 4,
    })
    r.raise_for_status()
    pages = (r.json().get("query") or {}).get("pages") or []
    if not pages or not pages[0].get("extract"):
        return f"Wikipedia has no article matching {topic}."
    page = pages[0]
    return f"From Wikipedia, {page['title']}: {' '.join(page['extract'].split())}"


async def _assert_public(url: str) -> None:
    """Refuse non-http(s) URLs and hosts resolving to private/loopback/link-local IPs.

    ponytail: checks DNS before connecting, so DNS rebinding between check and
    connect is still possible; pin the resolved IP in a custom transport if that matters.
    """
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only public http(s) URLs can be fetched")
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror:
        raise ValueError(f"could not resolve {u.hostname}") from None
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("that address is not publicly reachable")


def html_to_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    return " ".join(html.unescape(page).split())


async def fetch_text(url: str, max_bytes: int = 2_000_000) -> tuple[str, str]:
    """Return (final_url, text). Follows up to 3 redirects, re-validating each hop."""
    client = http()
    for _ in range(4):
        await _assert_public(url)
        r = await client.get(url)
        if r.is_redirect and "location" in r.headers:
            url = str(r.url.join(r.headers["location"]))
            continue
        r.raise_for_status()
        break
    else:
        raise ValueError("too many redirects")
    body = r.content[:max_bytes].decode(r.encoding or "utf-8", errors="replace")
    ctype = r.headers.get("content-type", "")
    return url, (html_to_text(body) if "html" in ctype or "<html" in body[:500].lower() else body)
