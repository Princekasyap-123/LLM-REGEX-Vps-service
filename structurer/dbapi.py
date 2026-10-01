"""Database API client: auto-login, duplicate check by phone, candidate save."""
import asyncio
import re

import httpx

from . import config
from .logging_setup import log


class ApiError(Exception):
    """DB API se baat nahi ho paayi / usne reject kiya."""


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
    """Structured candidate -> White Force store-candidate-data-from-chrome-extention payload."""
    return {
        "createdBy": config.SAVE_CREATED_BY,            # hamesha 1 (client ke createdBy ko ignore)
        "portal": portal,
        "name": d.get("name"), "email": d.get("email"), "phone": d.get("phone"),
        "gender": d.get("gender"), "dateOfBirth": d.get("dateOfBirth"),
        "currentLocation": d.get("currentLocation"),
        "preferredLocation": to_list(d.get("preferredLocation")),
        "currentDesignation": d.get("currentDesignation"),
        "currentCompany": d.get("currentCompany"),
        "currentSalary": salary_to_number(d.get("currentSalary")),
        "expectedSalary": salary_to_number(d.get("expectedSalary")),
        "totalExperience": experience_to_number(d.get("totalExperience")),
        "noticePeriod": d.get("noticePeriod"),
        "profileSummary": d.get("profileSummary"),
        "highestQualification": d.get("highestQualification"),
        "highestQualificationYear": to_year(d.get("highestQualificationYear")),
        "skills": d.get("skills") or [],
        "educations": [{"educationTitle": e.get("degree"), "educationInstitute": e.get("institute"),
                        "startYear": None, "endYear": to_year(e.get("year"))}
                       for e in d.get("educations") or []],
        "experiences": [{"designation": e.get("designation"), "company": e.get("company"),
                         "startDate": None, "endDate": None, "description": None}
                        for e in d.get("experiences") or []],
        "certificates": d.get("certificates") or [],
        "mobile": d.get("mobile") or d.get("phone"),
    }


class DbApi:
    def __init__(self):
        self.token = config.SENIORS_TOKEN or None
        self.login_lock = asyncio.Lock()

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
                    r = await client.request("GET", url, params=payload, headers=self._headers())
                else:
                    r = await client.request(method, url, json=payload, headers=self._headers())
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
            r = await client.post(config.DUPLICATE_CHECK_API_URL, json=payload,
                                  timeout=config.DUPLICATE_CHECK_TIMEOUT)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise ApiError(f"check API network: {e}")
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
        try:
            j = r.json()
        except Exception:
            raise ApiError(f"non-json response {r.status_code}: {r.text[:200]}")
        # 2xx chahiye; body mein status/success False ho to reject (jawab ka format pakka nahi pata)
        flags = [j.get(k) for k in ("status", "success") if isinstance(j, dict) and k in j]
        if not 200 <= r.status_code < 300 or any(f in (False, "error", "failed", "fail") for f in flags):
            raise ApiError(f"DB API rejected: {r.status_code} {str(j)[:300]}")
        return j if isinstance(j, dict) else {"data": j}


api = DbApi()
