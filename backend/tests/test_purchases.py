from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from PIL import Image
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.features.purchases import router as purchases
from app.features.purchases import mobile
from app.features.purchases.extraction import Extraction, parse_fields
from app.main import app


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


def test_saved_draft_remains_pending_and_can_be_reopened(client):
    image = BytesIO()
    Image.new("RGB", (8, 8)).save(image, format="PNG")
    receipt = client.post("/api/v1/receipts/uploads?filename=test.png", content=image.getvalue()).json()["record"]
    path = f"/api/v1/receipts/{receipt['id']}"
    count_path = "/api/v1/documents/review-count"
    assert client.get(count_path).json() == {"count": 1}
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
    assert client.put(f"{path}/draft", json={"fields": fields}).status_code == 409
    assert client.get(path).json()["fields"] == complete
    trashed = client.delete(path).json()
    assert trashed["status"] == "trashed"
    assert client.get("/api/v1/documents/history?status=approved").json() == []
    workbook = load_workbook(BytesIO(client.get("/api/v1/documents/export?status=approved").content))
    assert workbook.active.max_row == 1
    restored = client.post(f"{path}/restore").json()
    assert restored["status"] == "approved"
    assert restored["approved_at"] == approved.json()["approved_at"]
    assert restored["fields"] == complete
    assert client.get(count_path).json() == {"count": 0}


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
