from conftest import sample
from structurer.parsing import (clean_for_llm, extract_email, extract_phone, normalize_phone,
                                parse_shine)


def test_mohammed_profile():
    text = sample("mohammed.txt")
    phone, status = extract_phone(text)
    assert phone is None and status == "not_found"      # pehli line ID hai, phone nahi
    d, ok = parse_shine(clean_for_llm(text))
    assert ok
    assert d["name"] == "MOHAMMED HASSAN"
    assert d["totalExperience"] == "20 Yrs 2 Month"
    assert d["currentSalary"] == "Rs. 100+ Lacs"
    assert d["currentLocation"] == "Dubai"
    assert d["preferredLocation"] == "Dubai"
    assert d["currentCompany"] == "Kafak International Group"
    assert d["currentDesignation"].startswith("Retail , Operations & Business Development Director")
    assert d["noticePeriod"] is None                    # 'Open to Contractual' notice period nahi
    assert d["educations"] == [{"degree": "Bachelor’s degree (Finance, Commerce, and Business Administration)",
                                "institute": "Suez Canal University", "year": "2003"}]
    assert d["highestQualificationYear"] == "2003"
    assert [e["isCurrent"] for e in d["experiences"]] == [True, False]
    assert "staff development" in d["skills"] and "sales strategy development" in d["skills"]
    assert not any(s.endswith("Y") and s[:-1].replace(".", "").isdigit() for s in d["skills"])
    assert len(d["skills"]) == len(set(s.lower() for s in d["skills"]))   # no duplicates


def test_ritesh_profile_and_phone_mask_check():
    text = sample("ritesh.txt")
    phone, status = extract_phone(text)
    assert (phone, status) == ("9999368246", "ok")
    d, ok = parse_shine(clean_for_llm(text))
    assert ok
    assert d["name"] == "Ritesh Rathi"
    assert d["noticePeriod"] == "Immediate Joiner"
    assert d["currentSalary"] == "Rs. 4.2 Lacs" and d["currentLocation"] == "Delhi"
    assert d["preferredLocation"] == "Gurugram, Delhi"
    assert len(d["educations"]) == 2
    assert [e["company"] for e in d["experiences"]] == ["Prerna Properties Pvt. Ltd.", "PI Overseas Pvt. Ltd."]
    assert "salesforce" in d["skills"] and "crm software" in d["skills"] and "freshsales" in d["skills"]
    assert not any("(" in s or ")" in s for s in d["skills"])


def test_phone_is_top_number_never_masked():
    text = sample("ritesh.txt").replace("9999368246", "9999368999")      # masked tail ab alag hai
    assert extract_phone(text) == ("9999368999", "ok")                  # phir bhi top number
    no_top = "Name Surname\nView +91 99xxxxx246"
    assert extract_phone(no_top) == (None, "not_found")                 # masked kabhi phone nahi


def test_normalize_phone():
    assert normalize_phone("+91 99993 68246") == "9999368246"
    assert normalize_phone("09999368246") == "9999368246"
    assert normalize_phone("1234567890") is None
    assert normalize_phone("9710567643876") is None


def test_email():
    assert extract_email("mail me at a.b@x.co.in please") == "a.b@x.co.in"
    assert extract_email("no email here") is None


def test_header_format_samples():
    from conftest import sample
    from structurer.parsing import extract_phone, parse_shine, clean_for_llm
    cases = {
        "divay.txt": ("9810681616", "Divay Chhibber", "12 Yrs 0 Month", "Rs. 24 Lacs", "Faridabad",
                      [{"degree": "B.Tech (Mechanical Engineering)", "institute": "Manav Bharti University", "year": None}]),
        "bharath.txt": ("8074197749", "Bharathsimha Reddy Sangem", "7 Yrs 0 Month", "Rs. 12 Lacs", "Hyderabad", None),
        "subramanian.txt": ("9840377044", "Subramanian Krishnamurthy", "23 Yrs 4 Month", "Rs. 6.5 Lacs", "Chennai",
                            [{"degree": "M.A (Arts and Humanities)", "institute": "Annamalai University", "year": "2002"}]),
    }
    for f, (phone, name, exp, sal, loc, edu) in cases.items():
        text = sample(f)
        assert extract_phone(text) == (phone, "ok")
        d, ok = parse_shine(clean_for_llm(text))
        assert ok and d["name"] == name and d["totalExperience"] == exp
        assert d["currentSalary"] == sal and d["currentLocation"] == loc
        assert d["currentDesignation"] and d["currentCompany"]
        if edu:
            assert d["educations"] == edu
        assert all("|" not in s and not s.endswith("Y") or s == "" for s in d["skills"]) and d["skills"]
    d, _ = parse_shine(clean_for_llm(sample("bharath.txt")))
    assert [e["year"] for e in d["educations"]] == ["2018", "2016"]
    assert d["highestQualification"].startswith("Master of Business Administration")
