from datetime import datetime

from app.features.purchases import router as purchases
from app.features.purchases.extraction import Extraction
from app.features.purchases.inbox import InboxAccess, require_inbox_access
from app.main import app
from test_purchases import INVOICE_MATCH_TEXT, STATEMENT_MATCH_TEXT, client, fixture_pdf, upload_evidence


INBOX = "/api/v1/purchase-inbox"


def scope(monkeypatch, organization, ids, writable=None):
    access = InboxAccess(organization, "staff-1", "구매 담당자", frozenset(ids),
                         frozenset(ids if writable is None else writable))
    monkeypatch.setitem(app.dependency_overrides, require_inbox_access, lambda: access)


def link(client, invoice, statement):
    response = client.post(f"/api/v1/documents/{invoice}/matching", json={"candidate_id": statement})
    assert response.status_code == 200
    return response.json()


def test_inbox_reuses_existing_differences_and_applies_filters(client, monkeypatch):
    monkeypatch.setattr(purchases, "korea_now", lambda: datetime(2026, 11, 19, 10))
    higher = INVOICE_MATCH_TEXT.replace("1,100,000", "1,210,000").replace(
        "1,000,000", "1,100,000").replace("100,000\n합계", "110,000\n합계")
    invoice = upload_evidence(client, monkeypatch, "documents", higher, "invoice")
    statement = upload_evidence(client, monkeypatch, "statements", STATEMENT_MATCH_TEXT, "statement")
    existing = link(client, invoice, statement)
    difference = existing["linked"]["comparison"]["amounts"]["total_amount"]["difference"]
    assert difference == 110_000

    other_invoice_text = INVOICE_MATCH_TEXT.replace("가상상사", "다른업체").replace("123-45-67890", "456-78-90123")
    other_invoice = upload_evidence(client, monkeypatch, "documents",
                                    other_invoice_text.replace("20261118-12345678-12345678", "20261118-12345678-76543210"), "complete-invoice")
    other_statement = upload_evidence(client, monkeypatch, "statements",
                                     STATEMENT_MATCH_TEXT.replace("가상상사", "다른업체").replace("123 45 67890", "456 78 90123"), "complete-statement")
    link(client, other_invoice, other_statement)
    third_invoice = upload_evidence(client, monkeypatch, "documents", INVOICE_MATCH_TEXT.replace("20261118-12345678-12345678",
                                    "20261118-12345678-99999999").replace("작성일자: 2026-11-18", "작성일자: 2026-11-28"), "candidate-invoice")
    third_statement = upload_evidence(client, monkeypatch, "statements",
                                     STATEMENT_MATCH_TEXT.replace("2026-11-18", "2026-11-28"), "candidate-statement")
    monkeypatch.setattr(purchases, "extract", lambda *_: Extraction("", "unavailable"))
    missing = client.post("/api/v1/statements/uploads?filename=missing.pdf", content=fixture_pdf("missing")).json()["record"]["id"]
    scope(monkeypatch, "org-a", (invoice, statement, other_invoice, other_statement, third_invoice, third_statement, missing))
    default = client.get(INBOX).json()
    assert default["start"] == "2026-11-01" and default["end"] == "2026-11-30"
    assert {row["id"] for row in default["rows"]} == {invoice, third_invoice, third_statement, missing}
    [row] = client.get(INBOX, params={"vendor": "가상 상사", "status": "difference", "start": "2026-11-18",
                                      "end": "2026-11-18"}).json()["rows"]
    assert (row["id"], row["invoice_amount"], row["statement_amount"], row["difference"]) == (
        invoice, 1_210_000, 1_100_000, difference,
    )
    assert [row["id"] for row in client.get(INBOX, params={"status": "complete"}).json()["rows"]] == [other_invoice]
    assert {row["id"] for row in client.get(INBOX, params={"status": "candidate"}).json()["rows"]} == {third_invoice, third_statement}
    assert client.get(INBOX, params={"status": "difference", "end": "2026-11-17"}).json()["rows"] == []
    assert client.get(INBOX, params={"status": "pending_invoice"}).json()["rows"] == []
    [unknown] = client.get(INBOX, params={"status": "check"}).json()["rows"]
    assert unknown["id"] == missing and unknown["evidence_date"] is None
    assert unknown["invoice_amount"] is None and unknown["statement_amount"] is None and unknown["difference"] is None


def test_inbox_memo_identity_scope_and_existing_evidence_unchanged(client, monkeypatch):
    monkeypatch.setattr(purchases, "korea_now", lambda: datetime(2026, 11, 19, 10))
    invoice = upload_evidence(client, monkeypatch, "documents", INVOICE_MATCH_TEXT, "visible")
    statement = upload_evidence(client, monkeypatch, "statements", STATEMENT_MATCH_TEXT, "hidden")
    base = f"/api/v1/documents/{invoice}"
    memo_route = f"{INBOX}/{invoice}/memos"
    hidden_memos = f"{INBOX}/{statement}/memos"
    text = {"text": "거래처에 수정 요청함"}
    assert client.get(INBOX, headers={"X-Organization-Id": "org-a"}).status_code == 403
    assert client.get(memo_route).status_code == 403
    assert client.post(memo_route, json=text).status_code == 403

    scope(monkeypatch, "org-a", [invoice])
    assert client.get(INBOX).json()["rows"][0]["status"] == "check"
    assert client.get(hidden_memos).status_code == 404
    assert client.post(hidden_memos, json=text).status_code == 404
    assert client.post(memo_route, json={**text, "author": "위조 작성자"}).status_code == 422
    stored_before = client.get(base).json()
    bytes_before = client.get(base + "/original").content
    comparison_before = client.get(base + "/matching").json()
    saved = client.post(memo_route, json=text)
    assert saved.status_code == 201
    assert saved.json()["author"] == "구매 담당자"
    assert saved.json()["created_at"].endswith("+09:00")
    assert client.get(memo_route).json() == [saved.json()]
    assert client.get(base).json() == stored_before and client.get(base + "/original").content == bytes_before
    assert client.get(base + "/matching").json() == comparison_before

    scope(monkeypatch, "org-b", [statement])
    assert client.get(memo_route).status_code == 404
    assert client.post(memo_route, json=text).status_code == 404
    assert all(row["id"] != invoice for row in client.get(INBOX).json()["rows"])
    scope(monkeypatch, "org-b", [invoice, statement], writable=[])
    assert client.get(memo_route).json() == []
    assert client.post(memo_route, json=text).status_code == 404
    scope(monkeypatch, "org-a", [invoice, statement])
    link(client, invoice, statement)
    assert client.get(hidden_memos).json() == [saved.json()]
