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
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from app.db import Base, Session, engine
from .extraction import (
    EXTRACTION_REVISION, STATEMENT_ITEM_LIMIT, extract, pdf_preview, receipt_vendor, recognize_upload, statement_delivery_date,
    statement_tax_amounts, statement_vendor, supplier_business_number, validate_file,
)
from .matching import EvidenceMatches, MATCH_TYPES, evidence_date, registry_number, vendor_name
from .model import PurchaseFileClaim, PurchaseLink, PurchaseRecord

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
    business_number: str = Field(pattern=r"^[0-9]{3}[- ]*[0-9]{2}[- ]*[0-9]{5}$", max_length=32)
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
    business_number: str = Field(default="", pattern=r"^(?:[0-9]{3}[- ]*[0-9]{2}[- ]*[0-9]{5})?$", max_length=32)
    transaction_date: str = Field(min_length=10, max_length=10)
    statement_number: str = Field(default="", max_length=64)
    total_amount: int | None = Field(default=None, strict=True, ge=-999_999_999_999, le=999_999_999_999)
    supply_amount: int | None = Field(default=None, strict=True, ge=-999_999_999_999, le=999_999_999_999)
    tax_amount: int | None = Field(default=None, strict=True, ge=-999_999_999_999, le=999_999_999_999)
    items: list[StatementItem] = Field(min_length=1, max_length=STATEMENT_ITEM_LIMIT)


FIELDS_BY_KIND = {"receipt": ReceiptFields, "document": InvoiceFields, "statement": StatementFields}
TYPE_BY_KIND = {"receipt": "receipt", "document": "tax_invoice", "statement": "trade_statement"}


class Approval(BaseModel):
    fields: dict
    document_type: DocumentType


class Draft(BaseModel):
    fields: dict[str, str | StrictInt | list[DraftStatementItem] | None]
    confirmed_type: bool = False


class LinkRequest(BaseModel):
    candidate_id: str = Field(pattern=r"^[0-9a-f]{32}$")


def init_db():
    Base.metadata.create_all(engine)


def korea_now():
    return datetime.now(timezone.utc).replace(tzinfo=None) + KOREA_OFFSET


def effective_fields(row):
    fields = dict(row.fields)
    text = row.extraction.get("text", "")
    needs_fallback = row.status == "review" and not row.extraction.get("draft_saved_at")
    if row.kind == "receipt" and needs_fallback and not fields.get("vendor"):
        fields["vendor"] = receipt_vendor(text)
    if row.document_type == "tax_invoice" and needs_fallback and not fields.get("business_number"):
        fields["business_number"] = supplier_business_number(unicodedata.normalize("NFKC", text))
    if row.document_type == "trade_statement":
        text = unicodedata.normalize("NFKC", text)
        if needs_fallback:
            fields["vendor"] = statement_vendor(text) or fields.get("vendor", "")
            fields["transaction_date"] = statement_delivery_date(text) or fields.get("transaction_date", "")
        if "business_number" not in fields:
            fields["business_number"] = supplier_business_number(text)
        for field, value in statement_tax_amounts(text).items():
            fields.setdefault(field, value)
    return fields


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
        result["fields"] = effective_fields(row)
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
        changed = db.execute(
            update(PurchaseRecord)
            .where(PurchaseRecord.id == record_id, PurchaseRecord.kind == kind,
                   PurchaseRecord.status.in_(allowed))
            .values(status=target)
        )
        if changed.rowcount and not restore:
            db.execute(delete(PurchaseLink).where(
                (PurchaseLink.invoice_id == record_id) | (PurchaseLink.statement_id == record_id)
            ))
        db.commit()
        row = get_record(db, kind, record_id)
        valid_statuses = ("review", "approved") if restore else ("trashed",)
        if row.status not in valid_statuses:
            raise HTTPException(409, "문서 상태가 변경되었습니다. 처리 이력을 새로 조회해 주세요.")
        return serialize(row, full=True)


def invoice_approval_key(value):
    number = registry_number(value)
    return sha256(number.encode("ascii")).hexdigest() if re.fullmatch(r"[0-9]{24}", number) else None


def existing_invoice(db, key, exclude_id=None):
    invoices = record_query("document")
    if exclude_id:
        invoices = invoices.where(PurchaseRecord.id != exclude_id)
    existing = db.scalar(invoices.where(PurchaseRecord.approval_key == key))
    if existing is not None:
        return existing
    # Older uploads only populated the unique approval key on approval.
    for row in db.scalars(invoices.where(PurchaseRecord.approval_key.is_(None)).options(defer(PurchaseRecord.extraction))):
        if invoice_approval_key(row.fields.get("approval_number")) == key:
            return row
    return None


def suspected_duplicate(db, kind, fields):
    vendor = vendor_name(fields.get("vendor"))
    day = evidence_date(fields.get("transaction_date"))
    total = fields.get("total_amount")
    if not vendor or day is None or type(total) is not int:
        return None
    rows = db.execute(select(PurchaseRecord.id, PurchaseRecord.fields).where(
        PurchaseRecord.kind == kind, PurchaseRecord.status.in_(("review", "approved")),
    ).order_by(PurchaseRecord.created_at.desc(), PurchaseRecord.id))
    for record_id, existing in rows:
        if (vendor_name(existing.get("vendor")) == vendor and evidence_date(existing.get("transaction_date")) == day
                and existing.get("total_amount") == total):
            return record_id
    return None


def duplicate_upload(record, reason):
    return {"record": serialize(record, full=True), "duplicate": True, "duplicate_reason": reason}


def can_refresh_extraction(record):
    return (record.status == "review" and not record.extraction.get("draft_saved_at")
            and record.extraction.get("revision", 0) < EXTRACTION_REVISION)


def refresh_duplicate(record_id, kind, document_type, fields, extraction, key):
    with Session() as db:
        record = get_record(db, None, record_id)
        if not can_refresh_extraction(record) or not extraction["text"]:
            return duplicate_upload(record, "file")
        if record.document_type not in ("unknown", document_type):
            return duplicate_upload(record, "file")
        if key and (existing := existing_invoice(db, key, record_id)) is not None:
            return duplicate_upload(existing, "approval_number")
        if record.kind == kind:
            fields = {**record.fields, **{field: value for field, value in fields.items()
                                         if value not in (None, "", []) or field not in record.fields}}
        revision = PurchaseRecord.extraction["revision"].as_integer()
        try:
            db.execute(update(PurchaseRecord).where(
                PurchaseRecord.id == record_id,
                PurchaseRecord.status == "review",
                PurchaseRecord.extraction["draft_saved_at"].as_string().is_(None),
                or_(revision.is_(None), revision < EXTRACTION_REVISION),
            ).values(kind=kind, document_type=document_type, fields=fields,
                     extraction=extraction, vendor=fields.get("vendor", "")[:200],
                     approval_key=key or record.approval_key))
            db.commit()
        except IntegrityError:
            db.rollback()
            if key and (existing := existing_invoice(db, key, record_id)) is not None:
                return duplicate_upload(existing, "approval_number")
            raise
        db.expire_all()
        return duplicate_upload(get_record(db, None, record_id), "file")


def store_upload(kind, filename, content, confirm_suspected=False):
    try:
        media_type, page_count = validate_file(kind, filename, content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    digest = sha256(content).hexdigest()
    lookup = record_query().where(PurchaseRecord.file_hash == digest).order_by(
        (PurchaseRecord.kind == kind).desc(), (PurchaseRecord.status == "trashed"), PurchaseRecord.created_at,
    ).limit(1)
    with Session() as db:
        existing = db.scalar(lookup)
        if existing is not None and not can_refresh_extraction(existing):
            return duplicate_upload(existing, "file")
        existing_id = existing.id if existing else None
    extraction = extract(kind, content)
    kind, document_type, fields = recognize_upload(kind, extraction)
    if document_type == "unknown" and extraction.text and not extraction.warning:
        extraction.warning = "이 PDF에서 세금계산서 또는 거래명세서 양식을 확인하지 못했습니다. 원본을 대조해 주세요."
    if kind == "receipt" and extraction.text and not extraction.warning and not any(
        value not in (None, "") for value in fields.values()
    ):
        extraction.warning = "사진에서 거래처나 거래 정보를 찾지 못했습니다. 영수증 사진인지 확인하거나 직접 입력해 주세요."
    key = invoice_approval_key(fields.get("approval_number")) if kind == "document" else None
    extraction_data = {"text": extraction.text, "method": extraction.method, "warning": extraction.warning,
                       "page_count": page_count, "revision": EXTRACTION_REVISION if extraction.text else 0}
    if existing_id:
        return refresh_duplicate(existing_id, kind, document_type, fields, extraction_data, key)
    row = PurchaseRecord(
        id=uuid4().hex, filename=filename, kind=kind, file_hash=digest, media_type=media_type,
        original=content, document_type=document_type, fields=fields, extraction=extraction_data,
        vendor=fields.get("vendor", "")[:200], created_at=korea_now(), approval_key=key,
    )
    with Session() as db:
        if key:
            existing = existing_invoice(db, key)
            if existing is not None:
                return duplicate_upload(existing, "approval_number")
        if not confirm_suspected:
            suspect_id = suspected_duplicate(db, kind, fields)
            if suspect_id:
                existing = db.scalar(lookup)
                if existing is not None:
                    return duplicate_upload(existing, "file")
                if key and (existing := existing_invoice(db, key)) is not None:
                    return duplicate_upload(existing, "approval_number")
                raise HTTPException(409, {"code": "suspected_duplicate", "id": suspect_id,
                                         "message": "거래처·날짜·금액이 같은 증빙이 있습니다. 별도 거래인지 확인해 주세요."})
        db.add(row)
        db.add(PurchaseFileClaim(file_hash=digest, record_id=row.id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = db.scalar(lookup)
            if existing is not None:
                return duplicate_upload(existing, "file")
            if key:
                existing = existing_invoice(db, key)
                if existing is not None:
                    return duplicate_upload(existing, "approval_number")
            raise
        return {"record": serialize(row, full=True), "duplicate": False}


def validated_fields(kind, body):
    if body.document_type != TYPE_BY_KIND[kind]:
        raise HTTPException(422, "원본을 보고 문서 유형을 확인해 주세요.")
    date_label = {"statement": "납품일자", "document": "작성일자", "receipt": "거래일시"}[kind]
    try:
        values = FIELDS_BY_KIND[kind].model_validate(body.fields).model_dump()
    except ValidationError as exc:
        extra_labels = {"items": "품목별 품목명·수량·단가", "statement_number": "명세서 번호",
                        "transaction_date": date_label}
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
        raise HTTPException(422, f"{date_label} 형식을 확인해 주세요.") from exc

    if kind == "document":
        if values["supply_amount"] + values["tax_amount"] != values["total_amount"]:
            raise HTTPException(422, "공급가액과 세액의 합이 합계금액과 일치해야 합니다.")
        values["business_number"] = registry_number(values["business_number"])
        return values, invoice_approval_key(values["approval_number"])
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
                details.append(["기록 ID", "원본 파일", "명세서 번호", "거래처", "납품일자",
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
    async def upload(request: Request, filename: str, confirm_suspected: bool = False):
        filename, content = await read_upload(request, filename)
        return await run_in_threadpool(store_upload, kind, filename, content, confirm_suspected)

    @router.get("/review-count")
    def review_count(include_completed: bool = False):
        with Session() as db:
            if include_completed:
                today = datetime.combine(korea_now().date(), time.min)
                counts = dict(db.execute(
                    select(PurchaseRecord.status, func.count()).where(or_(
                        PurchaseRecord.status == "review",
                        (PurchaseRecord.status == "approved")
                        & (PurchaseRecord.approved_at >= today)
                        & (PurchaseRecord.approved_at < today + timedelta(days=1)),
                    )).group_by(PurchaseRecord.status)
                ).all())
                return {"count": counts.get("review", 0), "completed_today": counts.get("approved", 0)}
            return {"count": db.scalar(
                select(func.count()).select_from(PurchaseRecord).where(PurchaseRecord.status == "review")
            )}

    if kind == "document":
        @router.get("/trash/count")
        def trash_count():
            with Session() as db:
                return {"count": db.scalar(
                    select(func.count()).select_from(PurchaseRecord).where(PurchaseRecord.status == "trashed")
                )}

    @router.get("/history")
    def history(start: date | None = None, end: date | None = None, vendor: str = "",
                document_type: DocumentType | None = None, status: Status | None = None):
        with Session() as db:
            rows = list(db.scalars(history_query(start, end, vendor, document_type, status)))
            matches = EvidenceMatches(db, effective_fields) if any(row.kind in MATCH_TYPES for row in rows) else None
            return [
                {**serialize(row), "match_status": (
                    "check" if row.document_type == "unknown" else matches.describe(row.id)["status"]
                )} if row.kind in MATCH_TYPES and row.status != "trashed" else serialize(row)
                for row in rows
            ]

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

    if kind == "document":
        @router.delete("/trash")
        def empty_trash():
            with Session.begin() as db:
                result = db.execute(delete(PurchaseRecord).where(PurchaseRecord.status == "trashed"))
                db.execute(delete(PurchaseFileClaim).where(~select(PurchaseRecord.id).where(
                    PurchaseRecord.id == PurchaseFileClaim.record_id,
                ).exists()))
                return {"deleted": result.rowcount}

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

    @router.delete("/{record_id}/permanent", status_code=204)
    def permanently_delete(record_id: str):
        with Session.begin() as db:
            result = db.execute(delete(PurchaseRecord).where(
                PurchaseRecord.id == record_id,
                PurchaseRecord.kind == kind,
                PurchaseRecord.status == "trashed",
            ))
            if not result.rowcount:
                get_record(db, kind, record_id)
                raise HTTPException(409, "휴지통에 있는 기록만 영구 삭제할 수 있습니다.")
            db.execute(delete(PurchaseFileClaim).where(PurchaseFileClaim.record_id == record_id))
        return Response(status_code=204)

    if kind in MATCH_TYPES:
        @router.get("/{record_id}/matching")
        def match_detail(record_id: str):
            with Session() as db:
                row = get_record(db, kind, record_id)
                if row.document_type == "unknown" and row.status != "trashed":
                    return {"status": "check", "linked": None, "candidates": []}
                return EvidenceMatches(db, effective_fields).describe(record_id, full=True)

        @router.post("/{record_id}/matching")
        def confirm_link(record_id: str, body: LinkRequest):
            other_kind = "statement" if kind == "document" else "document"
            try:
                with Session.begin() as db:
                    # Acquire both row locks in the same order as concurrent
                    # link requests before reading the current document state.
                    for target in sorted((record_id, body.candidate_id)):
                        db.execute(update(PurchaseRecord).where(PurchaseRecord.id == target)
                                   .values(status=PurchaseRecord.status))
                    get_record(db, kind, record_id)
                    get_record(db, other_kind, body.candidate_id)
                    matches = EvidenceMatches(db, effective_fields)
                    current = matches.rows.get(record_id)
                    if matches.links.get(record_id) == body.candidate_id:
                        return matches.describe(record_id, full=True)
                    if record_id in matches.links or body.candidate_id in matches.links:
                        raise HTTPException(409, "이미 연결된 증빙입니다. 기존 연결을 해제한 뒤 다시 시도해 주세요.")
                    if not current or not any(
                        candidate["record"]["id"] == body.candidate_id for candidate in matches.candidates(current)
                    ):
                        raise HTTPException(409, "연결 후보가 변경되었습니다. 증빙 대조 결과를 다시 확인해 주세요.")
                    invoice_id, statement_id = (record_id, body.candidate_id) if kind == "document" else (
                        body.candidate_id, record_id
                    )
                    db.add(PurchaseLink(invoice_id=invoice_id, statement_id=statement_id))
                    db.flush()
                    return EvidenceMatches(db, effective_fields).describe(record_id, full=True)
            except IntegrityError as exc:
                raise HTTPException(409, "이미 연결된 증빙입니다.") from exc

        @router.delete("/{record_id}/matching", status_code=204)
        def unlink(record_id: str):
            with Session.begin() as db:
                get_record(db, kind, record_id)
                column = PurchaseLink.invoice_id if kind == "document" else PurchaseLink.statement_id
                db.execute(delete(PurchaseLink).where(column == record_id))
            return Response(status_code=204)

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
        optional_statement = {"business_number", "supply_amount", "tax_amount"} if kind == "statement" else set()
        if not (expected - optional_statement <= body.fields.keys() <= expected):
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
            if kind == "document" and key and (existing := existing_invoice(db, key, record_id)) is not None:
                raise HTTPException(409, {"message": "같은 승인번호가 등록된 기록이 있습니다.", "id": existing.id})
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
