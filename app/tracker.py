import httpx
from sqlalchemy import select

from app.db import Record


class TrackerError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


DEMO_METADATA = {
    "domains": [{"id": 1, "name": "tracker.example.test"}],
    "groups": [{"id": 1, "name": "FORTESTS"}],
    "sources": [{"id": 1, "name": "FORTESTS"}],
}
DEMO_OFFERS = [
    {"id": 3749, "name": "Miaflow · Основной RO"},
    {"id": 3717, "name": "Miaflow · Резерв RO"},
    {"id": 4021, "name": "Nova · MX"},
    {"id": 4086, "name": "Orbit · AU"},
]


class DemoTracker:
    def __init__(self, session):
        self.session = session

    def call(self, method, path, **kwargs):
        payload = kwargs.get("json", {})
        route, _, tail = path.partition("/")
        if route in {"domains", "groups", "traffic_sources"}:
            return DEMO_METADATA["sources" if route == "traffic_sources" else route]
        if route == "offers":
            if not tail:
                return DEMO_OFFERS
            return self.find(DEMO_OFFERS, int(tail))
        if route == "campaigns":
            rows = self.session.scalars(select(Record).where(Record.key.like("demo:%"))).all()
            if not tail:
                if method == "GET":
                    return [r.data["campaign"] for r in rows]
                identifier = max((r.data["campaign"]["id"] for r in rows), default=0) + 1
                campaign = {**payload, "id": identifier}
                self.session.add(Record(key=f"demo:{identifier}", data={"campaign": campaign, "streams": []}))
                self.session.flush()
                return campaign
            identifier, _, action = tail.partition("/")
            record = self.session.get(Record, f"demo:{identifier}")
            if record is None:
                raise TrackerError("Кампания не найдена.", 404)
            if action == "streams":
                return record.data["streams"]
            campaign = record.data["campaign"]
            if method == "DELETE":
                self.session.delete(record)
            elif method == "PUT":
                campaign = {**campaign, **payload}
                record.data = {**record.data, "campaign": campaign}
            return campaign
        if route == "streams":
            identifier = payload["campaign_id"] if method == "POST" else int(tail) // 100
            record = self.session.get(Record, f"demo:{identifier}")
            if record is None:
                raise TrackerError("Кампания не найдена.", 404)
            streams = record.data["streams"]
            if method == "POST":
                stream = {**payload, "id": identifier * 100 + len(streams) + 1}
                record.data = {**record.data, "streams": [*streams, stream]}
                return stream
            stream = self.find(streams, int(tail))
            if method == "GET":
                return stream
            updated = {**stream, **payload}
            record.data = {**record.data, "streams": [updated if s["id"] == updated["id"] else s for s in streams]}
            return updated
        raise TrackerError("Операция недоступна в деморежиме.")

    @staticmethod
    def find(items, identifier):
        for item in items:
            if item["id"] == identifier:
                return item
        raise TrackerError("Объект не найден.", 404)


class KeitaroTracker:
    def __init__(self, client):
        self.client = client

    def call(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise TrackerError("Keitaro недоступен. Результат последней операции может требовать проверки.") from exc
        if response.status_code == 404:
            raise TrackerError("Объект не найден в Keitaro.", 404)
        if response.is_error or response.is_redirect:
            raise TrackerError(f"Keitaro отклонил запрос (HTTP {response.status_code}).")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise TrackerError("Keitaro вернул ответ в неожиданном формате.") from exc
