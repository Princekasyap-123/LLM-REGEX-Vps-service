#!/usr/bin/env python3
"""Python client: apne scraper se is service ko call karne ke liye.

Use:
    export STRUCTURER_URL=https://ai.yourdomain.com
    export STRUCTURER_KEY=<SERVICE_API_KEY>
    python scripts/client.py check 9999368246
    python scripts/client.py submit profile.txt --portal recruiter.shine.com --created-by 1
    python scripts/client.py job 41
"""
import argparse
import json
import os
import sys

import httpx

URL = os.getenv("STRUCTURER_URL", "http://127.0.0.1:8000").rstrip("/")
KEY = os.getenv("STRUCTURER_KEY", "")


def _client():
    return httpx.Client(base_url=URL, headers={"x-api-key": KEY}, timeout=30)


def check(phone: str) -> dict:
    """Scrape se pehle phone check karo: {'exists': True/False, 'status': 'already_parsed'|'new'}."""
    with _client() as c:
        r = c.post("/check", json={"phone": phone})
        r.raise_for_status()
        return r.json()


def submit(text: str, portal: str = "recruiter.shine.com", created_by: int = 1) -> dict:
    """Raw text bhejo. status: already_parsed | queued | (duplicate:true)."""
    with _client() as c:
        r = c.post("/submit", json={"text": text, "portal": portal, "createdBy": created_by})
        if r.status_code not in (200, 202):
            r.raise_for_status()
        return r.json()


def job(job_id: int) -> dict:
    with _client() as c:
        r = c.get(f"/jobs/{job_id}")
        r.raise_for_status()
        return r.json()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").add_argument("phone")
    s = sub.add_parser("submit")
    s.add_argument("file")
    s.add_argument("--portal", default="recruiter.shine.com")
    s.add_argument("--created-by", type=int, default=1)
    sub.add_parser("job").add_argument("id", type=int)
    a = p.parse_args()

    if a.cmd == "check":
        out = check(a.phone)
    elif a.cmd == "submit":
        out = submit(open(a.file, encoding="utf-8").read(), a.portal, a.created_by)
    else:
        out = job(a.id)
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()
