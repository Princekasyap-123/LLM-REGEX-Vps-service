"""Saari settings yahin se aati hain (environment ya .env file)."""
import os
import re
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name, default, strip=True):
    """systemd EnvironmentFile inline '# comment' nahi hatata (python-dotenv hatata hai) -> yahin hata do."""
    v = os.getenv(name)
    if v is None:
        return default
    v = re.sub(r"\s+#.*$", "", v)
    return v.strip() if strip else v


def _get(name, default="", required=False):
    v = _env(name, default)
    if required and not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


def _int(name, default):
    return int(_env(name, str(default)))


def _float(name, default):
    return float(_env(name, str(default)))


def _bool(name, default):
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes")


# ---- service ----
SERVICE_API_KEY = _get("SERVICE_API_KEY", required=True)
LOG_DIR = _get("LOG_DIR", str(BASE_DIR / "logs"))
DB_PATH = _get("DB_PATH", str(BASE_DIR / "data" / "jobs.db"))
HTTP_TIMEOUT = _float("HTTP_TIMEOUT", 120)
LOG_PAYLOAD = _bool("LOG_PAYLOAD", True)          # save API ko jaane wala poora payload + response log mein

# ---- ollama ----
OLLAMA_URL = _get("OLLAMA_URL", "https://ollama.com/api/chat")
OLLAMA_MODEL = _get("OLLAMA_MODEL", "gpt-oss:120b")
OLLAMA_API_KEYS = [k.strip() for k in _get("OLLAMA_API_KEYS", required=True).split(",") if k.strip()]
KEY_MIN_INTERVAL = _float("KEY_MIN_INTERVAL", 5)
KEY_SESSION_BUDGET = _int("KEY_SESSION_BUDGET", 0)          # 0 = off
KEY_SESSION_WINDOW = 5 * 3600
RATE_LIMIT_COOLDOWN = _int("RATE_LIMIT_COOLDOWN", 900)
WAIT_FOR_KEY_SECONDS = _int("WAIT_FOR_KEY_SECONDS", 60)
SCHEMA_IN_PROMPT = _bool("SCHEMA_IN_PROMPT", True)

# ---- database API: save + auto login ----
SENIORS_URL = _get("SENIORS_URL", "")      # khaali = parse karke 'parsed' status mein rok do (save baad mein)
SAVE_CREATED_BY = _int("SAVE_CREATED_BY", 1)               # save payload mein createdBy hamesha ye
SENIORS_LOGIN_URL =_get("SENIORS_LOGIN_URL", "")
SENIORS_USER = _get("SENIORS_USER", "")
SENIORS_PASS = _get("SENIORS_PASS", "")
SENIORS_USER_FIELD = _get("SENIORS_USER_FIELD", "email")
SENIORS_PASS_FIELD = _get("SENIORS_PASS_FIELD", "password")
SENIORS_TOKEN_PATH = _get("SENIORS_TOKEN_PATH", "token")
SENIORS_TOKEN = _get("SENIORS_TOKEN", "")
SENIORS_AUTH_HEADER = _get("SENIORS_AUTH_HEADER", "Authorization")
SENIORS_AUTH_PREFIX = _env("SENIORS_AUTH_PREFIX", "Bearer ", strip=False).strip('"')   # trailing space zaroori

# ---- database API: duplicate check ----
DUPLICATE_CHECK_API_URL = _get("DUPLICATE_CHECK_API_URL", "")   # public, no auth
DUPLICATE_CHECK_TIMEOUT = _float("DUPLICATE_CHECK_TIMEOUT", 8)
CHECK_FAIL_OPEN = _bool("CHECK_FAIL_OPEN", False)
REQUIRE_PHONE = _bool("REQUIRE_PHONE", False)

# ---- pipeline tuning ----
WORKERS = _int("WORKERS", 4)
MAX_ATTEMPTS = _int("MAX_ATTEMPTS", 4)
MIN_TEXT_LEN = _int("MIN_TEXT_LEN", 200)
RETENTION_DAYS = _int("RETENTION_DAYS", 7)
SEEN_TTL_DAYS = _int("SEEN_TTL_DAYS", 30)
