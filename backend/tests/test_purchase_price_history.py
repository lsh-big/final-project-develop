from datetime import datetime
from hashlib import sha256
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.features.purchases import router as purchases
from app.features.purchases import inbox
from app.features.purchases.inbox import InboxAccess
from app.features.purchases.model import PurchaseRecord
from app.features.purchases.price_history import require_price_access
from app.main import app
from test_purchases import client, fixture_pdf


PRICE_API = "/api/v1/purchase-prices"


def seed_statement(name, day, items, *, vendor="가상상사", business="123-45-67890", status="approved"):
    # Metadata represents an already stored statement with an explicit comparison basis.
    record_id = uuid4().hex
    original = fixture_pdf(name)
    with purchases.Session.begin() as db:
        db.add(PurchaseRecord(
            id=record_id, filename=f"{name}.pdf", kind="statement", file_hash=sha256(original).hexdigest(),
            media_type="application/pdf", original=original, document_type="trade_statement", status=status,
            fields={"vendor": vendor, "business_number": business, "transaction_date": day, "items": items},
            extraction={"page_count": 1}, created_at=datetime(2026, 11, 19, 10),
        ))
    return record_id


def access(monkeypatch, ids):
    scope = InboxAccess("org-a", "staff-1", "담당자", frozenset(ids), frozenset())
    monkeypatch.setitem(app.dependency_overrides, require_price_access, lambda: scope)
    monkeypatch.setattr(purchases, "korea_now", lambda: datetime(2026, 11, 19, 10))


def item(price, *, name="제어용 릴레이", code="R-23", unit="개", tax_basis="exclusive"):
    return {"name": name, "quantity": 5, "unit_price": price, "item_code": code, "unit": unit, "tax_basis": tax_basis}


def test_price_history_uses_previous_matching_basis_and_protects_scope(client, monkeypatch):
    previous = seed_statement("previous", "2026-10-19", [item(120, name="이전 릴레이 이름")])
    wrong_vendor = seed_statement("different vendor", "2026-11-15", [item(50)], business="456-78-90123")
    wrong_unit = seed_statement("box", "2026-11-16", [item(300, unit="박스")])
    wrong_tax = seed_statement("different VAT", "2026-11-17", [item(200, tax_basis="inclusive")])
    wrong_code = seed_statement("different code", "2026-11-18", [item(80, code="R-24")])
    outside_org = seed_statement("organization B", "2026-11-18", [item(75)])
    unapproved = seed_statement("unapproved", "2026-11-18", [item(1)], status="review")
    latest = seed_statement("latest", "2026-11-19", [item(150), item(180, code="NO-PREV", unit="세트")])

    assert client.get(PRICE_API).status_code == 403
    assert client.get(f"{PRICE_API}/{latest}/1", headers={"X-Organization-Id": "org-a"}).status_code == 403
    access(monkeypatch, (previous, wrong_vendor, wrong_unit, wrong_tax, wrong_code, unapproved, latest))
    data = client.get(PRICE_API).json()
    assert (data["start"], data["end"]) == ("2026-10-21", "2026-11-19")
    assert data["summary"] == {"rise": 1, "fall": 0, "insufficient": 5}
    first = data["rows"][0]
    assert (first["record_id"], first["previous_unit_price"], first["unit_price"], first["difference"], first["rate"]) == (
        latest, "120", "150", "30", "25.00",
    )
    detail = client.get(f"{PRICE_API}/{latest}/1").json()
    assert detail["previous"]["record_id"] == previous and detail["current"]["record_id"] == latest
    assert [row["record_id"] for row in detail["history"]] == [latest, previous]
    assert client.get(f"/api/v1/statements/{previous}/original").status_code == 200
    assert client.get(f"{PRICE_API}/{outside_org}/1").status_code == 404
    assert client.get(f"{PRICE_API}/{unapproved}/1").status_code == 404
    filtered = client.get(PRICE_API, params={
        "start": "2026-11-19", "end": "2026-11-19", "vendor": "가상 상사", "item": "R-23", "rises_only": True,
    }).json()
    assert filtered["rows"] == [first]
    assert client.get(PRICE_API, params={"vendor": "다른 거래처"}).json()["rows"] == []
    assert client.get(PRICE_API, params={"start": "2026-11-20", "end": "2026-11-19"}).status_code == 422


def test_ranking_counts_and_missing_comparison_data(client, monkeypatch):
    zero = seed_statement("free sample", "2026-11-10", [item(0, code="ZERO")])
    nonzero = seed_statement("paid unit", "2026-11-19", [item(150, code="ZERO")])
    before = seed_statement("base values", "2026-11-12", [
        item(100, code="UP"), item(120, code="DOWN"), item(140, code="MISSING"),
        {"name": "기존 품목", "quantity": 1, "unit_price": 120},
    ])
    missing = seed_statement("missing price", "2026-11-18", [item(None, code="MISSING")])
    after = seed_statement("comparison values", "2026-11-19", [
        item(150, code="UP"), item(90, code="DOWN"), item(150, code="MISSING"),
        {"name": "기존 품목", "quantity": 1, "unit_price": 150},
    ])
    access(monkeypatch, (zero, nonzero, before, missing, after))
    result = client.get(PRICE_API, params={"start": "2026-11-19"}).json()
    assert result["summary"] == {"rise": 2, "fall": 1, "insufficient": 8}
    assert [row["direction"] for row in result["rows"]] == ["rise", "rise", "fall", "insufficient", "insufficient"]
    paid = result["rows"][1]
    assert (paid["record_id"], paid["previous_unit_price"], paid["difference"], paid["rate"]) == (nonzero, "0", "150", None)
    assert result["rows"][2]["difference"] == "-30"
    assert all(row["difference"] is None for row in result["rows"][-2:])
    old = client.get(f"{PRICE_API}/{after}/4").json()
    assert old["previous"] is None and old["current"]["unit_price"] == "150"
    assert client.get(f"{PRICE_API}/{after}/3").json()["previous"]["record_id"] == missing


def test_local_price_read_is_limited_to_opted_in_default_database(tmp_path, monkeypatch):
    path = tmp_path / "purchase-records.sqlite3"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    monkeypatch.setattr(inbox, "DATA_DIR", tmp_path)
    monkeypatch.setattr(purchases, "engine", engine)
    monkeypatch.setattr(purchases, "Session", sessionmaker(engine, expire_on_commit=False))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("AUTOFLOW_LOCAL_PRICE_HISTORY", raising=False)
    monkeypatch.delenv("AUTOFLOW_LOCAL_PURCHASE_INBOX", raising=False)
    try:
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as local:
            assert local.get(PRICE_API).status_code == 403
            monkeypatch.setenv("AUTOFLOW_LOCAL_PRICE_HISTORY", "1")
            record = seed_statement("local", "2026-11-19", [item(150)])
            assert local.get(f"{PRICE_API}/{record}/1").status_code == 200
            assert local.get(PRICE_API, headers={"Host": "untrusted.example"}).status_code == 403
            assert local.get(PRICE_API, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
            assert local.get("/api/v1/purchase-inbox").status_code == 403
            monkeypatch.setenv("AUTOFLOW_LOCAL_PURCHASE_INBOX", "1")
            inbox_api = "/api/v1/purchase-inbox"
            inbox_page = local.get(inbox_api, params={"start": "2026-11-19", "end": "2026-11-19"})
            assert inbox_page.status_code == 200 and not inbox_page.json()["can_read_memos"]
            assert [row["id"] for row in inbox_page.json()["rows"]] == [record]
            assert local.get(f"{inbox_api}/{record}/memos").status_code == 403
            assert local.post(f"{inbox_api}/{record}/memos", json={"text": "test"}).status_code == 403
            monkeypatch.delenv("AUTOFLOW_LOCAL_PRICE_HISTORY")
            assert local.get(PRICE_API).status_code == 403
            monkeypatch.setenv("AUTOFLOW_LOCAL_PRICE_HISTORY", "1")
            with TestClient(app, base_url="http://127.0.0.1", client=("192.168.2.10", 50000)) as remote:
                assert remote.get(PRICE_API).status_code == 403
                assert remote.get(inbox_api).status_code == 403
            monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path.as_posix()}")
            assert local.get(PRICE_API).status_code == 403
            assert local.get(inbox_api).status_code == 403
    finally:
        engine.dispose()
