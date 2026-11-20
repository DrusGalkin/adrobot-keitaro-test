import json

from fastapi.testclient import TestClient
import httpx
import pytest

from app.db import Base, Settings, make_engine
from app.main import create_app, rebalance
from app.tracker import KeitaroTracker


@pytest.fixture
def client(tmp_path):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    with TestClient(create_app(Settings(database_url=url, keitaro_mode="demo"))) as instance:
        yield instance


def create(client, geo="au", name="Test AU", activate=True):
    return client.post("/api/campaigns", json={
        "name": name, "geo": geo, "offer_id": 3749,
        "group_id": 1, "domain_id": 1, "traffic_source_id": 1,
        "activate": activate,
    })


def test_campaign_routes_and_catalog(client):
    assert client.get("/api/offers", params={"q": "3717"}).json()[0]["id"] == 3717
    assert create(client, "ZZ").status_code == 422
    response = create(client)
    assert response.status_code == 201
    campaign = response.json()
    assert campaign["name"] == "test_galkin_Test AU"
    geo, fallback = campaign["streams"]
    assert campaign["state"] == "active"
    assert campaign["domain_id"] == campaign["traffic_source_id"] == 1
    assert int(campaign["group_id"]) == 1
    assert geo["filters"] == [{"name": "country", "mode": "accept", "payload": ["AU"]}]
    assert geo["destination"] == "https://google.com"
    assert fallback["type"] == "default"
    assert fallback["offers"] == [{"offer_id": 3749, "state": "active", "share": 100}]


def test_campaign_prefix_is_not_duplicated(client):
    assert create(client, name="test_galkin_campaign1").json()["name"] == "test_galkin_campaign1"
    assert create(client, name="test_galkin_").status_code == 422


def test_edit_persists_and_checks_version(client):
    campaign = create(client).json()
    fallback = campaign["streams"][1]
    path = f"/api/campaigns/{campaign['id']}/streams/{fallback['id']}/offers"
    initial = {"offer_ids": [3749, 3717, 4021], "version": fallback["version"]}
    response = client.put(path, json=initial)
    assert response.status_code == 200
    saved = response.json()
    assert [o["share"] for o in saved["offers"]] == [34, 33, 33]
    assert client.put(path, json=initial).status_code == 409
    assert client.put(path, json={"offer_ids": [], "version": saved["version"]}).status_code == 422
    assert client.put(path, json={"offer_ids": [3749, 3749], "version": saved["version"]}).status_code == 422
    assert client.put(path, json={"offer_ids": [3717], "version": saved["version"]}).status_code == 200
    result = client.get(f"/api/campaigns/{campaign['id']}").json()
    assert result["streams"][1]["offers"][0]["name"] == "Miaflow · Резерв RO"
    assert result["streams"][1]["offers"][0]["share"] == 100


def test_disabled_offer_keeps_state():
    offers = rebalance([3, 2, 1], [{"offer_id": 2, "state": "disabled", "share": 12}])
    assert [offer["share"] for offer in offers] == [50, 0, 50]
    assert offers[1]["state"] == "disabled"


def test_single_offer_can_be_detached_and_reattached_in_disabled_campaign(client):
    campaign = create(client, activate=False).json()
    flow = campaign["streams"][1]
    path = f"/api/campaigns/{campaign['id']}/streams/{flow['id']}/offers"
    assert campaign["state"] == "disabled"
    removed = client.put(path, json={"offer_ids": [], "version": flow["version"]})
    assert removed.status_code == 200
    assert removed.json()["offers"] == []
    added = client.put(path, json={"offer_ids": [3749], "version": removed.json()["version"]})
    assert added.status_code == 200
    assert added.json()["offers"] == [{"offer_id": 3749, "state": "active", "share": 100}]


def test_live_adapter_writes_expected_payloads(tmp_path):
    url = f"sqlite:///{tmp_path / 'live-test.db'}"
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    calls = []
    campaign = {}
    streams = {}

    def keitaro(request):
        assert request.headers["api-key"] == "fixture"
        path = request.url.path.removeprefix("/admin_api/v1/")
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, path, body))
        if path.startswith("offers/"):
            return httpx.Response(200, json={"id": int(path.split("/")[-1]), "state": "active"})
        if path in {"domains", "groups", "traffic_sources"}:
            if path == "groups":
                assert request.url.params["type"] == "campaigns"
            return httpx.Response(200, json=[{"id": 1, "name": "test.example"}])
        if path == "campaigns":
            campaign.update(body, id=18)
            return httpx.Response(200, json=campaign)
        if path == "campaigns/18":
            campaign.update(body)
            return httpx.Response(200, json=campaign)
        if path == "streams":
            identifier = 24 + len(streams)
            streams[identifier] = {**body, "id": identifier}
            return httpx.Response(200, json=streams[identifier])
        if path == "streams/25":
            streams[25].update(body)
            return httpx.Response(200, json=streams[25])
        raise AssertionError(path)

    settings = Settings(database_url=url, keitaro_mode="live",
                        keitaro_url="https://test.example", keitaro_api_key="fixture")
    app = create_app(settings)
    with TestClient(app) as client, httpx.Client(
        transport=httpx.MockTransport(keitaro), base_url="https://test.example/admin_api/v1/",
        headers={"Api-Key": "fixture"},
    ) as transport:
        original = app.state.tracker
        app.state.tracker = KeitaroTracker(transport)
        try:
            response = create(client)
            assert response.status_code == 201
            assert calls[4][2]["state"] == "disabled"
            assert calls[5][2]["action_payload"] == "https://google.com"
            assert calls[6][2]["type"] == "default"
            assert calls[7] == ("PUT", "campaigns/18", {"state": "active"})
            version = response.json()["streams"][1]["version"]
            update = client.put("/api/campaigns/18/streams/25/offers", json={
                "offer_ids": [3749, 3717], "version": version,
            })
            assert update.status_code == 200
            assert set(calls[-1][2]) == {"offers"}
            assert [o["share"] for o in update.json()["offers"]] == [50, 50]
        finally:
            app.state.tracker = original
