"""Database API client: auto-login, duplicate check by phone, candidate save."""
import asyncio
import random
import re
import time

import httpx

from . import config
from .logging_setup import log


class ApiError(Exception):
    """DB API se baat nahi ho paayi / usne reject kiya."""


class AlreadyExists(ApiError):
    """Save API ne kaha 'User Already Exists' -> retry nahi, already_parsed maano."""


def dig(obj, path):
    for p in path.split("."):
        obj = obj.get(p) if isinstance(obj, dict) else None
    return obj


def truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "exists", "found")
    if isinstance(v, (list, dict, tuple, set)):
        return len(v) > 0
    return bool(v)


_UNITS = {"lac": 100_000, "lakh": 100_000, "cr": 10_000_000, "crore": 10_000_000, "k": 1000}


def salary_to_number(s):
    """'Rs. 4.2 Lacs' -> 420000, '450000' -> 450000, samajh na aaye to None."""
    if isinstance(s, (int, float)):
        return int(s)
    m = re.search(r"(\d[\d,]*\.?\d*)\s*([a-z]*)", (s or "").lower().replace("rs.", ""))
    if not m:
        return None
    num = float(m.group(1).replace(",", ""))
    mult = next((v for k, v in _UNITS.items() if m.group(2).startswith(k)), 1)
    return int(round(num * mult))


def experience_to_number(s):
    """'6 Yrs 0 Month' -> 6, '23 Yrs 4 Month' -> 23.3 (years)."""
    if isinstance(s, (int, float)):
        return s
    m = re.search(r"(\d+(?:\.\d+)?)\s*y(?:ea)?rs?(?:\s*(\d+)\s*m)?", (s or "").lower())
    if not m:
        m2 = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", s or "")
        return float(m2.group(1)) if m2 else None
    v = float(m.group(1)) + int(m.group(2) or 0) / 12
    return int(v) if v == int(v) else round(v, 1)


def to_year(s):
    m = re.search(r"(?:19|20)\d{2}", str(s or ""))
    return int(m.group(0)) if m else None


def to_list(v):
    if isinstance(v, list):
        return v
    return [p.strip() for p in (v or "").split(",") if p.strip()]


def to_payload(d, portal, created_by=None):
    """Structured candidate -> storeCandidateFromChromeExtention (Laravel) ka payload.
    Controller top-level se created_by / name / email / mobile / domain padhta hai aur baaki
    $data['data'] se (snake_case). Poora $data resume_parser_json mein bhi save hota hai."""
    phone = d.get("mobile") or d.get("phone")
    return {
        "created_by": config.SAVE_CREATED_BY,           # hamesha 1 (client ke createdBy ko ignore)
        "name": d.get("name"),                          # FIX: top-level name (backend yahin se padhta hai)
        "email": d.get("email"), "mobile": phone,
        "domain": portal, "imgSrc": None,
        "data": {
            "name": d.get("name"), "email": d.get("email"), "mobile": phone, "phone": phone,
            "gender": d.get("gender"), "dob": d.get("dateOfBirth"),
            "total_experience": experience_to_number(d.get("totalExperience")),
            "current_location": d.get("currentLocation"),
            "preferred_location": to_list(d.get("preferredLocation")),
            "current_salary": salary_to_number(d.get("currentSalary")),
            "expected_salary": salary_to_number(d.get("expectedSalary")),
            "notice_period": d.get("noticePeriod"),
            "functional_area": d.get("currentDesignation"),      # controller isse current_title banata hai
            "current_designation": d.get("currentDesignation"),
            "current_company": d.get("currentCompany"),
            "highest_qualification": d.get("highestQualification"),
            "highestQualification_year": to_year(d.get("highestQualificationYear")),
            "skills": d.get("skills") or [],
            "profile_summary": d.get("profileSummary"),
            "educations": [{"educationTitle": e.get("degree"), "educationInstitute": e.get("institute"),
                            "startYear": None, "endYear": to_year(e.get("year"))}
                           for e in d.get("educations") or []],
            "experiences": [{"designation": e.get("designation"), "company": e.get("company"),
                             "startDate": None, "endDate": None, "description": None}
                            for e in d.get("experiences") or []],
            "certificates": d.get("certificates") or [],
        },
    }


def _retry_after(r):
    """Retry-After header (seconds) -> float, warna None. Max 120s."""
    try:
        return min(120.0, float(r.headers.get("Retry-After")))
    except (TypeError, ValueError):
        return None


class DbApi:
    def __init__(self):
        self.token = config.SENIORS_TOKEN or None
        self.login_lock = asyncio.Lock()
        self.gate = asyncio.Semaphore(config.API_MAX_CONCURRENCY)   # ek saath kitne API calls
        self.pace_lock = asyncio.Lock()
        self.next_slot = 0.0

    async def _pace(self):
        """Do API calls ke beech kam se kam API_MIN_INTERVAL seconds ka gap (saare workers + /submit milakar)."""
        async with self.pace_lock:
            now = time.monotonic()
            start = max(now, self.next_slot)
            self.next_slot = start + config.API_MIN_INTERVAL
        if start > now:
            await asyncio.sleep(start - now)

    async def _send(self, client, method, url, tag, **kw):
        """Throttled HTTP call. 429 aaye to Retry-After (ya exponential backoff) ke baad dobara try."""
        r = None
        for attempt in range(1, config.API_429_RETRIES + 2):
            async with self.gate:
                await self._pace()
                r = await client.request(method, url, **kw)
            if r.status_code != 429 or attempt > config.API_429_RETRIES:
                return r
            wait = _retry_after(r) or min(60, 2 ** attempt) + random.random()
            log.warning("%s API 429 on %s (attempt %d/%d) -> waiting %.1fs", tag, url.split("?")[0],
                        attempt, config.API_429_RETRIES + 1, wait)
            await asyncio.sleep(wait)
        return r

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.token:
            h[config.SENIORS_AUTH_HEADER] = config.SENIORS_AUTH_PREFIX + self.token
        return h

    async def login(self, client, bad_token):
        async with self.login_lock:
            if self.token and self.token != bad_token:
                return                              # kisi aur worker ne refresh kar diya
            if not config.SENIORS_LOGIN_URL:
                raise ApiError("auth failed and SENIORS_LOGIN_URL is not configured")
            try:
                r = await client.post(config.SENIORS_LOGIN_URL, json={
                    config.SENIORS_USER_FIELD: config.SENIORS_USER,
                    config.SENIORS_PASS_FIELD: config.SENIORS_PASS})
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise ApiError(f"login network error: {e}")
            if r.status_code != 200:
                raise ApiError(f"login failed {r.status_code}: {r.text[:150]}")
            try:
                tok = dig(r.json(), config.SENIORS_TOKEN_PATH)
            except Exception:
                tok = None
            if not tok:
                raise ApiError("token not found in login response (check SENIORS_TOKEN_PATH)")
            self.token = tok
            log.info("DB API login ok (token refreshed)")

    async def request(self, client, method, url, payload, tag):
        """Auth ke saath request; 401/403 par ek baar re-login + retry."""
        if not self.token and config.SENIORS_LOGIN_URL:
            await self.login(client, None)
        r = None
        for attempt in (1, 2):
            used = self.token
            try:
                if method == "GET":
                    r = await self._send(client, "GET", url, tag, params=payload, headers=self._headers())
                else:
                    r = await self._send(client, method, url, tag, json=payload, headers=self._headers())
            except (httpx.TimeoutException, httpx.TransportError) as e:
                raise ApiError(f"network: {e}")
            if r.status_code in (401, 403) and attempt == 1:
                log.warning("%s DB API returned %s -> re-login", tag, r.status_code)
                await self.login(client, used)
                continue
            break
        return r

    async def exists(self, client, phone, email, tag):
        """White Force check-candidate-exists (public, no auth).
        POST {"phone","email"} -> {"exists": bool, "matched_by": ...}.
        True = email ya phone DB mein pehle se hai. URL set nahi / dono khaali to False.
        404 = not found, 422 = missing params (dono valid JSON); sirf 5xx/network = ApiError."""
        if not phone and not email:
            return False
        if not config.DUPLICATE_CHECK_API_URL:
            return False
        payload = {}
        if phone:
            payload["phone"] = phone
        if email:
            payload["email"] = email
        try:
            r = await self._send(client, "POST", config.DUPLICATE_CHECK_API_URL, tag, json=payload,
                                 timeout=config.DUPLICATE_CHECK_TIMEOUT)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ApiError(f"check API network: {e}")
        if r.status_code == 429:
            raise ApiError("check API rate limited (429) even after retries")
        if r.status_code >= 500:
            raise ApiError(f"check API {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except ValueError:
            raise ApiError(f"check API returned invalid JSON ({r.status_code})")
        found = bool(data.get("exists")) if isinstance(data, dict) else False
        log.info("%s dedup check phone=%s email=%s -> exists=%s matched_by=%s", tag, phone, email,
                 found, data.get("matched_by") if isinstance(data, dict) else None)
        return found

    async def save(self, client, payload, tag):
        r = await self.request(client, "POST", config.SENIORS_URL, payload, tag)
        if r.status_code == 429:
            raise ApiError("save API rate limited (429) even after retries")
        try:
            j = r.json()
        except Exception:
            raise ApiError(f"non-json response {r.status_code}: {r.text[:200]}")
        # 2xx chahiye; body mein status/success False ho to reject (jawab ka format pakka nahi pata)
        flags = [j.get(k) for k in ("status", "success") if isinstance(j, dict) and k in j]
        if not 200 <= r.status_code < 300 or any(f in (False, "error", "failed", "fail") for f in flags):
            if "already exists" in str(j).lower():
                raise AlreadyExists(f"DB API: {str(j)[:200]}")
            raise ApiError(f"DB API rejected: {r.status_code} {str(j)[:300]}")
        return j if isinstance(j, dict) else {"data": j}


api = DbApi()
