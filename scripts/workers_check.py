"""Check that shared state really is shared across `uvicorn --workers N`.

  python -m scripts.workers_check [--workers 4] [-n 40] [--limit 5] [--shared-state sqlite]

Starts a throwaway server (temp DB, dry-run, no network needed) with N workers and sends
tool calls over a fresh TCP connection each (`Connection: close`, several at once), so the
OS hands them to different workers instead of one keep-alive connection pinning a single
worker. The worker pid in each JSON log line shows who served what. It then checks:

- distribution: at least two workers answered (otherwise nothing below proves anything);
- rate limit: exactly --limit of the n calls from one caller were allowed, across workers;
- confirmation: create_event read-backs and their confirmed=true repeats, some answered by
  different workers, each create their event exactly once.

With --shared-state memory the rate-limit check is expected to fail: each worker counts on
its own. Exits 1 on any failed check.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path

import httpx

SECRET = "workers-check-" + uuid.uuid4().hex[:8]
CALLER = "+15550000001"
PAIRS = 12
CONFIRMERS = [f"+155500001{i:02d}" for i in range(PAIRS)]  # own caller each: under the limit


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def message(caller: str, name: str, args: dict, call_id: str = "workers-check") -> dict:
    return {"message": {"type": "tool-calls",
                        "call": {"id": call_id, "type": "inboundPhoneCall", "customer": {"number": caller}},
                        "toolCallList": [{"id": f"call_{uuid.uuid4().hex[:24]}", "type": "function",
                                          "function": {"name": name, "arguments": args}}]}}


async def send(url: str, body: dict, rid: str) -> str:
    # A new client per request: no keep-alive, so each request is a new accept().
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{url}/vapi/webhook", json=body, headers={
            "X-Vapi-Secret": SECRET, "X-Request-ID": rid, "Connection": "close"})
    r.raise_for_status()
    out = r.json()["results"][0]
    return out.get("result") or out.get("error", "")


def pids_by_request(log: Path) -> dict[str, int]:
    out = {}
    for line in log.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # uvicorn's own plain-text lines
        if rec.get("request_id") and rec.get("pid"):
            out[rec["request_id"]] = rec["pid"]
    return out


async def run_checks(url: str, n: int, limit: int, log: Path, db_path: str) -> list[tuple[str, bool, str]]:
    rids = [f"wc-{i}" for i in range(n)]
    convert = {"amount": 5, "from_unit": "km", "to_unit": "mi"}
    sem = asyncio.Semaphore(8)

    async def one(rid: str) -> str:
        async with sem:
            return await send(url, message(CALLER, "convert", convert), rid)

    answers = await asyncio.gather(*(one(r) for r in rids))
    allowed = sum(not a.startswith("Too many requests") for a in answers)

    async def pair(i: int) -> bool:
        event = {"title": f"Workers check {i}", "starts_at": "2030-01-01T09:00"}
        first = await send(url, message(CONFIRMERS[i], "create_event", event), f"wc-ask-{i}")
        second = await send(url, message(CONFIRMERS[i], "create_event", {**event, "confirmed": True}),
                            f"wc-yes-{i}")
        return first.startswith("CONFIRMATION REQUIRED") and second.startswith("Added")

    # Pairs run side by side so busy workers push the confirms onto other workers.
    confirmed = sum(await asyncio.gather(*(pair(i) for i in range(PAIRS))))
    with sqlite3.connect(db_path) as conn:
        counts = [r[0] for r in conn.execute("SELECT COUNT(*) FROM events GROUP BY title")]

    await asyncio.sleep(0.5)  # let the workers flush their last log lines
    pids = pids_by_request(log)
    served = Counter(pids[r] for r in rids if r in pids)
    crossed = sum(pids.get(f"wc-ask-{i}") != pids.get(f"wc-yes-{i}") for i in range(PAIRS))
    return [
        ("distribution", len(served) >= 2,
         f"{len(served)} workers answered: " + ", ".join(f"pid {p}: {c}" for p, c in sorted(served.items()))),
        ("rate limit", allowed == limit, f"{allowed} of {n} calls allowed with a limit of {limit}/min"),
        ("confirmation", confirmed == PAIRS and counts == [1] * PAIRS and crossed >= 1,
         f"{confirmed}/{PAIRS} read-back + confirm pairs ran once each, {crossed} of them "
         f"confirmed on a different worker than the read-back"),
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("-n", type=int, default=40, help="tool calls from one caller")
    p.add_argument("--limit", type=int, default=5, help="RATE_LIMIT_PER_MINUTE for the server")
    p.add_argument("--shared-state", choices=("sqlite", "memory"), default="sqlite")
    a = p.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="workers-check-"))
    db_path, log = str(tmp / "check.db"), tmp / "server.log"
    port = free_port()
    env = {**os.environ, "DB_PATH": db_path, "SHARED_STATE": a.shared_state,
           "RATE_LIMIT_PER_MINUTE": str(a.limit), "VAPI_WEBHOOK_SECRET": SECRET,
           "VAPI_HMAC_SECRET": "", "ALLOWED_CALLERS": ",".join([CALLER, *CONFIRMERS]), "DRY_RUN": "true",
           "WEATHER_PREFETCH": "", "ADMIN_PASSWORD": "", "ANTHROPIC_API_KEY": "", "TIMEZONE": "UTC"}
    url = f"http://127.0.0.1:{port}"
    with log.open("w") as out:
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port),
             "--workers", str(a.workers), "--no-access-log"],
            env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 30
        while log.read_text().count('"server.started"') < a.workers:
            if server.poll() is not None or time.time() > deadline:
                print(log.read_text()[-2000:], file=sys.stderr)
                print("server did not start", file=sys.stderr)
                return 1
            time.sleep(0.2)
        results = asyncio.run(run_checks(url, a.n, a.limit, log, db_path))
    finally:
        server.terminate()
        server.wait(timeout=15)

    print(f"uvicorn --workers {a.workers}, SHARED_STATE={a.shared_state}")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
