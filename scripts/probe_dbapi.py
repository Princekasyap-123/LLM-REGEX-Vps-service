"""DB API response time probe (read-only: sirf duplicate-check endpoint, save nahi hota).

    python scripts/probe_dbapi.py [calls=30] [concurrency=1,2,4,6,8]

Har concurrency level par avg/p50/p95/max latency aur 429 count dikhata hai -> API_MAX_CONCURRENCY
aur API_MIN_INTERVAL isi se chuno (jahan p95 badhne ya 429 aane lage, usse ek level neeche).
"""
import asyncio
import statistics
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from structurer import config  # noqa: E402


async def level(client, url, calls, conc):
    sem, lat, codes = asyncio.Semaphore(conc), [], []

    async def one(i):
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await client.post(url, json={"phone": f"90000{i:05d}"})
                codes.append(r.status_code)
            except Exception as e:
                codes.append(type(e).__name__)
            lat.append(time.perf_counter() - t0)

    t0 = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(calls)))
    wall = time.perf_counter() - t0
    lat.sort()
    print(f"conc={conc:<2} avg={statistics.mean(lat):.2f}s p50={lat[len(lat)//2]:.2f}s "
          f"p95={lat[int(len(lat)*.95)-1]:.2f}s max={lat[-1]:.2f}s  "
          f"throughput={calls/wall:.1f}/s  429s={codes.count(429)} errs={sum(c != 200 for c in codes)}")


async def main():
    url = config.DUPLICATE_CHECK_API_URL
    if not url:
        sys.exit("DUPLICATE_CHECK_API_URL not set")
    calls = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    levels = [int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "1,2,4,6,8").split(",")]
    async with httpx.AsyncClient(timeout=config.DUPLICATE_CHECK_TIMEOUT) as c:
        await c.post(url, json={"phone": "9000000000"})          # warm-up (TLS/keepalive)
        for n in levels:
            await level(c, url, calls, n)


asyncio.run(main())
