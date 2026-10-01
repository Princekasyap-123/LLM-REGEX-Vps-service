"""SQLite: durable job queue + seen_phones cache (duplicate check ke liye)."""
import os
import sqlite3
import threading
import time

from . import config
from .logging_setup import log

_db = None
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
    id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT UNIQUE, status TEXT, via TEXT,
    key_used INTEGER, attempts INTEGER DEFAULT 0, portal TEXT, created_by INTEGER,
    raw_text TEXT, result TEXT, db_response TEXT, error TEXT,
    next_try REAL DEFAULT 0, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS idx_status ON jobs(status, next_try);
CREATE TABLE IF NOT EXISTS seen_phones(
    phone TEXT PRIMARY KEY, source TEXT, ts REAL);
"""


def init(path=None):
    global _db
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    _db = sqlite3.connect(path, check_same_thread=False)
    _db.execute("PRAGMA journal_mode=WAL")
    _db.executescript(_SCHEMA)
    _db.commit()


def run(sql, args=()):
    with _lock:
        cur = _db.execute(sql, args)
        _db.commit()
        return cur


def rows(sql, args=()):
    with _lock:
        cur = _db.execute(sql, args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------- jobs ----------
def add_job(h, portal, created_by, raw_text):
    now = time.time()
    cur = run("INSERT INTO jobs(hash,status,portal,created_by,raw_text,created,updated) "
              "VALUES(?,?,?,?,?,?,?)", (h, "queued", portal, created_by, raw_text, now, now))
    return cur.lastrowid


def add_audit(status, portal, created_by, note=None):
    """Bina raw text ka record (jaise already_parsed) taaki stats mein gine jaayein."""
    now = time.time()
    cur = run("INSERT INTO jobs(hash,status,portal,created_by,error,created,updated) "
              "VALUES(NULL,?,?,?,?,?,?)", (status, portal, created_by, note, now, now))
    return cur.lastrowid


def find_by_hash(h):
    r = rows("SELECT id, status FROM jobs WHERE hash=?", (h,))
    return r[0] if r else None


def set_job(jid, **kw):
    kw["updated"] = time.time()
    cols = ", ".join(f"{k}=?" for k in kw)
    run(f"UPDATE jobs SET {cols} WHERE id=?", (*kw.values(), jid))


def claim_job():
    r = rows("SELECT * FROM jobs WHERE status='queued' AND next_try<=? ORDER BY id LIMIT 1",
             (time.time(),))
    if not r:
        return None
    set_job(r[0]["id"], status="processing")     # beech mein await nahi -> race nahi
    return r[0]


def recover():
    """Crash/restart ke baad 'processing' jobs wapas queue mein."""
    run("UPDATE jobs SET status='queued' WHERE status='processing'")
    if config.SENIORS_URL:                       # save API ab set hai: ruke hue jobs save karo
        n = run("UPDATE jobs SET status='queued', error=NULL, next_try=0 WHERE status='parsed'").rowcount
        if n:
            log.info("requeued %d parsed jobs for saving", n)


def retry_or_fail(job, err, stage):
    att = job["attempts"] + 1
    if att >= config.MAX_ATTEMPTS:
        set_job(job["id"], status="failed", attempts=att, error=err)
        log.error("job %s FAILED after %d attempts (%s): %s", job["id"], att, stage, err)
    else:
        delay = 60 * att
        set_job(job["id"], status="queued", attempts=att, next_try=time.time() + delay, error=err)
        log.warning("job %s %s error (attempt %d/%d), retry in %ds: %s",
                    job["id"], stage, att, config.MAX_ATTEMPTS, delay, err)


def retry_failed():
    return run("UPDATE jobs SET status='queued', attempts=0, next_try=0 WHERE status='failed'").rowcount


# ---------- seen phones (local cache) ----------
def seen_has(phone):
    cutoff = time.time() - config.SEEN_TTL_DAYS * 86400
    return bool(rows("SELECT 1 FROM seen_phones WHERE phone=? AND ts>?", (phone, cutoff)))


def seen_add(phone, source):
    run("INSERT OR REPLACE INTO seen_phones(phone,source,ts) VALUES(?,?,?)",
        (phone, source, time.time()))


# ---------- housekeeping + stats ----------
def purge():
    cutoff = time.time() - config.RETENTION_DAYS * 86400
    a = run("UPDATE jobs SET raw_text=NULL, result=NULL WHERE status IN ('done','skipped') "
            "AND updated<? AND (raw_text IS NOT NULL OR result IS NOT NULL)", (cutoff,)).rowcount
    b = run("DELETE FROM jobs WHERE status='already_parsed' AND updated<?", (cutoff,)).rowcount
    c = run("DELETE FROM seen_phones WHERE ts<?",
            (time.time() - config.SEEN_TTL_DAYS * 86400,)).rowcount
    return a, b, c


def stats_snapshot():
    by_status = {r["status"]: r["c"] for r in rows(
        "SELECT status, COUNT(*) c FROM jobs GROUP BY status")}
    by_via = {r["via"]: r["c"] for r in rows(
        "SELECT via, COUNT(*) c FROM jobs WHERE status='done' GROUP BY via")}
    day = time.time() - 86400
    done24 = rows("SELECT COUNT(*) c FROM jobs WHERE status='done' AND updated>?", (day,))[0]["c"]
    dup24 = rows("SELECT COUNT(*) c FROM jobs WHERE status='already_parsed' AND updated>?",
                 (day,))[0]["c"]
    done = sum(by_via.values())
    return {
        "jobs_by_status": by_status, "done_by_method": by_via,
        "done_last_24h": done24, "already_parsed_last_24h": dup24,
        "parser_share_pct": round(100 * by_via.get("parser", 0) / done, 1) if done else None,
    }
