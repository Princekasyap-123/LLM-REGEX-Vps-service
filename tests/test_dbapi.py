"""DbApi ka asli HTTP logic (auto-login, 401 re-login) aur public duplicate-check nakli server par."""
import asyncio
import json

import httpx
import pytest

from structurer import config
from structurer.dbapi import ApiError, DbApi


def make_server(state):
    def handler(request: httpx.Request):
        url = str(request.url)
        if url.endswith("/login"):
            state["logins"] += 1
            return httpx.Response(200, json={"data": {"token": f"tok{state['logins']}"}})
        if request.headers.get("authorization") != f"Bearer tok{state['valid']}":
            return httpx.Response(401, json={"message": "expired"})
        if url.endswith("/save"):
            return httpx.Response(200, json={"status": True, "data": {"id": 7}})
        return httpx.Response(404)
    return handler


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    monkeypatch.setattr(config, "SENIORS_LOGIN_URL", "http://db.test/login")
    monkeypatch.setattr(config, "SENIORS_TOKEN_PATH", "data.token")
    monkeypatch.setattr(config, "SENIORS_URL", "http://db.test/save")
    monkeypatch.setattr(config, "DUPLICATE_CHECK_API_URL", "http://db.test/check")


def test_auto_login_and_relogin_on_save():
    state = {"logins": 0, "valid": 1}

    async def go():
        api = DbApi()
        async with httpx.AsyncClient(transport=httpx.MockTransport(make_server(state))) as c:
            await api.save(c, {"x": 1}, "t")                            # pehli call: auto login
            assert state["logins"] == 1
            state["valid"] = 2                                          # token expire ho gaya
            resp = await api.save(c, {"x": 1}, "t")                     # 401 -> re-login -> retry
            assert resp["data"]["id"] == 7 and state["logins"] == 2
    asyncio.run(go())


def check(handler, phone="9999368246", email="a@x.com"):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await DbApi().exists(c, phone, email, "t")
    return asyncio.run(go())


def test_check_sends_both_fields_without_auth():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"exists": True, "matched_by": "email"})
    assert check(handler) is True
    assert seen["body"] == {"phone": "9999368246", "email": "a@x.com"} and seen["auth"] is None


def test_check_omits_empty_fields_and_skips_when_nothing_to_check():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"exists": False})
    assert check(handler, email=None) is False and seen["body"] == {"phone": "9999368246"}
    seen.clear()
    assert check(handler, phone=None, email=None) is False and not seen    # API call hi nahi


def test_check_not_found_and_422_are_not_errors():
    assert check(lambda r: httpx.Response(404, json={"exists": False})) is False
    assert check(lambda r: httpx.Response(422, json={"detail": "missing"})) is False


def test_check_url_unset_means_no_skip(monkeypatch):
    monkeypatch.setattr(config, "DUPLICATE_CHECK_API_URL", "")
    assert check(lambda r: httpx.Response(200, json={"exists": True})) is False


def test_check_errors_raise():
    with pytest.raises(ApiError):
        check(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(ApiError):
        check(lambda r: httpx.Response(200, text="<html>"))

    def down(request):
        raise httpx.ConnectError("down")
    with pytest.raises(ApiError):
        check(down)


def test_payload_matches_white_force_shape():
    from structurer.dbapi import to_payload, salary_to_number, experience_to_number
    assert salary_to_number("Rs. 24 Lacs") == 2400000 and salary_to_number("Rs. 6.5 Lacs") == 650000
    assert salary_to_number(None) is None and salary_to_number("450000") == 450000
    assert experience_to_number("4 Yrs 0 Month") == 4 and experience_to_number("23 Yrs 4 Month") == 23.3
    p = to_payload({"name": "A", "phone": "9810681616", "preferredLocation": "Noida, Delhi",
                    "currentSalary": "Rs. 24 Lacs", "totalExperience": "12 Yrs 0 Month",
                    "highestQualificationYear": None, "skills": ["x"]}, "recruiter.shine.com", 99)
    assert p["createdBy"] == 1 and p["mobile"] == "9810681616" and p["email"] is None
    assert p["preferredLocation"] == ["Noida", "Delhi"] and p["currentSalary"] == 2400000
    assert p["totalExperience"] == 12 and p["highestQualificationYear"] is None
    assert p["certificates"] == [] and p["educations"] == [] and "data" not in p
