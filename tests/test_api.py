import json

import pytest
from fastapi.testclient import TestClient

from api import create_app


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "api.sqlite3", testing=True)) as c:
        yield c


def headers(client):
    return {"X-Firefly-Token": client.get("/api/state").json()["csrf_token"]}


def test_import_review_export_and_delete(client):
    h = headers(client)
    result = client.post(
        "/api/import",
        headers=h,
        json={
            "files": [
                {"name": "notes.jsonl", "content": json.dumps({"note_id": "1", "desc": "lumo 很好用"})}
            ],
            "site": "rednote",
        },
    )
    assert result.status_code == 200, result.text
    state = client.get("/api/state").json()
    key = state["sources"][0]["key"]
    r = client.put(
        f"/api/sources/{key}/review",
        headers=h,
        json={
            "relevance": "relevant",
            "review_status": "reviewed",
            "opinions": [
                {
                    "module": "语音助手（lumo）",
                    "type": "体验评价",
                    "sentiment": "正面",
                    "theme": "好用",
                    "evidence": "lumo 很好用",
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert "lumo 很好用" in client.get("/api/export.csv?kind=note").text
    assert client.get(f"/api/sources/{key}").json()["url"].startswith("https://www.rednote.com/")
    bid = result.json()["batches"][0]["id"]
    assert client.delete(f"/api/batches/{bid}", headers=h).json()["deleted_sources"] == 1


def test_csrf_and_external_origin_blocked(client):
    assert client.post("/api/import", json={}).status_code == 403
    assert client.get("/api/state", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/state", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_credentials_never_returned(client):
    h = headers(client)
    response = client.put(
        "/api/settings",
        headers=h,
        json={
            "api_key": "SUPERSECRET",
            "base_url": "https://example.org/v1",
            "model": "test",
            "input_price": 1,
            "output_price": 1,
        },
    )
    assert response.status_code == 200
    assert "SUPERSECRET" not in response.text
    assert "SUPERSECRET" not in client.get("/api/state").text


def test_empty_state_and_no_paid_side_effect(client):
    state = client.get("/api/state").json()
    assert state["sources"] == [] and state["budget"]["spent"] == 0
    r = client.post("/api/analysis/estimate", headers=headers(client), json={"mode": "pending"})
    assert not r.json()["can_start"]


def test_html_and_script_served(client):
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200


def test_import_and_start_are_serialized(client, monkeypatch):
    import threading

    h = headers(client)
    client.post(
        "/api/import",
        headers=h,
        json={
            "files": [{"name": "a.jsonl", "content": json.dumps({"note_id": "1", "desc": "lumo 很好用"})}],
            "site": "rednote",
        },
    )
    engine = client.app.state.analyzer
    engine.settings.update(
        {
            "base_url": "https://example.org/v1",
            "model": "test",
            "api_key": "test-key",
            "input_price": 1,
            "output_price": 1,
        }
    )
    calls = []

    def transport(*_):
        calls.append(1)
        return {"relevance": "relevant", "opinions": []}, {"prompt_tokens": 1, "completion_tokens": 1}

    engine.transport = transport
    token = client.post("/api/analysis/estimate", headers=h, json={"mode": "pending"}).json()[
        "estimate_token"
    ]
    entered, release = threading.Event(), threading.Event()
    original = client.app.state.repo.import_files

    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)

    monkeypatch.setattr(client.app.state.repo, "import_files", delayed)
    results = {}

    def import_data():
        results["import"] = client.post(
            "/api/import",
            headers=h,
            json={
                "files": [{"name": "b.jsonl", "content": json.dumps({"note_id": "2", "desc": "新笔记"})}],
                "site": "rednote",
            },
        )

    def start():
        results["start"] = client.post("/api/analysis/start", headers=h, json={"estimate_token": token})

    t1 = threading.Thread(target=import_data)
    t1.start()
    assert entered.wait(2)
    t2 = threading.Thread(target=start)
    t2.start()
    release.set()
    t1.join(4)
    t2.join(4)
    engine.wait(2)
    assert results["import"].status_code == 200
    assert results["start"].status_code == 400
    assert calls == []


def test_env_secret_loads_but_file_and_secret_are_not_served(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("FIREFLY_API_KEY=LOCAL_TEST_SECRET\n", encoding="utf-8")
    with TestClient(create_app(tmp_path / "env.sqlite3", testing=True, env_path=env_path)) as client:
        response = client.get("/api/state")
        settings = response.json()["settings"]
        assert settings["has_api_key"] and settings["key_source"] == "env"
        assert settings["env_file"] == str(env_path)
        assert "LOCAL_TEST_SECRET" not in response.text
        assert client.get("/.env").status_code == 404
        assert client.get("/static/../.env").status_code == 404
        assert "LOCAL_TEST_SECRET" not in client.get("/api/export.csv").text
