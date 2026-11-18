from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import re
from typing import Literal
import unicodedata
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from openpyxl import Workbook
from openpyxl.styles import Font
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from app.db import Base, Session, engine
from .extraction import (
    STATEMENT_ITEM_LIMIT, extract, parse_fields, pdf_preview, receipt_vendor, supplier_business_number, validate_file,
)
from .model import PurchaseRecord

MAX_BYTES = 10 * 1024 * 1024
KOREA_OFFSET = timedelta(hours=9)

FIELD_LABELS = {
    "vendor": "거래처",
    "business_number": "공급자 사업자등록번호",
    "transaction_date": "거래일시 / 작성일자",
    "approval_number": "승인번호",
    "item_summary": "품목 요약",
    "supply_amount": "공급가액",
    "tax_amount": "세액",
    "total_amount": "합계금액",
}
TYPE_LABELS = {
    "receipt": "영수증", "tax_invoice": "매입 세금계산서",
    "trade_statement": "거래명세서", "unknown": "유형 확인 필요",
}
STATUS_LABELS = {"review": "검토 대기", "approved": "승인 완료", "trashed": "휴지통"}
ActiveStatus = Literal["review", "approved"]
Status = Literal["review", "approved", "trashed"]
DocumentType = Literal["receipt", "tax_invoice", "trade_statement", "unknown"]


class ReceiptFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    vendor: str = Field(min_length=1, max_length=200)
    transaction_date: str = Field(min_length=10, max_length=19)
    approval_number: str = Field(pattern=r"^[0-9]{6,12}$")
    total_amount: int = Field(strict=True, ge=1, le=999_999_999_999)


class InvoiceFields(ReceiptFields):
    business_number: str = Field(pattern=r"^[0-9]{3}-?[0-9]{2}-?[0-9]{5}$")
    approval_number: str = Field(pattern=r"^[0-9]{8}[- ]?[0-9]{8}[- ]?[0-9]{8}$")
    item_summary: str = Field(default="", max_length=500)
    supply_amount: int = Field(strict=True, ge=-999_999_999_999, le=999_999_999_999)
    tax_amount: int = Field(strict=True, ge=-999_999_999_999, le=999_999_999_999)
    total_amount: int = Field(strict=True, ge=-999_999_999_999, le=999_999_999_999)


class DraftStatementItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(max_length=200)
    quantity: float | None = Field(strict=True, ge=-999_999_999_999, le=999_999_999_999)
    unit_price: float | None = Field(strict=True, ge=-999_999_999_999, le=999_999_999_999)


class StatementItem(DraftStatementItem):
    name: str = Field(min_length=1, max_length=200)
    quantity: float = Field(strict=True, ge=-999_999_999_999, le=999_999_999_999)
    unit_price: float = Field(strict=True, ge=-999_999_999_999, le=999_999_999_999)


class StatementFields(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    vendor: str = Field(min_length=1, max_length=200)
    transaction_date: str = Field(min_length=10, max_length=10)
    statement_number: str = Field(default="", max_length=64)
    total_amount: int | None = Field(default=None, strict=True, ge=-999_999_999_999, le=999_999_999_999)
    items: list[StatementItem] = Field(min_length=1, max_length=STATEMENT_ITEM_LIMIT)


FIELDS_BY_KIND = {"receipt": ReceiptFields, "document": InvoiceFields, "statement": StatementFields}
TYPE_BY_KIND = {"receipt": "receipt", "document": "tax_invoice", "statement": "trade_statement"}


class Approval(BaseModel):
    fields: dict
    document_type: DocumentType


class Draft(BaseModel):
    fields: dict[str, str | StrictInt | list[DraftStatementItem] | None]
    confirmed_type: bool = False


def init_db():
    Base.metadata.create_all(engine)


def korea_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + KOREA_OFFSET


def serialize(row, full=False):
    result = {
        "id": row.id,
        "filename": row.filename,
        "media_type": row.media_type,
        "kind": row.kind,
        "document_type": row.document_type,
        "status": row.status,
        "fields": row.fields,
        "created_at": row.created_at.isoformat() + "+09:00",
        "approved_at": row.approved_at.isoformat() + "+09:00" if row.approved_at else None,
    }
    if full:
        result["extraction"] = row.extraction
        result["page_count"] = row.extraction.get("page_count", 1)
        result["draft_saved_at"] = row.extraction.get("draft_saved_at")
        result["confirmed_type"] = row.document_type != "unknown" or row.extraction.get("confirmed_type", False)
        needs_fallback = row.status == "review" and not result["draft_saved_at"]
        if needs_fallback and row.kind == "receipt" and not row.fields.get("vendor"):
            vendor = receipt_vendor(row.extraction["text"])
            if vendor:
                result["fields"] = {**row.fields, "vendor": vendor}
        if needs_fallback and row.document_type == "tax_invoice" and not row.fields.get("business_number"):
            business_number = supplier_business_number(unicodedata.normalize("NFKC", row.extraction["text"]))
            if business_number:
                result["fields"] = {**row.fields, "business_number": business_number}
    return result


def record_query(kind=None):
    query = select(PurchaseRecord).options(defer(PurchaseRecord.original))
    return query.where(PurchaseRecord.kind == kind) if kind else query


def get_record(db, kind, record_id):
    row = db.scalar(record_query(kind).where(PurchaseRecord.id == record_id))
    if row is None:
        raise HTTPException(404, "처리 기록을 찾을 수 없습니다.")
    return row


def change_trash_status(kind, record_id, *, restore=False):
    allowed = ("trashed",) if restore else ("review", "approved")
    target = case((PurchaseRecord.approved_at.is_not(None), "approved"), else_="review") if restore else "trashed"
    with Session() as db:
        db.execute(
            update(PurchaseRecord)
            .where(PurchaseRecord.id == record_id, PurchaseRecord.kind == kind,
                   PurchaseRecord.status.in_(allowed))
            .values(status=target)
        )
        db.commit()
        row = get_record(db, kind, record_id)
        valid_statuses = ("review", "approved") if restore else ("trashed",)
        if row.status not in valid_statuses:
            raise HTTPException(409, "문서 상태가 변경되었습니다. 처리 이력을 새로 조회해 주세요.")
        return serialize(row, full=True)


def store_upload(kind, filename, content):
    try:
        media_type, page_count = validate_file(kind, filename, content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    digest = sha256(content).hexdigest()
    lookup = record_query(kind).where(PurchaseRecord.file_hash == digest)
    with Session() as db:
        existing = db.scalar(lookup)
        if existing is not None:
            return {"record": serialize(existing, full=True), "duplicate": True}
    extraction = extract(kind, content)
    document_type, fields = parse_fields(kind, extraction.text, extraction.supplier_text)
    if kind == "receipt" and extraction.text and not extraction.warning and not any(
        value not in (None, "") for value in fields.values()
    ):
        extraction.warning = "사진에서 거래처나 거래 정보를 찾지 못했습니다. 영수증 사진인지 확인하거나 직접 입력해 주세요."
    row = PurchaseRecord(
        id=uuid4().hex, filename=filename, kind=kind, file_hash=digest, media_type=media_type,
        original=content, document_type=document_type, fields=fields, extraction={"text": extraction.text, "method": extraction.method,
                    "warning": extraction.warning, "page_count": page_count},
        vendor=fields.get("vendor", "")[:200], created_at=korea_now(),
    )
    with Session() as db:
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(lookup)
            if existing is None:
                raise
            return {"record": serialize(existing, full=True), "duplicate": True}
        return {"record": serialize(row, full=True), "duplicate": False}


def validated_fields(kind, body):
    if body.document_type != TYPE_BY_KIND[kind]:
        raise HTTPException(422, "원본을 보고 문서 유형을 확인해 주세요.")
    try:
        values = FIELDS_BY_KIND[kind].model_validate(body.fields).model_dump()
    except ValidationError as exc:
        extra_labels = {"items": "품목별 품목명·수량·단가", "statement_number": "명세서 번호"}
        labels = dict.fromkeys(
            extra_labels.get(error["loc"][0], FIELD_LABELS.get(error["loc"][0], "입력값"))
            for error in exc.errors()
        )
        raise HTTPException(422, f"필수 항목 또는 입력 형식을 확인해 주세요. ({', '.join(labels)})") from exc
    raw_date = values["transaction_date"]
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
            values["transaction_date"] = date.fromisoformat(raw_date).isoformat()
        elif kind == "receipt" and re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?", raw_date):
            values["transaction_date"] = datetime.fromisoformat(raw_date).isoformat()
        else:
            raise ValueError()
    except ValueError as exc:
        raise HTTPException(422, "거래일시 또는 작성일자 형식을 확인해 주세요.") from exc

    if kind == "document":
        if values["supply_amount"] + values["tax_amount"] != values["total_amount"]:
            raise HTTPException(422, "공급가액과 세액의 합이 합계금액과 일치해야 합니다.")
        values["business_number"] = values["business_number"].replace("-", "")
        key = re.sub(r"[- ]", "", values["approval_number"])
    else:
        number = values["approval_number"] if kind == "receipt" else values["statement_number"]
        if not number:
            return values, None
        vendor = re.sub(r"\s+", "", unicodedata.normalize("NFKC", values["vendor"])).casefold()
        number = re.sub(r"\s+", "", unicodedata.normalize("NFKC", number)).casefold()
        key = f"{vendor}|{raw_date[:10]}|{number}"
    return values, sha256(key.encode("utf-8")).hexdigest()


def history_query(start, end, vendor, document_type, status):
    query = record_query().options(defer(PurchaseRecord.extraction))
    if start and end and start > end:
        raise HTTPException(422, "시작일은 종료일 이전이어야 합니다.")
    if start:
        query = query.where(PurchaseRecord.created_at >= datetime.combine(start, time.min))
    if end:
        query = query.where(PurchaseRecord.created_at <= datetime.combine(end, time.max))
    if vendor:
        query = query.where(PurchaseRecord.vendor.icontains(vendor.strip(), autoescape=True))
    if document_type:
        query = query.where(PurchaseRecord.document_type == document_type)
    if status:
        query = query.where(PurchaseRecord.status == status)
    else:
        query = query.where(PurchaseRecord.status.in_(("review", "approved")))
    return query.order_by(PurchaseRecord.created_at.desc(), PurchaseRecord.id)


def spreadsheet(rows):
    book = Workbook()
    sheet = book.active
    sheet.title = "구매 증빙 처리 이력"
    sheet.append(["문서 유형", "처리 상태", "원본 파일", *FIELD_LABELS.values(),
                  "등록일시 (KST)", "승인일시 (KST)", "명세서 번호"])
    details = None
    for row in rows:
        values = [
            TYPE_LABELS[row.document_type], STATUS_LABELS[row.status], row.filename,
            *(row.fields.get(field) for field in FIELD_LABELS), row.created_at, row.approved_at,
            row.fields.get("statement_number"),
        ]
        append_spreadsheet_row(sheet, values)
        if row.kind == "statement" and row.fields.get("items"):
            if details is None:
                details = book.create_sheet("거래명세서 품목")
                details.append(["기록 ID", "원본 파일", "명세서 번호", "거래처", "작성일자",
                                "품목 순서", "품목명", "수량", "단가"])
            for index, item in enumerate(row.fields["items"], start=1):
                append_spreadsheet_row(details, [
                    row.id, row.filename, row.fields.get("statement_number"), row.fields.get("vendor"),
                    row.fields.get("transaction_date"), index, item["name"], item["quantity"], item["unit_price"],
                ])
    for worksheet in book:
        worksheet.freeze_panes = "D2"
        worksheet.auto_filter.ref = worksheet.dimensions
        for column in worksheet.columns:
            column[0].font = Font(bold=True)
            worksheet.column_dimensions[column[0].column_letter].width = 23
    output = BytesIO()
    book.save(output)
    return output.getvalue()


def append_spreadsheet_row(sheet, values):
    sheet.append(values)
    for cell in sheet[sheet.max_row]:
        if isinstance(cell.value, str):
            cell.data_type = "s"


def clean_upload_filename(filename: str):
    filename = re.split(r"[/\\]", filename)[-1].strip()
    if not filename or len(filename) > 255 or any(ord(char) < 32 for char in filename):
        raise HTTPException(422, "파일 이름을 확인해 주세요.")
    return filename


async def read_upload(request: Request, filename: str):
    filename = clean_upload_filename(filename)
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > MAX_BYTES:
            raise HTTPException(413, "파일 크기는 최대 10 MB입니다.")
        content.extend(chunk)
    if not content:
        raise HTTPException(422, "빈 파일은 업로드할 수 없습니다.")
    return filename, bytes(content)


def attach_routes(router: APIRouter, kind: str):
    @router.post("/uploads")
    async def upload(request: Request, filename: str):
        filename, content = await read_upload(request, filename)
        return await run_in_threadpool(store_upload, kind, filename, content)

    @router.get("/review-count")
    def review_count():
        with Session() as db:
            return {"count": db.scalar(
                select(func.count()).select_from(PurchaseRecord).where(PurchaseRecord.status == "review")
            )}

    @router.get("/history")
    def history(start: date | None = None, end: date | None = None, vendor: str = "",
                document_type: DocumentType | None = None, status: Status | None = None):
        with Session() as db:
            return [serialize(row) for row in db.scalars(history_query(start, end, vendor, document_type, status))]

    @router.get("/export")
    def export(start: date | None = None, end: date | None = None, vendor: str = "",
               document_type: DocumentType | None = None, status: ActiveStatus | None = None):
        with Session() as db:
            rows = db.scalars(history_query(start, end, vendor, document_type, status))
            return Response(
                spreadsheet(rows),
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": 'attachment; filename="purchase-history.xlsx"', "Cache-Control": "no-store"},
            )

    @router.get("/{record_id}")
    def detail(record_id: str):
        with Session() as db:
            return serialize(get_record(db, kind, record_id), full=True)

    @router.delete("/{record_id}")
    def trash(record_id: str):
        return change_trash_status(kind, record_id)

    @router.post("/{record_id}/restore")
    def restore(record_id: str):
        return change_trash_status(kind, record_id, restore=True)

    @router.get("/{record_id}/original")
    def original(record_id: str):
        with Session() as db:
            row = get_record(db, kind, record_id)
            return Response(row.original, media_type=row.media_type, headers={
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(row.filename, safe='')}",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            })

    @router.get("/{record_id}/preview/{page_number}")
    def preview(record_id: str, page_number: int):
        with Session() as db:
            row = get_record(db, kind, record_id)
            if row.media_type != "application/pdf":
                raise HTTPException(404, "PDF 원본이 없습니다.")
            content = row.original
        try:
            return Response(pdf_preview(content, page_number), media_type="image/png", headers={"Cache-Control": "no-store"})
        except IndexError as exc:
            raise HTTPException(404, "페이지를 찾을 수 없습니다.") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(422, "미리보기를 만들 수 없습니다. 원본을 열어 확인해 주세요.") from exc

    @router.put("/{record_id}/draft")
    def save_draft(record_id: str, body: Draft):
        expected = FIELDS_BY_KIND[kind].model_fields.keys()
        if body.fields.keys() != expected:
            raise HTTPException(422, "문서 유형의 임시저장 항목을 확인해 주세요.")
        fields = {}
        for name, value in body.fields.items():
            if name == "items":
                if not isinstance(value, list) or len(value) > STATEMENT_ITEM_LIMIT:
                    raise HTTPException(422, f"품목은 최대 {STATEMENT_ITEM_LIMIT}개까지 저장할 수 있습니다.")
                value = [item.model_dump() for item in value]
            elif name.endswith("_amount"):
                if value is not None and (type(value) is not int or abs(value) > 999_999_999_999):
                    raise HTTPException(422, "금액은 원 단위 정수로 입력해 주세요.")
            elif not isinstance(value, str) or len(value) > ({"vendor": 200, "item_summary": 500}.get(name, 64)):
                raise HTTPException(422, "임시저장할 항목의 글자 수를 확인해 주세요.")
            fields[name] = value

        with Session() as db:
            row = get_record(db, kind, record_id)
            if row.status != "review":
                raise HTTPException(409, "검토 대기 문서만 임시저장할 수 있습니다.")
            extraction = {
                **row.extraction,
                "draft_saved_at": korea_now().isoformat() + "+09:00",
                "confirmed_type": body.confirmed_type,
            }
            saved = db.execute(
                update(PurchaseRecord)
                .where(PurchaseRecord.id == record_id, PurchaseRecord.status == "review")
                .values(fields=fields, vendor=(fields.get("vendor") or "").strip()[:200], extraction=extraction)
            )
            if saved.rowcount != 1:
                raise HTTPException(409, "검토 대기 문서만 임시저장할 수 있습니다.")
            db.commit()
            db.expire_all()
            return serialize(get_record(db, kind, record_id), full=True)

    @router.post("/{record_id}/approve")
    def approve(record_id: str, body: Approval):
        with Session() as db:
            row = get_record(db, kind, record_id)
            if row.status == "approved":
                return serialize(row, full=True)
            if row.status != "review":
                raise HTTPException(409, "휴지통에서 복원한 뒤 승인해 주세요.")
            fields, key = validated_fields(kind, body)
            try:
                db.execute(
                    update(PurchaseRecord)
                    .where(PurchaseRecord.id == record_id, PurchaseRecord.status == "review")
                    .values(fields=fields, approval_key=key, vendor=fields["vendor"], status="approved",
                            document_type=body.document_type, approved_at=korea_now())
                )
                db.commit()
            except IntegrityError:
                db.rollback()
                if key is None:
                    raise
                existing = db.scalar(record_query(kind).where(PurchaseRecord.approval_key == key))
                if existing is None:
                    raise
                label = "명세서 번호" if kind == "statement" else "승인번호"
                raise HTTPException(409, {"message": f"같은 {label}가 등록된 기록이 있습니다.", "id": existing.id})
            db.expire_all()
            row = get_record(db, kind, record_id)
            if row.status != "approved":
                raise HTTPException(409, "문서 상태가 변경되었습니다. 처리 이력을 새로 조회해 주세요.")
            return serialize(row, full=True)


if __name__ == "__main__":
    init_db()
