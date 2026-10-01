"""HTTP endpoints."""
import os
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import config, pipeline, store
from .dbapi import ApiError, api
from .ollama_pool import pool_stats
from .parsing import normalize_phone

router = APIRouter()


def auth(x_api_key: str = Header(None)):
    if x_api_key != config.SERVICE_API_KEY:
        raise HTTPException(401, "Invalid API key")


class SubmitReq(BaseModel):
    text: str
    createdBy: int | None = None
    portal: str | None = None


class CheckReq(BaseModel):
    phone: str | None = None
    email: str | None = None


@router.post("/submit", dependencies=[Depends(auth)])
async def submit(req: SubmitReq, request: Request):
    """Raw text bhejo. Phone DB mein hai -> 'already_parsed'; nahi -> queue (202)."""
    code, body = await pipeline.submit_candidate(request.app.state.client, req.text,
                                                 req.portal, req.createdBy)
    return JSONResponse(status_code=code, content=body)


@router.post("/check", dependencies=[Depends(auth)])
async def check(req: CheckReq, request: Request):
    """Phone aur/ya email bhejo: candidate pehle se parsed hai ya nahi (scrape se pehle bhi use kar sakte ho)."""
    phone = normalize_phone(req.phone) if req.phone else None
    email = req.email.strip() or None if req.email else None
    if req.phone and not phone:
        raise HTTPException(422, "Invalid Indian mobile number")
    if not phone and not email:
        raise HTTPException(422, "Send phone and/or email")
    try:
        exists = await pipeline.precheck(request.app.state.client, phone, email, "check")
    except ApiError as e:
        raise HTTPException(503, f"DB API check failed: {e}")
    return {"phone": phone, "email": email, "exists": exists,
            "status": "already_parsed" if exists else "new",
            "message": "Candidate already parsed" if exists
            else "Candidate not found, send raw text to /submit"}


@router.get("/jobs/{jid}", dependencies=[Depends(auth)])
async def get_job(jid: int):
    r = store.rows("SELECT id,status,via,key_used,attempts,portal,error,created,updated,db_response "
                   "FROM jobs WHERE id=?", (jid,))
    if not r:
        raise HTTPException(404, "not found")
    return r[0]


@router.get("/jobs", dependencies=[Depends(auth)])
async def list_jobs(status: str = "failed", limit: int = 50):
    return store.rows("SELECT id,status,via,attempts,portal,error,updated FROM jobs "
                      "WHERE status=? ORDER BY id DESC LIMIT ?", (status, min(limit, 500)))


@router.post("/retry-failed", dependencies=[Depends(auth)])
async def retry_failed():
    return {"requeued": store.retry_failed()}


@router.get("/stats", dependencies=[Depends(auth)])
async def stats():
    return {**store.stats_snapshot(), "ollama_keys": pool_stats(),
            "db_api_logged_in": bool(api.token), "server_time": int(time.time())}


@router.get("/logs", dependencies=[Depends(auth)], response_class=PlainTextResponse)
async def logs(lines: int = 200):
    path = os.path.join(config.LOG_DIR, "app.log")
    if not os.path.exists(path):
        return ""
    with open(path, errors="replace") as f:
        return "".join(deque(f, maxlen=min(lines, 2000)))


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return (Path(__file__).parent / "static" / "dashboard.html").read_text()
