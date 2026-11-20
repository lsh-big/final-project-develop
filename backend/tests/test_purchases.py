from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image
import pymupdf
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.features.purchases import router as purchases
from app.features.purchases import mobile
from app.features.purchases import extraction
from app.features.purchases.extraction import Extraction, parse_fields
from app.features.purchases.model import PurchaseRecord
from app.main import app


STATEMENT_TEXT = """거래명세서
공급자 상호: 가상상사
작성일자: 2026-11-18
명세서 번호: DEMO-42
품목명   수량    단가     금액
복사용지   2    12,000    24,000
절연 케이블   1.5    4,000    6,000
합계금액: 30,000원
"""


def test_statement_extracts_multiple_item_rows():
    document_type, fields = parse_fields("statement", STATEMENT_TEXT)
    assert document_type == "trade_statement"
    assert fields["vendor"] == "가상상사"
    assert fields["transaction_date"] == "2026-11-18"
    assert fields["statement_number"] == "DEMO-42"
    assert fields["total_amount"] == 30_000
    assert fields["items"] == [
        {"name": "복사용지", "quantity": 2, "unit_price": 12_000},
        {"name": "절연 케이블", "quantity": 1.5, "unit_price": 4_000},
    ]
    single_space = "거래명세표\n품목명 수량 단가 금액\n절연 케이블 1.5 4000 6000"
    assert parse_fields("statement", single_space)[1]["items"] == [fields["items"][1]]
    assert parse_fields("statement", "전자세금계산서")[0] == "unknown"


@pytest.mark.parametrize("delivery", [
    "명세서 번호    DEMO-DN-0001       납품일         2026-11-03",
    "납 품 일 자 :\n    2026년 11월 3일",
    "| 작성일자 | 납품일자 |\n| 2026-11-18 | 2026–11–03 |",
])
def test_statement_prefers_delivery_date_over_issue_date(delivery):
    _, fields = parse_fields("statement", STATEMENT_TEXT + delivery)
    assert fields["transaction_date"] == "2026-11-03"


def test_statement_reads_full_supplier_name_from_paired_columns():
    text = """거래명세서
공급자             공급받는 자
상호     가상 솔빛제어      상호      가상 시연구매팀
"""
    for clipped_supplier in ("", "상호  가상 솔빛"):
        _, fields = parse_fields("statement", text.replace("\n", "\r\n"), clipped_supplier)
        assert fields["vendor"] == "가상 솔빛제어"


def test_receipt_reads_unlabeled_merchant_from_header():
    text = """
    시연용 가상 영수증
    가상 새봄전기
    카드 매출전표
    사업자번호   123-45-67890
    거래일시     2026-11-18 14:25
    """
    _, fields = parse_fields("receipt", text)
    assert fields["vendor"] == "가상 새봄전기"
    _, noisy = parse_fields("receipt", text.replace("카드 매출전표", "카드 매출전표\nine"))
    assert noisy["vendor"] == "가상 새봄전기"
    _, distinct = parse_fields("receipt", text.replace("카드 매출전표", "카드 매출전표\nABC"))
    assert distinct["vendor"] == ""
    _, ambiguous = parse_fields("receipt", text.replace("가상 새봄전기", "가상 새봄전기\n다른 상호"))
    assert ambiguous["vendor"] == ""


def test_invoice_uses_supplier_section_only():
    text = """
    전자 세금계산서
    공급자
    사업자등록번호: 123-45-67890
    공급받는 자
    상호: 삼일엘텍
    사업자등록번호: 321-54-09876
    공급가액: 120,000
    """
    document_type, fields = parse_fields("document", text)
    assert document_type == "tax_invoice"
    assert fields["vendor"] == ""
    assert fields["business_number"] == "123-45-67890"
    assert fields["supply_amount"] == 120_000
    assert fields["tax_amount"] is None


def test_invoice_reads_supplier_number_from_side_by_side_columns():
    text = """
    전자 세금계산서 (공급받는 자 보관용)
    공급자              공급받는 자
    사업자등록번호:    123–45–67890     사업자등록번호:    321-54-09876
    작성일자: 2026-11-18
    """
    _, fields = parse_fields("document", text, "사업자등록번호: 123–45")
    assert fields["business_number"] == "123-45-67890"
    _, missing = parse_fields("document", text.replace("123–45–67890", ""))
    assert missing["business_number"] == ""


def test_invoice_reads_values_in_the_row_below_their_headers():
    text = """
    전자 세금계산서
    공급자: 가상상사
    공급받는 자: 삼일엘텍
    작성일자     공급가액     세액     합계금액
    2026-11-18   120,000      12,000   132,000
    """
    document_type, fields = parse_fields("document", text)
    assert document_type == "tax_invoice"
    assert fields["vendor"] == "가상상사"
    assert fields["transaction_date"] == "2026-11-18"
    assert fields["supply_amount"] == 120_000
    assert fields["tax_amount"] == 12_000
    assert fields["total_amount"] == 132_000


@pytest.fixture
def client(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'test.sqlite3').as_posix()}")
    monkeypatch.setattr(purchases, "engine", engine)
    monkeypatch.setattr(purchases, "Session", sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(purchases, "extract", lambda *_: Extraction("", "unavailable"))
    with TestClient(app) as client:
        yield client
    engine.dispose()


@pytest.mark.parametrize(("image_format", "suffix"), [("PNG", "png"), ("JPEG", "jpg")])
def test_concurrent_upload_and_approval_are_idempotent(client, image_format, suffix):
    prefix = "/api/v1/receipts"
    image = BytesIO()
    Image.new("RGB", (8, 8)).save(image, format=image_format)

    def upload(n):
        return client.post(f"{prefix}/uploads", params={"filename": f"test-{n}.{suffix}"},
                           content=image.getvalue()).json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(upload, range(2))
    assert {first["duplicate"], second["duplicate"]} == {False, True}
    record_id = first["record"]["id"]
    assert second["record"]["id"] == record_id

    def approve(vendor):
        response = client.post(f"{prefix}/{record_id}/approve", json={
            "document_type": "receipt",
            "fields": {
                "vendor": vendor, "transaction_date": "2026-11-18T14:25",
                "approval_number": "00123456", "total_amount": 33_000,
            },
        })
        assert response.status_code == 200
        return response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = executor.map(approve, ("=2+2", "=3+3"))
    assert first["fields"] == second["fields"]
    assert first["approved_at"] == second["approved_at"]

    history = client.get(f"{prefix}/history", params={"status": "approved"}).json()
    assert len(history) == 1
    response = client.get(f"{prefix}/export", params={"status": "approved"})
    sheet = load_workbook(BytesIO(response.content)).active
    assert sheet["D2"].data_type == "s"
    assert sheet["K2"].data_type == "n"
    assert sheet["K2"].value == 33_000


def test_saved_draft_remains_pending_and_can_be_reopened(client, monkeypatch):
    today = datetime(2026, 11, 18)
    monkeypatch.setattr(purchases, "korea_now", lambda: today)
    image = BytesIO()
    Image.new("RGB", (8, 8)).save(image, format="PNG")
    receipt = client.post("/api/v1/receipts/uploads?filename=test.png", content=image.getvalue()).json()["record"]
    path = f"/api/v1/receipts/{receipt['id']}"
    count_path = "/api/v1/documents/review-count"
    summary_path = f"{count_path}?include_completed=true"
    assert client.get(count_path).json() == {"count": 1}
    assert client.get(summary_path).json() == {"count": 1, "completed_today": 0}
    fields = {"vendor": "수정한 거래처", "transaction_date": "", "approval_number": "", "total_amount": None}
    saved = client.put(f"{path}/draft", json={"fields": fields}).json()
    assert saved["status"] == "review"
    assert saved["draft_saved_at"]
    assert client.get(path).json()["fields"] == fields
    assert client.get(count_path).json() == {"count": 1}

    complete = {**fields, "transaction_date": "2026-11-18", "approval_number": "00123456", "total_amount": 33000}
    approved = client.post(f"{path}/approve", json={"fields": complete, "document_type": "receipt"})
    assert approved.status_code == 200
    assert client.get(count_path).json() == {"count": 0}
    assert client.get(summary_path).json() == {"count": 0, "completed_today": 1}
    assert client.put(f"{path}/draft", json={"fields": fields}).status_code == 409
    assert client.get(path).json()["fields"] == complete
    trashed = client.delete(path).json()
    assert trashed["status"] == "trashed"
    assert client.get(summary_path).json() == {"count": 0, "completed_today": 0}
    assert client.get("/api/v1/documents/history?status=approved").json() == []
    workbook = load_workbook(BytesIO(client.get("/api/v1/documents/export?status=approved").content))
    assert workbook.active.max_row == 1
    restored = client.post(f"{path}/restore").json()
    assert restored["status"] == "approved"
    assert restored["approved_at"] == approved.json()["approved_at"]
    assert restored["fields"] == complete
    assert client.get(count_path).json() == {"count": 0}
    assert client.get(summary_path).json() == {"count": 0, "completed_today": 1}
    monkeypatch.setattr(purchases, "korea_now", lambda: today + timedelta(days=1))
    assert client.get(summary_path).json() == {"count": 0, "completed_today": 0}


def test_trash_hides_record_and_restore_preserves_file_and_draft(client):
    image = BytesIO()
    Image.new("RGB", (8, 8)).save(image, format="PNG")
    prefix = "/api/v1/receipts"
    upload = f"{prefix}/uploads?filename=test.png"
    record = client.post(upload, content=image.getvalue()).json()["record"]
    path = f"{prefix}/{record['id']}"
    fields = {"vendor": "수정 거래처", "transaction_date": "", "approval_number": "", "total_amount": None}
    assert client.put(f"{path}/draft", json={"fields": fields}).status_code == 200
    assert client.delete(path).json()["status"] == "trashed"
    assert client.get(f"{prefix}/review-count").json() == {"count": 0}
    assert client.get(f"{prefix}/history").json() == []
    assert client.get(f"{prefix}/history?status=trashed").json()[0]["id"] == record["id"]
    assert load_workbook(BytesIO(client.get(f"{prefix}/export").content)).active.max_row == 1
    assert client.put(f"{path}/draft", json={"fields": fields}).status_code == 409
    assert client.post(f"{path}/approve", json={"fields": fields, "document_type": "receipt"}).status_code == 409
    duplicate = client.post(upload, content=image.getvalue()).json()
    assert duplicate["duplicate"]
    assert duplicate["record"]["id"] == record["id"]
    assert duplicate["record"]["status"] == "trashed"
    restored = client.post(f"{path}/restore").json()
    assert restored["status"] == "review"
    assert restored["fields"] == fields
    assert client.get(f"{path}/original").content == image.getvalue()
    assert client.get(f"{prefix}/review-count").json() == {"count": 1}


def test_permanent_deletion_only_removes_trashed_records(client):
    def upload(kind, filename, content):
        response = client.post(f"/api/v1/{kind}/uploads", params={"filename": filename}, content=content)
        assert response.status_code == 200
        return f"/api/v1/{kind}/{response.json()['record']['id']}"

    image = BytesIO()
    Image.new("RGB", (8, 8)).save(image, format="PNG")
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf_bytes = pdf.tobytes()
    receipt = upload("receipts", "receipt.png", image.getvalue())
    statement = upload("statements", "statement.pdf", pdf_bytes)
    with pymupdf.open() as pdf:
        pdf.new_page(width=500)
        document_bytes = pdf.tobytes()
    document = upload("documents", "document.pdf", document_bytes)
    bulk = "/api/v1/documents/trash"

    assert client.delete(f"{receipt}/permanent").status_code == 409
    assert client.delete(receipt).status_code == 200
    assert client.delete(f"{receipt}/permanent").status_code == 204
    assert client.get(f"{receipt}/original").status_code == 404
    assert client.post(f"{receipt}/restore").status_code == 404
    receipt = upload("receipts", "receipt.png", image.getvalue())
    assert client.delete(receipt).status_code == 200
    assert client.delete(statement).status_code == 200
    assert client.delete(bulk).json() == {"deleted": 2}
    assert client.delete(bulk).json() == {"deleted": 0}
    assert client.get(receipt).status_code == client.get(statement).status_code == 404
    assert client.get(f"{document}/original").content == document_bytes


INVOICE_MATCH_TEXT = """전자 세금계산서
공급자 상호: 가상상사
공급자 사업자등록번호: 123-45-67890
공급받는 자: 가상구매팀
작성일자: 2026-11-18
승인번호: 20261118-12345678-12345678
공급가액: 1,000,000
세액: 100,000
합계금액: 1,100,000
"""
STATEMENT_MATCH_TEXT = """거래명세서
공급자 상호: 가상상사
공급자 사업자등록번호: 123 45 67890
납품일자: 2026-11-18
공급가액: 1,000,000
부가세: 100,000
합계금액: 1,100,000
"""


def fixture_pdf(marker):
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((50, 50), marker)
        return pdf.tobytes()


def upload_evidence(client, monkeypatch, kind, text, marker):
    monkeypatch.setattr(purchases, "extract", lambda *_: Extraction(text, "pdf_text"))
    result = client.post(f"/api/v1/{kind}/uploads", params={"filename": f"{marker}.pdf", "confirm_suspected": "true"},
                         content=fixture_pdf(marker))
    assert result.status_code == 200
    return result.json()["record"]["id"]


def test_evidence_matching_comparison_and_unlink(client, monkeypatch):
    statement_id = upload_evidence(client, monkeypatch, "statements", STATEMENT_MATCH_TEXT, "statement")
    statement = f"/api/v1/statements/{statement_id}/matching"
    assert client.get(statement).json()["candidates"] == []
    higher = INVOICE_MATCH_TEXT.replace("1,100,000", "1,210,000").replace("1,000,000", "1,100,000").replace("100,000\n합계", "110,000\n합계")
    higher_id = upload_evidence(client, monkeypatch, "documents", higher, "higher")
    exact = INVOICE_MATCH_TEXT.replace("12345678-12345678", "12345678-12345679")
    exact_id = upload_evidence(client, monkeypatch, "documents", exact, "exact")
    wrong = exact.replace("123-45-67890", "999-45-67890").replace("12345679", "12345680")
    upload_evidence(client, monkeypatch, "documents", wrong, "wrong-supplier")
    distant = exact.replace("2026-11-18", "2026-11-10").replace("12345679", "12345681")
    upload_evidence(client, monkeypatch, "documents", distant, "distant")
    response = client.get(statement).json()
    assert response["status"] == "check"
    assert [candidate["record"]["id"] for candidate in response["candidates"]] == [exact_id, higher_id]
    assert response["candidates"][1]["comparison"]["amounts"]["total_amount"]["difference"] == 110_000
    invoice = f"/api/v1/documents/{exact_id}/matching"
    assert client.get(invoice).json()["candidates"][0]["record"]["id"] == statement_id
    linked = client.post(invoice, json={"candidate_id": statement_id}).json()
    assert linked["status"] == "linked"
    assert linked["linked"]["comparison"]["amount_review"] == "matched"
    assert client.get(statement).json()["linked"]["record"]["id"] == exact_id
    assert client.delete(statement).status_code == 204
    assert client.get(invoice).json()["linked"] is None
    linked = client.post(statement, json={"candidate_id": higher_id}).json()
    assert linked["linked"]["record"]["id"] == higher_id
    assert linked["status"] == "check"
    assert linked["linked"]["comparison"]["amount_review"] == "check"


def test_evidence_match_with_missing_fields_and_single_concurrent_link(client, monkeypatch):
    invoice_id = upload_evidence(client, monkeypatch, "documents", INVOICE_MATCH_TEXT, "invoice")
    low_confidence = STATEMENT_MATCH_TEXT.replace("공급자 사업자등록번호: 123 45 67890\n", "").replace("부가세: 100,000\n", "")
    first = upload_evidence(client, monkeypatch, "statements", low_confidence, "low-confidence")
    second = upload_evidence(client, monkeypatch, "statements", STATEMENT_MATCH_TEXT.replace("2026-11-18", "2026-11-19"), "other")
    invoice = f"/api/v1/documents/{invoice_id}/matching"
    candidates = client.get(invoice).json()["candidates"]
    candidate = next(candidate for candidate in candidates if candidate["record"]["id"] == first)
    assert candidate["confidence"] == "low"
    assert candidate["comparison"]["tax_unclear"]
    assert candidate["comparison"]["amounts"]["tax_amount"] == {"invoice": 100_000, "statement": None, "difference": None}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda target: client.post(invoice, json={"candidate_id": target}), (first, second)))
    assert sorted(response.status_code for response in results) == [200, 409]
    assert sum(client.get(f"/api/v1/statements/{target}/matching").json()["linked"] is not None for target in (first, second)) == 1


def test_upload_duplicate_file_approval_and_confirmable_suspect(client, monkeypatch):
    monkeypatch.setattr(purchases, "extract", lambda *_: Extraction(INVOICE_MATCH_TEXT, "pdf_text"))
    original = fixture_pdf("original")
    url = "/api/v1/documents/uploads"
    different = fixture_pdf("different bytes")
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda content: client.post(url, params={"filename": "invoice.pdf"}, content=content),
                                  (original, different)))
    assert [response.status_code for response in responses] == [200, 200]
    results = [response.json() for response in responses]
    assert sum(result["duplicate"] for result in results) == 1
    assert {result["record"]["id"] for result in results} == {results[0]["record"]["id"]}
    assert next(result for result in results if result["duplicate"])["duplicate_reason"] == "approval_number"
    stored = original if not results[0]["duplicate"] else different
    exact = client.post(url, params={"filename": "renamed.pdf"}, content=stored).json()
    assert exact["duplicate_reason"] == "file"
    assert client.post("/api/v1/statements/uploads?filename=copy.pdf", content=stored).json()["duplicate_reason"] == "file"
    assert len(client.get("/api/v1/documents/history").json()) == 1

    receipt_text = "상호: 가상상사\n거래일시: 2026-11-18 14:25\n합계금액: 12,000"
    monkeypatch.setattr(purchases, "extract", lambda *_: Extraction(receipt_text, "ocr"))
    images = []
    for color in ("red", "blue"):
        image = BytesIO()
        Image.new("RGB", (8, 8), color).save(image, format="PNG")
        images.append(image.getvalue())
    receipt = "/api/v1/receipts/uploads?filename=receipt.png"
    first = client.post(receipt, content=images[0]).json()["record"]
    warning = client.post(receipt, content=images[1])
    assert warning.status_code == 409 and warning.json()["detail"]["code"] == "suspected_duplicate"
    assert len(client.get("/api/v1/receipts/history?document_type=receipt").json()) == 1
    confirmed = client.post(receipt + "&confirm_suspected=true", content=images[1]).json()
    assert not confirmed["duplicate"] and confirmed["record"]["id"] != first["id"]


def test_additive_migration_preserves_legacy_record_and_upload(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.sqlite3').as_posix()}")
    PurchaseRecord.__table__.create(engine)
    session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(purchases, "engine", engine)
    monkeypatch.setattr(purchases, "Session", session)
    legacy_pdf = fixture_pdf("legacy")
    with session.begin() as db:
        db.add(PurchaseRecord(id="a" * 32, filename="legacy.pdf", kind="document", file_hash="f" * 64,
                              media_type="application/pdf", original=legacy_pdf, document_type="tax_invoice",
                              fields={"approval_number": "20261118-12345678-12345678"}, extraction={},
                              created_at=purchases.korea_now()))
    monkeypatch.setattr(purchases, "extract", lambda *_: Extraction(INVOICE_MATCH_TEXT, "pdf_text"))
    purchases.init_db()
    with TestClient(app) as client:
        assert {"purchase_links", "purchase_file_claims"} <= set(inspect(engine).get_table_names())
        path = "/api/v1/documents/" + "a" * 32
        assert client.get(path).status_code == 200
        assert client.get(path + "/original").content == legacy_pdf
        assert client.get(path + "/matching").json()["status"] == "check"
        duplicate = client.post("/api/v1/documents/uploads?filename=new.pdf", content=fixture_pdf("new")).json()
        assert duplicate["duplicate_reason"] == "approval_number"
        monkeypatch.setattr(purchases, "extract", lambda *_: Extraction(STATEMENT_MATCH_TEXT, "pdf_text"))
        new = client.post("/api/v1/statements/uploads?filename=new.pdf", content=fixture_pdf("new statement")).json()
        assert not new["duplicate"]
    engine.dispose()


def test_pdf_type_detection_uses_document_content(client, monkeypatch):
    for selected, text, expected_kind, expected_type in (
        ("statements", INVOICE_MATCH_TEXT, "document", "tax_invoice"),
        ("documents", STATEMENT_MATCH_TEXT, "statement", "trade_statement"),
    ):
        monkeypatch.setattr(purchases, "extract", lambda *_: Extraction(text, "pdf_text"))
        response = client.post(f"/api/v1/{selected}/uploads", params={"filename": "evidence.pdf"},
                               content=fixture_pdf(selected))
        assert response.status_code == 200
        record = response.json()["record"]
        assert (record["kind"], record["document_type"]) == (expected_kind, expected_type)
        assert record["fields"]["total_amount"] == 1_100_000


def test_reupload_repairs_misclassified_legacy_pdf_in_place(client, monkeypatch):
    file = fixture_pdf("old")
    upload = "/api/v1/statements/uploads?filename=invoice.pdf"
    old = client.post(upload, content=file).json()["record"]
    assert old["document_type"] == "unknown"
    monkeypatch.setattr(purchases, "extract", lambda *_: Extraction(INVOICE_MATCH_TEXT, "pdf_text"))
    result = client.post(upload, content=file).json()
    assert result["duplicate"]
    assert result["record"]["id"] == old["id"]
    assert result["record"]["kind"] == "document"
    assert result["record"]["fields"]["approval_number"] == "20261118-12345678-12345678"
    assert len(client.get("/api/v1/documents/history").json()) == 1


def test_receipt_uses_sparse_ocr_when_block_layout_loses_fields(monkeypatch):
    def ocr(_image, page_mode=6):
        if page_mode == 6:
            return "카드 영수증"
        return ("카드 영수증\n가맹점 명:\n가상문구\n결제일시:\n2026.11.18 14시 25분"
                "\n승인번호:\n00123456\n합계금액: 20,000\n결제금액:\n33 , 000원")

    monkeypatch.setattr(extraction, "ocr", ocr)
    text = extraction.read_ocr("receipt", b"image")
    _, fields = parse_fields("receipt", text)
    assert fields == {"vendor": "가상문구", "transaction_date": "2026-11-18T14:25",
                      "approval_number": "00123456", "total_amount": 33000}


def test_mobile_qr_delivers_one_receipt_and_expires(client, monkeypatch):
    monkeypatch.setattr(mobile, "Session", purchases.Session)
    monkeypatch.setenv("RECEIPT_MOBILE_URL", "http://192.168.1.10:8010")
    sessions = "/api/v1/receipts/mobile-sessions"
    with TestClient(mobile.mobile_app) as phone:
        assert phone.get("/").status_code == 200
        assert phone.get("/api/v1/receipts/history").status_code == 404
        ticket = client.post(sessions).json()
        assert ticket["qr_image"].startswith("data:image/png;base64,")
        status_path = f"{sessions}/{ticket['id']}"
        assert client.get(status_path).json()["state"] == "waiting"
        auth = {"Authorization": f"Bearer {urlsplit(ticket['url']).fragment}"}
        path = "/upload?filename=receipt.png"
        assert phone.post(path, content=b"no token").status_code == 410
        assert phone.post(path, content=b"bad photo", headers=auth).status_code == 422
        assert client.get(status_path).json()["state"] == "waiting"
        image = BytesIO()
        Image.new("RGB", (8, 8)).save(image, format="PNG")
        assert phone.post(path, content=image.getvalue(), headers=auth).json()["received"]
        delivery = client.get(status_path).json()
        assert delivery["state"] == "received"
        assert client.get(f"/api/v1/receipts/{delivery['record_id']}").json()["status"] == "review"
        assert phone.post(path, content=image.getvalue(), headers=auth).status_code == 410

        expired = client.post(sessions).json()
        future = mobile.korea_now() + timedelta(minutes=6)
        monkeypatch.setattr(mobile, "korea_now", lambda: future)
        expired_auth = {"Authorization": f"Bearer {urlsplit(expired['url']).fragment}"}
        assert phone.post(path, content=image.getvalue(), headers=expired_auth).status_code == 410
        assert client.get(f"{sessions}/{expired['id']}").json()["state"] == "expired"


def test_mobile_qr_batch_saves_valid_photos_and_reports_failed_and_duplicate_files(client, monkeypatch):
    monkeypatch.setattr(mobile, "Session", purchases.Session)
    monkeypatch.setenv("RECEIPT_MOBILE_URL", "http://192.168.1.10:8010")
    sessions = "/api/v1/receipts/mobile-sessions"
    ticket = client.post(sessions).json()
    status_path = f"{sessions}/{ticket['id']}"
    headers = {"Authorization": f"Bearer {urlsplit(ticket['url']).fragment}"}
    photos = []
    for color in ("white", "black"):
        image = BytesIO()
        Image.new("RGB", (8, 8), color).save(image, format="PNG")
        photos.append(image.getvalue())

    with TestClient(mobile.mobile_app) as phone:
        failed_file = ("photos", ("broken.png", b"bad", "image/png"))
        rejected = phone.post("/upload-batch", headers=headers, files=[failed_file])
        assert rejected.json()["received"] is False
        assert client.get(status_path).json()["state"] == "waiting"

        files = [
            ("photos", ("first.png", photos[0], "image/png")),
            failed_file,
            ("photos", ("second.png", photos[1], "image/png")),
            ("photos", ("same.png", photos[0], "image/png")),
        ]
        result = phone.post("/upload-batch", headers=headers, files=files).json()
        assert result["received"] is True
        assert result["items"][1]["error"]
        assert result["items"][3]["duplicate"] is True
        assert all("record_id" not in item for item in result["items"])

        delivery = client.get(status_path).json()
        assert delivery["state"] == "received"
        assert delivery["items"][0]["record_id"] == delivery["items"][3]["record_id"]
        assert delivery["items"][0]["record_id"] != delivery["items"][2]["record_id"]
        assert client.get("/api/v1/receipts/review-count").json() == {"count": 2}
        assert phone.post("/upload-batch", headers=headers, files=files).status_code == 410


def test_statement_review_and_excel_preserve_item_rows(client, monkeypatch):
    monkeypatch.setattr(purchases, "extract", lambda *_: Extraction(STATEMENT_TEXT, "pdf_text"))
    with pymupdf.open() as pdf:
        pdf.new_page()
        content = pdf.tobytes()
    prefix = "/api/v1/statements"
    uploaded = client.post(f"{prefix}/uploads?filename=statement.pdf", content=content).json()["record"]
    assert uploaded["kind"] == "statement"
    assert uploaded["document_type"] == "trade_statement"
    assert len(uploaded["fields"]["items"]) == 2
    path = f"{prefix}/{uploaded['id']}"
    assert client.get(f"{path}/preview/1").headers["content-type"] == "image/png"

    draft = uploaded["fields"]
    draft["items"][1]["unit_price"] = None
    assert client.put(f"{path}/draft", json={"fields": draft}).status_code == 200
    assert client.get(path).json()["fields"]["items"][1]["unit_price"] is None
    body = {"document_type": "trade_statement", "fields": draft}
    assert client.post(f"{path}/approve", json=body).status_code == 422
    draft["items"][1]["unit_price"] = 4_100.5
    assert client.post(f"{path}/approve", json=body).status_code == 200
    history = client.get("/api/v1/documents/history?document_type=trade_statement&status=approved").json()
    assert len(history) == 1 and history[0]["id"] == uploaded["id"]
    workbook = load_workbook(BytesIO(client.get("/api/v1/documents/export?document_type=trade_statement").content))
    worksheet = workbook["거래명세서 품목"]
    assert worksheet["G3"].value == "절연 케이블"
    assert worksheet["H3"].value == 1.5
    assert worksheet["I3"].value == 4_100.5
    assert worksheet["I3"].data_type == "n"
