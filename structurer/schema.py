"""Output format (JSON schema), LLM prompt aur grounding fields."""
import copy

_S = {"type": ["string", "null"]}

CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": _S, "email": _S, "phone": _S, "gender": _S, "dateOfBirth": _S,
        "currentLocation": _S, "preferredLocation": _S,
        "currentDesignation": _S, "currentCompany": _S,
        "currentSalary": _S, "expectedSalary": _S,
        "totalExperience": _S, "noticePeriod": _S, "profileSummary": _S,
        "highestQualification": _S, "highestQualificationYear": _S,
        "skills": {"type": "array", "items": {"type": "string"}},
        "educations": {"type": "array", "items": {"type": "object", "properties": {
            "degree": _S, "institute": _S, "year": _S}}},
        "experiences": {"type": "array", "items": {"type": "object", "properties": {
            "designation": _S, "company": _S, "isCurrent": {"type": "boolean"}}}},
        "certificates": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["name", "email", "phone", "gender", "dateOfBirth", "currentLocation",
                 "preferredLocation", "currentDesignation", "currentCompany",
                 "currentSalary", "expectedSalary", "totalExperience", "noticePeriod",
                 "profileSummary", "highestQualification", "highestQualificationYear",
                 "skills", "educations", "experiences", "certificates"],
}

SYSTEM_PROMPT = """You extract candidate data from raw recruiter-portal text.
Return ONLY JSON that matches the given schema.
Rules:
- Use only information present in the text. Never guess or invent.
- If a value is missing, use null (strings) or [] (lists).
- Copy name, company, location, salary and experience EXACTLY as written in the text.
- The first line is the candidate name (any ID/phone line has been removed).
- Line like 'Immediate Joiner' or '15 Days Notice' is noticePeriod. 'Open to Contractual' is NOT a notice period.
- 'Current: <designation> | <company>' is the current job; 'Previous: ...' is a past job (isCurrent=false).
- Education lines look like '<degree> | <institute> | <year>'. highestQualification is the first listed one.
- skills: split the comma-separated list, remove duplicates, remove experience suffixes like '1.5Y',
  fix broken brackets (e.g. 'crm software (salesforce' -> 'crm software', 'salesforce'),
  and drop noise words that are not skills.
- profileSummary: write exactly 2 short sentences in third person (use the candidate's name) summarising
  current role, total experience, location and main skills. Use ONLY facts present in the text; if there is
  too little information, use null.
- Ignore 'May also know', match percentages, 'Response Likelihood', dates like 'Updated:' and 'Active:'."""

# Ye fields text se exactly copy hone chahiye; text mein na mile to null kar dete hain.
GROUND_FIELDS = ["name", "currentCompany", "currentLocation", "preferredLocation",
                 "currentSalary", "totalExperience", "noticePeriod"]


def llm_schema():
    """LLM ko phone/email ka field dikhta hi nahi (wo regex se nikalte hain)."""
    s = copy.deepcopy(CANDIDATE_SCHEMA)
    for k in ("phone", "email"):
        s["properties"].pop(k, None)
        s["required"].remove(k)
    return s
