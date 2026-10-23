"""Concurrent load test against a running Plexis server over MCP (Streamable HTTP)."""

from __future__ import annotations

import asyncio
import statistics
import time
from datetime import timedelta

import httpx
import httpx2

from qed.config import load
from qed.domain import world

CITIES = [("Seattle", "Portland"), ("Boston", "New York"), ("Austin", "Denver"), ("Los Angeles", "San Diego")]


async def run(n: int, concurrency: int) -> dict[str, float]:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    s = load()
    base = f"http://{s.host}:{s.port}"
    token = httpx.post(
        f"{base}/oauth/token",
        data={"grant_type": "client_credentials", "scope": "mcp:service"},
        auth=("alexa-plus-simulator", "dev-secret-change-me"),
    ).json()["access_token"]
    day = world.today() + timedelta(days=25)
    latencies: list[float] = []
    errors = 0
    sem = asyncio.Semaphore(concurrency)

    async def one(i: int) -> None:
        nonlocal errors
        o, d = CITIES[i % len(CITIES)]
        req = f"{1 + i % 4} nights from {o} to {d} on {day.strftime('%B')} {day.day + i % 5} for {1 + i % 3} under $3,000"
        async with sem:
            client = httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(60)
            )
            async with (
                streamable_http_client(f"{base}/mcp", http_client=client) as st,
                ClientSession(st[0], st[1]) as sess,
            ):
                await sess.initialize()
                t0 = time.perf_counter()
                res = await sess.call_tool("plan_trip", {"request": req})
                latencies.append((time.perf_counter() - t0) * 1000)
                if res.is_error:
                    errors += 1

    t0 = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(n)))
    wall = time.perf_counter() - t0
    latencies.sort()
    out = {
        "requests": n,
        "concurrency": concurrency,
        "errors": errors,
        "throughputRps": round(n / wall, 2),
        "p50Ms": round(statistics.median(latencies), 1),
        "p95Ms": round(latencies[int(len(latencies) * 0.95) - 1], 1),
    }
    print(out)
    return out
