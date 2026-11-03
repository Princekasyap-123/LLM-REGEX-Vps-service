"""Poora flow: duplicate check -> rule parser -> (LLM fallback) -> DB API save."""
import asyncio
import hashlib
import json

import httpx

from . import config, store
from .dbapi import AlreadyExists, ApiError, api, to_payload
from .logging_setup import log, mask
from .ollama_pool import NoKeyAvailable, llm_structure
from .parsing import (clean_for_llm, extract_email, extract_phone, norm, parse_shine)
from .schema import CANDIDATE_SCHEMA, GROUND_FIELDS, llm_schema


_wake = None          # asyncio.Event: submit set karta hai, idle workers wait karte hain


# ------------------------------------------------------------------ duplicate check
def _keys(phone, email):
    return [k for k in (phone, email.lower() if email else None) if k]


async def precheck(client, phone, email, tag, api_check=True):
    """True = email ya phone pehle se parsed/DB mein hai. ApiError = API se pata nahi chala."""
    keys = _keys(phone, email)
    if any(store.seen_has(k) for k in keys):
        log.info("%s phone %s / email %s already known (local cache)", tag, mask(phone), email)
        return True
    if not api_check:
        return False
    if await api.exists(client, phone, email, tag):
        for k in keys:
            store.seen_add(k, "db_api")
        log.info("%s phone %s / email %s already in DB (API says exists)", tag, mask(phone), email)
        return True
    return False


# ------------------------------------------------------------------ submit (API side)
async def submit_candidate(client, text, portal, created_by):
    """(http_status, response_dict). Turant jawab deta hai; parsing background mein hoti hai."""
    phone, phone_status = extract_phone(text)
    email = extract_email(text)

    if phone or email:
        try:
            if config.SUBMIT_API_CHECK:
                already = await precheck(client, phone, email, "submit")
            else:                                     # fast intake: sirf local cache, API check worker karega
                already = any(store.seen_has(k) for k in _keys(phone, email))
            if already:
                jid = store.add_audit("already_parsed", portal, created_by, phone_status)
                log.info("submit: phone %s email %s already parsed -> skipped (audit id %s)",
                         mask(phone), email, jid)
                return 200, {"status": "already_parsed", "message": "Candidate already parsed",
                             "phone": phone, "email": email, "id": jid}
        except ApiError as e:
            log.warning("submit: duplicate check failed (%s) -> queueing, worker will re-check", e)
    else:
        log.info("submit: no phone/email in text (%s), duplicate check skipped", phone_status)
    if not phone and config.REQUIRE_PHONE:
        return 422, {"status": "rejected", "message": "No valid phone found in text"}

    h = hashlib.sha256(f"{phone}|{norm(clean_for_llm(text))}".encode()).hexdigest()
    existing = store.find_by_hash(h)
    if existing:
        log.info("submit: same text already submitted as job %s (%s)", existing["id"], existing["status"])
        return 200, {"id": existing["id"], "status": existing["status"], "duplicate": True}

    jid = store.add_job(h, portal, created_by, text)
    log.info("job %s queued portal=%s len=%d phone=%s", jid, portal, len(text), mask(phone))
    if _wake is not None:
        _wake.set()                                   # idle worker ko turant jagao
    return 202, {"id": jid, "status": "queued"}


async def submit_many(client, items):
    """Batch intake: [(text, portal, created_by)] -> [{http, ...}] same order. Sab ek saath process."""
    async def one(it):
        try:
            code, body = await submit_candidate(client, *it)
        except Exception as e:
            log.exception("batch submit item failed")
            return {"http": 500, "status": "error", "message": f"{type(e).__name__}: {e}"}
        return {"http": code, **body}
    return await asyncio.gather(*(one(it) for it in items))


# ------------------------------------------------------------------ worker side
async def structure_job(job, client, tag):
    """(data, via, key_no) ya None (job skipped / already_parsed)."""
    text = job["raw_text"]
    phone, phone_status = extract_phone(text)
    clean = clean_for_llm(text)

    if len(clean) < config.MIN_TEXT_LEN:
        store.set_job(job["id"], status="skipped", error="text too short")
        log.info("%s skipped (too short: %d chars)", tag, len(clean))
        return None

    email = extract_email(text)

    async def already_known(api_check):               # True = skip (already_parsed set ho chuka)
        if not (phone or email):
            return False
        try:
            if await precheck(client, phone, email, tag, api_check):
                store.set_job(job["id"], status="already_parsed", error=None, raw_text=None)
                log.info("%s already parsed -> skipped (no parse, no save)", tag)
                return True
        except ApiError as e:
            if not config.CHECK_FAIL_OPEN:
                raise
            log.warning("%s duplicate check failed (%s) -> continuing (fail-open)", tag, e)
        return False

    # worker par dobara check (race/late jobs). "llm" mode: API check sirf LLM call se pehle
    # (parser free hai; duplicate ko save API "Already Exists" se rok deti hai) -> ek API call kam.
    early = config.WORKER_API_CHECK != "llm"
    if await already_known(api_check=early):
        return None

    data, via, key_no = None, None, None
    if not job["portal"] or "shine" in job["portal"].lower():
        parsed, ok = parse_shine(clean)
        if ok:
            data, via = parsed, "parser"
    if data is None:
        if not early and await already_known(api_check=True):
            return None
        data, key_no = await llm_structure(client, clean, llm_schema(), tag)
        text_n = norm(text)
        for f in GROUND_FIELDS:                       # jo text mein nahi hai wo null
            v = data.get(f)
            if isinstance(v, str) and norm(v) not in text_n:
                data[f] = None
        data["skills"] = [s for s in data.get("skills", [])
                          if isinstance(s, str) and norm(s) in text_n]
        via = "llm"

    for k, v in CANDIDATE_SCHEMA["properties"].items():
        data.setdefault(k, [] if v.get("type") == "array" else None)
    data.update({"phone": phone, "mobile": phone, "email": email})
    log.info("%s structured via=%s phone=%s(%s) name=%s", tag, via, mask(phone), phone_status,
             (data.get("name") or "")[:25])
    return data, via, key_no


async def process(job, client):
    jid, tag = job["id"], f"job {job['id']}"
    if job["result"]:                                 # pehle structure ho chuka, sirf save fail hua tha
        data, via, key_no = json.loads(job["result"]), job["via"], job["key_used"]
        log.info("%s reusing earlier structured result (no LLM call)", tag)
    else:
        out = await structure_job(job, client, tag)
        if out is None:
            return
        data, via, key_no = out
        store.set_job(jid, result=json.dumps(data), via=via, key_used=key_no)

    if not config.SENIORS_URL:                        # save API abhi nahi: result rakho, baad mein save hoga
        store.set_job(jid, status="parsed", error="waiting for SENIORS_URL", raw_text=None)
        log.info("%s parsed, held (SENIORS_URL not set)", tag)
        return

    if not data.get("phone"):                         # save API ko mobile chahiye ("Mobile can not be empty")
        store.set_job(jid, status="skipped", error="no phone: save API needs mobile", raw_text=None)
        log.warning("%s skipped: no phone number, save API requires mobile", tag)
        return

    if not (data.get("name") or "").strip():          # save API ko name bhi chahiye ("Column 'name' cannot be null")
        store.set_job(jid, status="skipped", error="no name parsed: save API needs name")
        log.warning("%s skipped: no name parsed, save API requires name", tag)
        return

    payload = to_payload(data, job["portal"], job["created_by"])
    if config.LOG_PAYLOAD:
        log.info("%s SAVE PAYLOAD (via=%s) ->\n%s", tag, via,
                 json.dumps(payload, indent=2, ensure_ascii=False))
    try:
        resp = await api.save(client, payload, tag)
    except AlreadyExists as e:
        # Save API ne 'User Already Exists' kaha. Seedha already_parsed mat maano: ho sakta hai pehle
        # ka fail hua save adhoora record chhod gaya ho. Check API se confirm karo.
        # Check khud fail ho to ApiError upar jaata hai -> worker retry karega.
        confirmed = await api.exists(client, data.get("phone"), data.get("email"), tag)
        if confirmed:
            store.set_job(jid, status="already_parsed", error=None, raw_text=None)
            for k in _keys(data.get("phone"), data.get("email")):
                store.seen_add(k, "db_api")
            log.info("%s already in DB (save API: %s; check API confirmed) -> marked already_parsed", tag, e)
        else:
            store.set_job(jid, status="failed",
                          error="save API says 'already exists' but check API says not found: "
                                "possible half-created record, check DB manually")
            log.error("%s save API says already exists (%s) but check API does NOT find phone=%s -> "
                      "marked failed, NOT cached (possible orphan record in DB)",
                      tag, e, mask(data.get("phone")))
        return
    if config.LOG_PAYLOAD:
        log.info("%s SAVE RESPONSE <- %s", tag, json.dumps(resp, ensure_ascii=False)[:1000])
    store.set_job(jid, status="done", db_response=json.dumps(resp)[:2000], error=None)
    for k in _keys(data.get("phone"), data.get("email")):
        store.seen_add(k, "saved")
    log.info("%s DONE: saved to DB API (record id=%s)", tag, (resp.get("data") or {}).get("id") if isinstance(resp.get("data"), dict) else resp)


async def worker(n):
    limits = httpx.Limits(max_connections=config.API_MAX_CONCURRENCY + 4, max_keepalive_connections=8)
    async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT, limits=limits) as client:
        while True:
            _wake.clear()                             # clear pehle, claim baad mein: wake miss nahi hota
            job = store.claim_job()
            if not job:
                try:                                  # submit jagayega; timeout retry/next_try jobs ke liye
                    await asyncio.wait_for(_wake.wait(), 2)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await process(job, client)
            except NoKeyAvailable as e:
                import time
                store.set_job(job["id"], status="queued", next_try=time.time() + 300,
                              error=f"waiting for Ollama key: {e}")
                log.warning("job %s: no Ollama key free (%s), retry in 5 min", job["id"], e)
            except ApiError as e:
                store.retry_or_fail(job, str(e), "DB API")
            except Exception as e:
                log.exception("job %s crashed", job["id"])
                store.retry_or_fail(job, f"{type(e).__name__}: {e}", "crash")


async def housekeeping():
    while True:
        await asyncio.sleep(3600)
        a, b, c = store.purge()
        if a or b or c:
            log.info("housekeeping: purged raw text of %d jobs, %d audit rows, %d cached phones", a, b, c)


def start_background():
    global _wake
    _wake = asyncio.Event()
    tasks = [asyncio.create_task(worker(i)) for i in range(config.WORKERS)]
    tasks.append(asyncio.create_task(housekeeping()))
    return tasks
