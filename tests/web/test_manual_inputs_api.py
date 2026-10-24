"""'읽은 기사 붙여 넣기' API: 토큰·검증·파일 저장·조회."""

from helpers_web import AUTH

from trader.sources import manual_inputs


def test_manual_inputs_requires_token_and_validates(client):
    body = {"market": "KR", "title": "반도체 수출 호조"}
    assert client.post("/api/control/manual-inputs", json=body).status_code == 401
    r = client.post("/api/control/manual-inputs", json={**body, "title": "  "}, headers=AUTH)
    assert r.status_code == 400
    r = client.post("/api/control/manual-inputs", json={**body, "url": "ftp://x"}, headers=AUTH)
    assert r.status_code == 400
    r = client.post("/api/control/manual-inputs", json={**body, "market": "JP"}, headers=AUTH)
    assert r.status_code == 422
    assert client.get("/api/control/manual-inputs").status_code == 400  # market 필수


def test_manual_inputs_roundtrip(client, deps, project):
    body = {
        "market": "KR",
        "title": "반도체 수출 호조",
        "url": "https://www.saveticker.com/news/1",
        "note": "세이브티커에서 읽음",
    }
    r = client.post("/api/control/manual-inputs", json=body, headers=AUTH)
    assert r.status_code == 200 and r.json()["received_at"].endswith("+00:00")
    g = client.get("/api/control/manual-inputs?market=KR").json()
    assert g["items"][0]["title"] == "반도체 수출 호조"
    assert g["items"][0]["note"] == "세이브티커에서 읽음"
    assert client.get("/api/control/manual-inputs?market=US").json()["items"] == []
    assert (project / "state" / "manual_inputs" / "_market_KR.md").is_file()
    assert manual_inputs.read_market("KR")[0]["url"] == body["url"]
