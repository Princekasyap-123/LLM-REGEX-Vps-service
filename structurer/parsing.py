"""Rule-based parsing: phone/email regex, text cleaning, Shine profile parser."""
import json
import re

PHONE_RE = re.compile(r"(?<!\d)[6-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ID_LINE_RE = re.compile(r"\+?\d[\d\s-]{6,}")

EXP_LINE = re.compile(
    r"^(\d+\s*Yrs?\s*\d+\s*Months?)\s*(Rs\.?\s*[\d.,+]+\s*Lacs?)?\s*(.*)$", re.I)
NOTICE_RE = re.compile(r"immediate|\d+\s*days?|notice|serving|\d+\s*months?", re.I)
EDU_RE = re.compile(r"^,?\s*(.+?)\s*\|\s*(.+?)(?:\s*\|\s*((?:19|20)\d{2}))?\s*$")   # year optional
HEADER_RE = re.compile(r"^(?:[×✕xX]|\+?\d[\d\s-]{6,}(?:\s*[—–-]+\s*raw text)?)$", re.I)   # '98106 — raw text', '×'


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def normalize_phone(s: str):
    """Valid Indian mobile (10 digit, 6-9 se shuru) ya None."""
    d = re.sub(r"\D", "", s or "")
    if len(d) == 12 and d.startswith("91"):
        d = d[2:]
    elif len(d) == 11 and d.startswith("0"):
        d = d[1:]
    return d if re.fullmatch(r"[6-9]\d{9}", d) else None


def extract_phone(text: str):
    """(phone, status): status = ok | not_found. LLM kabhi involve nahi.
    Phone hamesha sabse upar wali (pehli) line ka number hai; neeche ka masked 'View +91 98xxxxx616'
    kabhi use nahi hota (na compare, na bhejna)."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    phone = normalize_phone(lines[0]) if lines else None
    if not phone:
        found = set(PHONE_RE.findall(text))
        if len(found) == 1:
            phone = found.pop()
    return phone, ("ok" if phone else "not_found")


def extract_email(text: str):
    m = EMAIL_RE.search(text)
    return m.group(0) if m else None


def clean_for_llm(text: str) -> str:
    """ID/phone wali pehli line aur 'Response Likelihood' ke baad ka junk hata do."""
    lines = text.strip().splitlines()
    while lines and (not lines[0].strip() or HEADER_RE.fullmatch(lines[0].strip())):
        lines.pop(0)
    return re.split(r"Response Likelihood", "\n".join(lines))[0].strip()


def parse_json(content: str) -> dict:
    content = content.strip()
    content = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def _split_job(s):
    if " | " in s:
        d, c = s.rsplit(" | ", 1)
        return d.strip(), c.strip()
    return s.strip(), None


def parse_shine(text: str):
    """Shine profile text -> (dict, ok). ok=False ho to LLM fallback chalega."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    while lines and HEADER_RE.fullmatch(lines[0]):
        lines.pop(0)

    out = {"name": lines[0] if lines else None, "noticePeriod": None,
           "totalExperience": None, "currentSalary": None, "currentLocation": None,
           "preferredLocation": None, "currentDesignation": None, "currentCompany": None,
           "highestQualification": None, "highestQualificationYear": None,
           "educations": [], "experiences": [], "skills": [], "certificates": []}
    skill_lines = []

    for l in lines[1:]:
        if l.lower().startswith(("may also know", "response likelihood")):
            break
        m = EXP_LINE.match(l)
        if m and not out["totalExperience"]:
            out["totalExperience"] = m.group(1).strip()
            out["currentSalary"] = (m.group(2) or "").strip() or None
            out["currentLocation"] = (m.group(3) or "").strip() or None
        elif l.startswith("Current:"):
            d, c = _split_job(l.split(":", 1)[1])
            out["currentDesignation"], out["currentCompany"] = d, c
            out["experiences"].append({"designation": d, "company": c, "isCurrent": True})
        elif l.startswith("Previous:"):
            d, c = _split_job(l.split(":", 1)[1])
            out["experiences"].append({"designation": d, "company": c, "isCurrent": False})
        elif l.startswith("Pref. Location:"):
            out["preferredLocation"] = l.split(":", 1)[1].strip() or None
        elif EDU_RE.match(l):
            deg, inst, yr = EDU_RE.match(l).groups()
            out["educations"].append({"degree": deg, "institute": inst, "year": yr})
        elif l.startswith(","):
            skill_lines.append(l)
        elif NOTICE_RE.search(l) and len(l) < 40 and not out["noticePeriod"]:
            out["noticePeriod"] = l

    if out["educations"]:
        out["highestQualification"] = out["educations"][0]["degree"]
        out["highestQualificationYear"] = out["educations"][0]["year"]

    raw = ",".join(skill_lines).replace("(", ",").replace(")", ",")
    seen = set()
    for s in raw.split(","):
        s = s.strip()
        if s and not re.fullmatch(r"\d+(\.\d+)?\s*Y", s, re.I) and s.lower() not in seen:
            seen.add(s.lower())
            out["skills"].append(s)

    name = out["name"] or ""
    ok = bool(name and len(name) <= 60 and ":" not in name
              and (out["experiences"] or out["educations"] or out["skills"]))
    return out, ok
