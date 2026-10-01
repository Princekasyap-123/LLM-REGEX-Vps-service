"""3-key fallback: 429 par agli key, sab 429 par NoKeyAvailable."""
import asyncio
import json

import httpx
import pytest

from structurer import ollama_pool as op


@pytest.fixture(autouse=True)
def reset_keys():
    for k in op.keys:
        k.blocked_until = k.next_ok = 0.0
        k.busy = False
        k.calls.clear()
        k.ok = k.r429 = k.errors = 0


def ok_resp():
    return httpx.Response(200, json={"message": {"content": json.dumps({"name": "X"})}})


def run(handler):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await op.llm_structure(c, "text", {"type": "object"}, "t")
    return asyncio.run(go())


def test_falls_back_to_next_key_on_429():
    def handler(req):
        return httpx.Response(429) if req.headers["authorization"] == "Bearer k1" else ok_resp()
    data, key_no = run(handler)
    assert data == {"name": "X"} and key_no == 2
    assert op.keys[0].r429 == 1 and op.keys[0].blocked_until > 0


def test_bad_key_is_disabled_and_next_used():
    def handler(req):
        return httpx.Response(401) if req.headers["authorization"] == "Bearer k1" else ok_resp()
    _, key_no = run(handler)
    assert key_no == 2 and op.keys[0].blocked_until - op.keys[0].next_ok > 3000


def test_all_keys_limited_raises():
    with pytest.raises(op.NoKeyAvailable):
        run(lambda req: httpx.Response(429))


def test_bad_json_tries_next_key():
    def handler(req):
        if req.headers["authorization"] == "Bearer k1":
            return httpx.Response(200, json={"message": {"content": "not json at all"}})
        return ok_resp()
    assert run(handler)[1] == 2


def test_blocked_key_is_skipped_next_time():
    op.keys[0].blocked_until = 9e12
    seen = []
    def handler(req):
        seen.append(req.headers["authorization"])
        return ok_resp()
    run(handler)
    assert "Bearer k1" not in seen
