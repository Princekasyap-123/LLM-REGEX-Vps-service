"""Ollama cloud key pool: pacing, soft budget, 429 fallback, cooldown."""
import asyncio
import json
import time
from collections import deque

import httpx

from . import config
from .logging_setup import log
from .parsing import parse_json
from .schema import SYSTEM_PROMPT


class NoKeyAvailable(Exception):
    """Saari keys busy / cooldown / budget mein. Job baad mein retry hogi."""


class Key:
    def __init__(self, idx, secret):
        self.idx, self.secret = idx, secret
        self.blocked_until = 0.0
        self.next_ok = 0.0
        self.busy = False
        self.calls = deque()
        self.ok = self.r429 = self.errors = 0

    def recent(self, now):
        while self.calls and now - self.calls[0] > config.KEY_SESSION_WINDOW:
            self.calls.popleft()
        return len(self.calls)

    def under_budget(self, now):
        return (not config.KEY_SESSION_BUDGET) or self.recent(now) < config.KEY_SESSION_BUDGET

    def can_ever_serve(self, now):
        return now >= self.blocked_until and self.under_budget(now)

    def available(self, now):
        return self.can_ever_serve(now) and not self.busy and now >= self.next_ok


keys = [Key(i, k) for i, k in enumerate(config.OLLAMA_API_KEYS)]


async def _acquire(tried):
    deadline = time.time() + config.WAIT_FOR_KEY_SECONDS
    while time.time() < deadline:
        now = time.time()
        free = [k for k in keys if k.idx not in tried and k.available(now)]
        if free:
            k = min(free, key=lambda k: k.recent(now))     # sabse kam use hui key
            k.busy = True                                  # await nahi -> race nahi
            return k
        if not any(k.idx not in tried and k.can_ever_serve(now) for k in keys):
            return None
        await asyncio.sleep(0.25)
    return None


def _release(k):
    k.busy = False
    k.next_ok = time.time() + config.KEY_MIN_INTERVAL


async def _call(client, key, schema, text):
    system = SYSTEM_PROMPT + ("\nSchema:\n" + json.dumps(schema) if config.SCHEMA_IN_PROMPT else "")
    payload = {"model": config.OLLAMA_MODEL, "stream": False, "think": "low",
               "options": {"temperature": 0}, "format": schema,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": text}]}
    return await client.post(config.OLLAMA_URL, json=payload,
                             headers={"Authorization": f"Bearer {key}"})


async def llm_structure(client, text, schema, tag):
    """(data, key_number). Har key ek baar try hoti hai; sab fail -> NoKeyAvailable."""
    tried, last = set(), "no key available"
    while True:
        k = await _acquire(tried)
        if k is None:
            raise NoKeyAvailable(last)
        tried.add(k.idx)
        k.calls.append(time.time())
        try:
            r = await _call(client, k.secret, schema, text)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            k.errors += 1
            k.blocked_until = time.time() + 30
            last = f"key{k.idx + 1} network: {e}"
            log.warning("%s %s", tag, last)
            continue
        finally:
            _release(k)

        if r.status_code == 429:
            ra = r.headers.get("retry-after", "")
            k.blocked_until = time.time() + (int(ra) if ra.isdigit() else config.RATE_LIMIT_COOLDOWN)
            k.r429 += 1
            last = f"key{k.idx + 1} 429"
            log.warning("%s key #%d hit 429 after %d ok calls -> fallback", tag, k.idx + 1, k.ok)
            continue
        if r.status_code in (401, 403):
            k.blocked_until = time.time() + 3600
            last = f"key{k.idx + 1} auth {r.status_code}"
            log.error("%s %s (key disabled for 1h)", tag, last)
            continue
        if r.status_code >= 500:
            k.blocked_until = time.time() + 30
            last = f"key{k.idx + 1} {r.status_code}"
            log.warning("%s %s", tag, last)
            continue
        if r.status_code != 200:
            raise RuntimeError(f"ollama {r.status_code}: {r.text[:200]}")
        try:
            data = parse_json(r.json()["message"]["content"])
        except Exception:
            last = f"key{k.idx + 1} bad json"
            log.warning("%s %s", tag, last)
            continue
        k.ok += 1
        log.info("%s Ollama key #%d ok", tag, k.idx + 1)
        return data, k.idx + 1


def pool_stats():
    now = time.time()
    return [{"key": k.idx + 1, "ok": k.ok, "429s": k.r429, "errors": k.errors,
             "calls_in_window": k.recent(now),
             "blocked_for_s": max(0, int(k.blocked_until - now))} for k in keys]
