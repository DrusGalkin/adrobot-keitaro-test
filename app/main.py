from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from threading import Lock
from uuid import uuid4
import json

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field, PositiveInt, field_validator
import pycountry
from sqlalchemy.orm import Session

from app.db import Record, Settings, make_engine
from app.tracker import DemoTracker, KeitaroTracker, TrackerError

PUBLIC = Path(__file__).parent / "public"
GOOGLE = "https://google.com"


class CampaignInput(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    geo: str
    offer_id: PositiveInt
    domain_id: PositiveInt
    group_id: PositiveInt
    traffic_source_id: PositiveInt

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("geo")
    @classmethod
    def country(cls, value):
        value = value.strip().upper()
        if pycountry.countries.get(alpha_2=value) is None:
            raise ValueError("Введите двухбуквенный код страны ISO 3166-1.")
        return value


class OfferEdit(BaseModel):
    offer_ids: list[PositiveInt] = Field(min_length=1, max_length=100)
    version: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("offer_ids")
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("Оффер уже есть в списке.")
        return value


def offer_rows(stream):
    return [
        {"offer_id": int(o["offer_id"]), "share": int(o["share"]), "state": o.get("state") or "active"}
        for o in stream.get("offers") or []
    ]


def version(stream):
    data = [stream["id"], stream.get("updated_at"), offer_rows(stream)]
    return sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def editable(stream):
    return stream.get("schema") == "landings" and stream.get("state") != "deleted"


def rebalance(ids, current):
    states = {o["offer_id"]: o["state"] for o in current}
    enabled = [identifier for identifier in ids if states.get(identifier, "active") == "active"]
    if not enabled:
        raise TrackerError("Оставьте хотя бы один активный оффер.", 422)
    share, remainder = divmod(100, len(enabled))
    weights = {identifier: share + int(index < remainder) for index, identifier in enumerate(enabled)}
    return [
        {"offer_id": identifier, "state": states.get(identifier, "active"), "share": weights.get(identifier, 0)}
        for identifier in ids
    ]


def catalog(tracker):
    result = {}
    for group, path, params in (
        ("domains", "domains", {}), ("groups", "groups", {"type": "campaigns"}),
        ("sources", "traffic_sources", {}),
    ):
        items = tracker.call("GET", path, params=params)
        result[group] = [
            {"id": item["id"], "name": item.get("name") or str(item["id"])}
            for item in items if item.get("state") != "deleted"
        ]
    return result


def campaign_view(campaign, streams):
    return {
        "id": campaign["id"], "name": campaign["name"], "alias": campaign.get("alias"),
        "state": campaign.get("state"), "domain_id": campaign.get("domain_id"),
        "group_id": campaign.get("group_id"), "traffic_source_id": campaign.get("traffic_source_id"),
        "streams": [
            {
                "id": s["id"], "name": s["name"], "type": s.get("type"), "schema": s.get("schema"),
                "filters": s.get("filters") or [], "destination": s.get("action_payload"),
                "offers": offer_rows(s), "editable": editable(s), "version": version(s),
            }
            for s in sorted(streams, key=lambda s: (s.get("position") or 0, s["id"]))
            if s.get("state") != "deleted"
        ],
    }


def create_app(settings=None):
    cfg = settings or Settings()
    engine = make_engine(cfg.database_url)
    mutation = Lock()

    @asynccontextmanager
    async def lifespan(app):
        if cfg.keitaro_mode not in {"demo", "live"}:
            raise ValueError("KEITARO_MODE должен быть demo или live.")
        if cfg.keitaro_mode == "live":
            url = httpx.URL(cfg.keitaro_url)
            key = cfg.keitaro_api_key.get_secret_value()
            if url.scheme not in {"http", "https"} or not url.host or not key:
                raise ValueError("Укажите KEITARO_URL и KEITARO_API_KEY.")
            root = str(url).rstrip("/")
            if not root.endswith("/admin_api/v1"):
                root += "/admin_api/v1"
            app.state.tracker = KeitaroTracker(httpx.Client(
                base_url=root + "/", headers={"Api-Key": key}, timeout=12, follow_redirects=False,
            ))
        try:
            yield
        finally:
            if cfg.keitaro_mode == "live":
                app.state.tracker.client.close()
            engine.dispose()

    app = FastAPI(title="Adrobot Campaign Console", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=PUBLIC), name="static")

    def database():
        with Session(engine) as session:
            yield session

    def tracker(request: Request, db: Session = Depends(database)):
        return DemoTracker(db) if cfg.keitaro_mode == "demo" else request.app.state.tracker

    @app.exception_handler(TrackerError)
    async def tracker_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(PUBLIC / "index.html")

    @app.get("/api/catalog")
    def get_catalog(client=Depends(tracker)):
        return {"mode": cfg.keitaro_mode, "campaign_prefix": cfg.campaign_prefix, **catalog(client)}

    @app.get("/api/offers")
    def get_offers(q: str = Query("", max_length=150), client=Depends(tracker)):
        needle = q.strip().casefold()
        matches = [
            {"id": o["id"], "name": o["name"]}
            for o in client.call("GET", "offers")
            if o.get("state", "active") == "active" and (needle in o["name"].casefold() or needle in str(o["id"]))
        ]
        return matches[:30]

    @app.get("/api/campaigns")
    def get_campaigns(q: str = Query("", max_length=150), client=Depends(tracker)):
        needle = q.strip().casefold()
        return [
            {key: c.get(key) for key in ("id", "name", "state")}
            for c in client.call("GET", "campaigns")
            if c.get("state") != "deleted" and (needle in c["name"].casefold() or needle in str(c["id"]))
        ]

    @app.get("/api/campaigns/{campaign_id}")
    def get_campaign(campaign_id: PositiveInt, client=Depends(tracker)):
        view = campaign_view(client.call("GET", f"campaigns/{campaign_id}"),
                             client.call("GET", f"campaigns/{campaign_id}/streams"))
        if any(stream["offers"] for stream in view["streams"]):
            names = {int(offer["id"]): offer["name"] for offer in client.call("GET", "offers")}
            for stream in view["streams"]:
                for offer in stream["offers"]:
                    offer["name"] = names.get(offer["offer_id"])
        return view

    @app.post("/api/campaigns", status_code=201)
    def create_campaign(data: CampaignInput, client=Depends(tracker), db: Session = Depends(database)):
        prefix = cfg.campaign_prefix
        name = data.name
        if prefix and name[:len(prefix)].casefold() == prefix.casefold():
            name = name[len(prefix):].strip()
        if not name or len(prefix + name) > 150:
            raise TrackerError("Введите название. Длина вместе с префиксом должна быть не более 150 символов.", 422)
        with mutation:
            offer = client.call("GET", f"offers/{data.offer_id}")
            if offer.get("state", "active") != "active":
                raise TrackerError("Выберите активный оффер.", 422)
            metadata = catalog(client)
            for kind, selected in (("domains", data.domain_id), ("groups", data.group_id),
                                   ("sources", data.traffic_source_id)):
                if not any(int(item["id"]) == selected for item in metadata[kind]):
                    raise TrackerError("Выбранный домен, группа или источник недоступны.", 422)
            alias = uuid4().hex[:12]
            campaign = client.call("POST", "campaigns", json={
                "name": prefix + name, "alias": alias, "type": "position", "state": "disabled",
                "domain_id": data.domain_id, "group_id": str(data.group_id),
                "traffic_source_id": data.traffic_source_id,
            })
            identifier = campaign["id"]
            common = {"campaign_id": identifier, "state": "active", "collect_clicks": True}
            try:
                route = client.call("POST", "streams", json={
                    **common, "name": f"{data.geo} · Google", "type": "regular", "position": 1,
                    "schema": "redirect", "action_type": "http", "action_payload": GOOGLE,
                    "filters": [{"name": "country", "mode": "accept", "payload": [data.geo]}],
                })
                fallback = client.call("POST", "streams", json={
                    **common, "name": "Офферы", "type": "default", "position": 2,
                    "schema": "landings", "action_type": "http", "landings": [],
                    "offers": rebalance([data.offer_id], []),
                })
                campaign = client.call("PUT", f"campaigns/{identifier}", json={"state": "active"})
            except TrackerError as exc:
                if cfg.keitaro_mode == "live":
                    raise TrackerError(f"Настройка кампании #{identifier} прервана. Проверьте её состояние в Keitaro. {exc}") from exc
                raise
            db.add(Record(key=f"event:{uuid4().hex}", data={
                "operation": "create", "mode": cfg.keitaro_mode, "campaign_id": identifier,
            }))
            db.commit()
            result = campaign_view(campaign, [route, fallback])
            domain = next(item["name"] for item in metadata["domains"] if int(item["id"]) == data.domain_id)
            origin = domain.rstrip("/") if domain.startswith(("http://", "https://")) else "https://" + domain.rstrip("/")
            result["url"] = f"{origin}/{campaign.get('alias') or alias}"
            return result

    @app.put("/api/campaigns/{campaign_id}/streams/{stream_id}/offers")
    def edit_offers(campaign_id: PositiveInt, stream_id: PositiveInt, data: OfferEdit,
                    client=Depends(tracker), db: Session = Depends(database)):
        with mutation:
            stream = client.call("GET", f"streams/{stream_id}")
            if int(stream["campaign_id"]) != campaign_id:
                raise TrackerError("Поток не принадлежит этой кампании.", 404)
            if not editable(stream):
                raise TrackerError("Этот поток не поддерживает офферы.", 409)
            if version(stream) != data.version:
                raise TrackerError("Поток изменился. Загрузите свежие данные и повторите правки.", 409)
            current = offer_rows(stream)
            if [o["offer_id"] for o in current] == data.offer_ids:
                return {"offers": current, "version": version(stream)}
            new_ids = set(data.offer_ids) - {o["offer_id"] for o in current}
            for identifier in new_ids:
                if client.call("GET", f"offers/{identifier}").get("state", "active") != "active":
                    raise TrackerError("Можно добавить только активный оффер.", 422)
            offers = rebalance(data.offer_ids, current)
            updated = client.call("PUT", f"streams/{stream_id}", json={"offers": offers})
            db.add(Record(key=f"event:{uuid4().hex}", data={
                "operation": "edit_offers", "mode": cfg.keitaro_mode, "campaign_id": campaign_id,
                "stream_id": stream_id, "before": current, "after": offer_rows(updated),
            }))
            db.commit()
            return {"offers": offer_rows(updated), "version": version(updated)}

    return app


app = create_app()
