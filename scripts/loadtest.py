"""Load/latency test for the tool-calls webhook. Voice tools must answer fast: every second of
tool latency is a second of dead air on the call.

  VAPI_WEBHOOK_SECRET=... python -m scripts.loadtest [--url http://localhost:8000] \\
      [--tool get_weather] [-n 200] [-c 20] [--max-p95 1.5] [--cold]

--cold asks for the weather in a different city on every request (up to len(COLD_CITIES)),
so every request is an uncached lookup: the worst case a caller can hit.

Each request is a realistic Vapi `tool-calls` message with a unique toolCallId. Callers are
spread over many fake, non-allowlisted numbers so the per-caller rate limit doesn't skew
results, which also means only read-only tools can be tested. Exits 1 if p95 is over budget
or any request failed.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import statistics
import sys
import time
import uuid

import httpx

SAMPLE_ARGS = {
    "get_weather": [{"location": c} for c in ("London", "Tokyo", "San Francisco", "Mumbai", "Sydney")],
    "convert": [{"amount": 100, "from_unit": "USD", "to_unit": "EUR"},
                {"amount": 5, "from_unit": "km", "to_unit": "mi"},
                {"amount": 70, "from_unit": "F", "to_unit": "C"}],
    "wikipedia": [{"topic": t} for t in ("Ada Lovelace", "Alan Turing", "Grace Hopper")],
    "news_headlines": [{"topic": t} for t in ("top", "technology", "business")],
    "web_search": [{"query": "vapi voice ai"}],
}


COLD_CITIES = (
    "Accra;Addis Ababa;Almaty;Amman;Amsterdam;Ankara;Asuncion;Athens;Baku;Bamako;Bangkok;"
    "Beirut;Belgrade;Berlin;Bern;Bogota;Brasilia;Bratislava;Brussels;Bucharest;Budapest;"
    "Buenos Aires;Cairo;Canberra;Caracas;Colombo;Copenhagen;Dakar;Dhaka;Doha;Dublin;Hanoi;"
    "Harare;Havana;Helsinki;Islamabad;Jakarta;Kabul;Kampala;Kathmandu;Khartoum;Kigali;Kyiv;"
    "La Paz;Lima;Lisbon;Ljubljana;Luanda;Lusaka;Madrid;Managua;Manila;Maputo;Minsk;Montevideo;"
    "Muscat;Nairobi;Oslo;Ottawa;Panama City;Paramaribo;Phnom Penh;Prague;Quito;Rabat;Reykjavik;"
    "Riga;Riyadh;Rome;Santiago;Sarajevo;Seoul;Skopje;Sofia;Stockholm;Taipei;Tallinn;Tashkent;"
    "Tbilisi;Tegucigalpa;Tirana;Tunis;Ulaanbaatar;Valletta;Vienna;Vilnius;Warsaw;Wellington;"
    "Windhoek;Yerevan;Zagreb"
).split(";")


def payload(tool: str, args: dict, i: int) -> dict:
    return {"message": {
        "timestamp": int(time.time() * 1000),
        "type": "tool-calls",
        "call": {"id": f"loadtest-{i // 10}", "type": "inboundPhoneCall", "status": "in-progress",
                 "customer": {"number": f"+1555{i:07d}"}},
        "toolCallList": [{"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                          "function": {"name": tool, "arguments": args}}],
    }}


def pct(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile."""
    return sorted_values[max(0, math.ceil(p / 100 * len(sorted_values)) - 1)]


async def run(url: str, secret: str, tool: str, n: int, concurrency: int,
              cold: bool = False) -> tuple[list[float], list[str], int]:
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors: list[str] = []
    deferred = 0
    samples = [{"location": c} for c in COLD_CITIES] if cold else SAMPLE_ARGS[tool]

    async with httpx.AsyncClient(timeout=25, headers={"X-Vapi-Secret": secret}) as client:
        async def one(i: int) -> None:
            async with sem:
                start = time.perf_counter()
                try:
                    r = await client.post(f"{url}/vapi/webhook", json=payload(tool, samples[i % len(samples)], i))
                    elapsed = time.perf_counter() - start
                    result = r.json()["results"][0] if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    elapsed, result = time.perf_counter() - start, {"error": repr(e)}
                latencies.append(elapsed)
                if "error" in result:
                    errors.append(str(result["error"])[:120])
                elif str(result.get("result", "")).startswith("STILL WORKING"):
                    nonlocal deferred
                    deferred += 1

        await asyncio.gather(*(one(i) for i in range(n)))
    return latencies, errors, deferred


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--tool", default="get_weather", choices=sorted(SAMPLE_ARGS))
    p.add_argument("-n", type=int, default=200, help="total requests")
    p.add_argument("-c", type=int, default=20, help="concurrency")
    p.add_argument("--max-p95", type=float, default=1.5, help="latency budget in seconds")
    p.add_argument("--cold", action="store_true", help="a new city per request (get_weather only)")
    a = p.parse_args(argv)
    if a.cold:
        a.tool, a.n = "get_weather", min(a.n, len(COLD_CITIES))
    secret = os.getenv("VAPI_WEBHOOK_SECRET")
    if not secret:
        sys.exit("Set VAPI_WEBHOOK_SECRET to the server's secret.")

    t0 = time.perf_counter()
    lat, errors, deferred = asyncio.run(run(a.url.rstrip("/"), secret, a.tool, a.n, a.c, a.cold))
    wall = time.perf_counter() - t0
    s = sorted(lat)

    def ms(x: float) -> str:
        return f"{x * 1000:.0f}ms"
    print(f"{a.tool}{' (cold)' if a.cold else ''}: {len(lat)} requests, concurrency {a.c}, {wall:.1f}s, {len(lat) / wall:.1f} req/s")
    print(f"latency min {ms(s[0])}  p50 {ms(statistics.median(s))}  p95 {ms(pct(s, 95))}  "
          f"p99 {ms(pct(s, 99))}  max {ms(s[-1])}")
    print(f"errors: {len(errors)}" + (f" (first: {errors[0]})" if errors else "")
          + f"  deferred (holding line): {deferred}")
    ok = not errors and pct(s, 95) <= a.max_p95
    print("PASS" if ok else f"FAIL (budget p95 <= {ms(a.max_p95)}, no errors)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
