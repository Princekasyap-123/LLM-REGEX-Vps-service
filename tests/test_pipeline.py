import asyncio
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import sample
from structurer import pipeline, store
from structurer.dbapi import ApiError, api
from structurer.main import app


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    store.init(str(tmp_path / "t.db"))
    yield


class FakeDb:
    """DB API ka nakli version: known phones + saved records."""
    def __init__(self, known=()):
        self.known = set(known)
        self.saved = []
        self.fail_save = 0
        self.check_calls = 0

    async def exists(self, client, phone, email, tag):
        self.check_calls += 1
        return phone in self.known or (email or "").lower() in self.known

    async def save(self, client, payload, tag):
        if self.fail_save:
            self.fail_save -= 1
            raise ApiError("DB API rejected: 500 boom")
        self.saved.append(payload)
        self.known.add(payload["phone"])
        return {"status": True, "data": {"id": 1000 + len(self.saved)}}


@pytest.fixture
def fake(monkeypatch):
    f = FakeDb()
    monkeypatch.setattr(api, "exists", f.exists)
    monkeypatch.setattr(api, "save", f.save)
    return f


def run(coro):
    return asyncio.run(coro)


def test_already_parsed_phone_is_not_queued(fake):
    fake.known.add("9999368246")
    async def go():
        async with httpx.AsyncClient() as c:
            return await pipeline.submit_candidate(c, sample("ritesh.txt"), "recruiter.shine.com", 1)
    code, body = run(go())
    assert code == 200
    assert body["status"] == "already_parsed" and body["message"] == "Candidate already parsed"
    assert store.rows("SELECT COUNT(*) c FROM jobs WHERE status='queued'")[0]["c"] == 0


def test_new_candidate_parsed_and_saved(fake):
    async def go():
        async with httpx.AsyncClient() as c:
            code, body = await pipeline.submit_candidate(c, sample("ritesh.txt"), "recruiter.shine.com", 1)
            assert code == 202 and body["status"] == "queued"
            job = store.claim_job()
            await pipeline.process(job, c)
            return body["id"]
    jid = run(go())
    job = store.rows("SELECT * FROM jobs WHERE id=?", (jid,))[0]
    assert job["status"] == "done" and job["via"] == "parser" and job["key_used"] is None
    saved = fake.saved[0]
    assert saved["mobile"] == saved["phone"] == "9999368246" and saved["portal"] == "recruiter.shine.com"
    assert saved["createdBy"] == 1
    assert saved["name"] == "Ritesh Rathi" and saved["currentLocation"] == "Delhi"
    assert saved["currentSalary"] == 420000 and saved["totalExperience"] == 6
    assert saved["preferredLocation"] == ["Gurugram", "Delhi"]
    assert saved["educations"][0] == {"educationTitle": "10+2 or Below (Arts and Humanities)",
                                      "educationInstitute": "Delhi University - Other",
                                      "startYear": None, "endYear": 2014}
    assert saved["highestQualificationYear"] == 2014
    assert store.seen_has("9999368246")            # ab local cache se bhi pata chalega


def test_second_submit_after_done_skips_without_api_call(fake):
    async def go():
        async with httpx.AsyncClient() as c:
            await pipeline.submit_candidate(c, sample("ritesh.txt"), "recruiter.shine.com", 1)
            await pipeline.process(store.claim_job(), c)
            before = fake.check_calls
            code, body = await pipeline.submit_candidate(c, sample("ritesh.txt"), "recruiter.shine.com", 1)
            return code, body, fake.check_calls - before
    code, body, extra_calls = run(go())
    assert body["status"] == "already_parsed" and extra_calls == 0     # local cache se


def test_worker_rechecks_and_skips_if_added_meanwhile(fake):
    async def go():
        async with httpx.AsyncClient() as c:
            await pipeline.submit_candidate(c, sample("ritesh.txt"), "recruiter.shine.com", 1)
            fake.known.add("9999368246")           # queue mein aane ke baad kisi aur ne insert kar diya
            await pipeline.process(store.claim_job(), c)
    run(go())
    assert store.rows("SELECT status FROM jobs")[0]["status"] == "already_parsed"
    assert fake.saved == []


def test_no_save_url_holds_then_saves_after_restart(fake, monkeypatch):
    from structurer import config
    monkeypatch.setattr(config, "SENIORS_URL", "")
    async def go():
        async with httpx.AsyncClient() as c:
            await pipeline.submit_candidate(c, sample("ritesh.txt"), "recruiter.shine.com", 1)
            await pipeline.process(store.claim_job(), c)
            assert store.rows("SELECT status FROM jobs")[0]["status"] == "parsed"
            assert fake.saved == []
            monkeypatch.setattr(config, "SENIORS_URL", "http://dbapi.test/save")
            store.recover()                                  # restart
            await pipeline.process(store.claim_job(), c)
    run(go())
    assert store.rows("SELECT status FROM jobs")[0]["status"] == "done" and len(fake.saved) == 1


def test_save_failure_retries_without_second_parse(fake, monkeypatch):
    fake.fail_save = 1
    calls = {"llm": 0}
    async def fake_llm(client, text, schema, tag):
        calls["llm"] += 1
        return {"name": "Ritesh Rathi", "skills": ["sales"]}, 1
    monkeypatch.setattr(pipeline, "llm_structure", fake_llm)

    async def go():
        async with httpx.AsyncClient() as c:
            await pipeline.submit_candidate(c, sample("ritesh.txt"), "other-portal.com", 1)
            job = store.claim_job()
            try:
                await pipeline.process(job, c)
            except ApiError as e:
                store.retry_or_fail(job, str(e), "DB API")
            store.run("UPDATE jobs SET next_try=0")
            await pipeline.process(store.claim_job(), c)
    run(go())
    job = store.rows("SELECT * FROM jobs")[0]
    assert job["status"] == "done" and job["via"] == "llm" and job["attempts"] == 1
    assert calls["llm"] == 1                         # LLM sirf ek baar chala


def test_check_fails_closed_then_open(fake, monkeypatch):
    async def boom(client, phone, email, tag):
        raise ApiError("check API 500")
    monkeypatch.setattr(api, "exists", boom)
    async def go():
        async with httpx.AsyncClient() as c:
            code, body = await pipeline.submit_candidate(c, sample("ritesh.txt"), "recruiter.shine.com", 1)
            assert code == 202                       # submit par API down: queue ho gaya
            with pytest.raises(ApiError):
                await pipeline.process(store.claim_job(), c)    # fail-closed: duplicate nahi banega
    run(go())


def test_short_text_skipped(fake):
    async def go():
        async with httpx.AsyncClient() as c:
            await pipeline.submit_candidate(c, "9999368246\nRitesh", "recruiter.shine.com", 1)
            await pipeline.process(store.claim_job(), c)
    run(go())
    assert store.rows("SELECT status FROM jobs")[0]["status"] == "skipped"


def test_http_end_to_end(fake):
    hdr = {"x-api-key": "test-key"}
    with TestClient(app) as client:
        assert client.post("/submit", json={"text": "x"}).status_code == 401
        r = client.post("/check", json={"phone": "+91 99993 68246"}, headers=hdr)
        assert r.json()["status"] == "new"
        r = client.post("/submit", json={"text": sample("mohammed.txt"), "portal": "recruiter.shine.com",
                                         "createdBy": 1}, headers=hdr)
        assert r.status_code == 202
        jid = r.json()["id"]
        for _ in range(40):                          # background worker ka intezar
            j = client.get(f"/jobs/{jid}", headers=hdr).json()
            if j["status"] in ("done", "failed", "skipped"):
                break
            time.sleep(0.25)
        assert j["status"] == "done", j
        st = client.get("/stats", headers=hdr).json()
        assert st["done_by_method"].get("parser") == 1
        assert "queued" in client.get("/logs?lines=500", headers=hdr).text
        assert client.get("/dashboard").status_code == 200
        fake.known.add("9999368246")
        r = client.post("/check", json={"phone": "9999368246"}, headers=hdr)
        assert r.json() == {"phone": "9999368246", "email": None, "exists": True, "status": "already_parsed",
                            "message": "Candidate already parsed"}
        r = client.post("/submit", json={"text": sample("ritesh.txt")}, headers=hdr)
        assert r.status_code == 200 and r.json()["status"] == "already_parsed"
