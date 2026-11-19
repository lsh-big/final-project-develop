from calendar import monthrange
from dataclasses import dataclass
from datetime import date
import os
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, literal, select

from app.db import DATA_DIR
from . import router as purchases
from .matching import EvidenceMatches, amount, evidence_date, vendor_name
from .model import PurchaseInboxMemo, PurchaseLink, PurchaseRecord


router = APIRouter(prefix="/purchase-inbox", tags=["purchase inbox"])
InboxStatus = Literal["pending_invoice", "candidate", "difference", "complete", "check"]
ACTIVE = ("review", "approved")
COMPARABLE = ("document", "statement")


@dataclass(frozen=True)
class InboxAccess:
    organization_id: str
    author_id: str
    author_name: str
    readable_ids: frozenset[str]
    writable_ids: frozenset[str]
    can_read_memos: bool = True


def verified_evidence_access(request: Request) -> InboxAccess | None:
    # Only authenticated server middleware can establish this scope.
    access = getattr(request.state, "purchase_inbox_access", None)
    if isinstance(access, InboxAccess) and all((access.organization_id, access.author_id, access.author_name)):
        return access
    return None


def local_evidence_access(request: Request, setting: str, kinds, statuses) -> InboxAccess | None:
    if (getattr(request.state, "purchase_inbox_access", None) is not None
            or os.getenv(setting) != "1" or os.getenv("DATABASE_URL")):
        return None
    loopback = {"127.0.0.1", "::1"}
    if (request.client is None or request.client.host not in loopback
            or request.url.hostname not in loopback | {"localhost"}
            or request.headers.get("sec-fetch-site") == "cross-site"):
        return None
    url = purchases.engine.url
    if (url.get_backend_name() != "sqlite" or not url.database
            or Path(url.database).resolve() != (DATA_DIR / "purchase-records.sqlite3").resolve()):
        return None
    with purchases.Session() as db:
        ids = db.scalars(select(PurchaseRecord.id).where(
            PurchaseRecord.kind.in_(kinds), PurchaseRecord.status.in_(statuses),
        ))
        return InboxAccess("local-workspace", "local-read-only", "로컬 조회", frozenset(ids), frozenset(), False)


def require_inbox_access(request: Request) -> InboxAccess:
    access = verified_evidence_access(request) or local_evidence_access(
        request, "AUTOFLOW_LOCAL_PURCHASE_INBOX", COMPARABLE, ACTIVE,
    )
    if access is None:
        raise HTTPException(403, "미처리 증빙함을 열려면 서버에서 사용자와 증빙 접근 권한을 확인할 수 있어야 합니다.")
    return access


Access = Annotated[InboxAccess, Depends(require_inbox_access)]


def require_memo_access(access: Access) -> InboxAccess:
    if not access.can_read_memos:
        raise HTTPException(403, "확인 메모를 사용하려면 서버에서 사용자를 확인할 수 있어야 합니다.")
    return access


MemoAccess = Annotated[InboxAccess, Depends(require_memo_access)]


class MemoInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    text: str = Field(min_length=1, max_length=1000)


def accessible_records(access):
    return PurchaseRecord.id.in_(access.readable_ids), PurchaseRecord.status.in_(ACTIVE), PurchaseRecord.kind.in_(COMPARABLE)


def inbox_row(row, fields, match, can_write):
    linked, candidates = match["linked"], match["candidates"]
    comparable = linked or (candidates[0] if len(candidates) == 1 else None)
    comparison = comparable["comparison"] if comparable else None
    invoice = amount(fields, "total_amount") if row.kind == "document" else None
    statement = amount(fields, "total_amount") if row.kind == "statement" else None
    difference = None
    status = "candidate" if candidates else "check"
    if comparison:
        total = comparison["amounts"]["total_amount"]
        invoice, statement, difference = total["invoice"], total["statement"], total["difference"]
        if any(item["difference"] not in (None, 0) for item in comparison["amounts"].values()):
            status = "difference"
        elif linked and comparison["amount_review"] == "matched":
            status = "complete"
    return {"id": row.id, "kind": row.kind, "record_status": row.status, "vendor": fields.get("vendor") or "",
            "evidence_date": evidence_date(fields.get("transaction_date")), "registered_date": row.created_at.date(),
            "invoice_amount": invoice, "statement_amount": statement, "difference": difference,
            "status": status, "can_write_memo": can_write}


@router.get("")
def list_inbox(access: Access, start: date | None = None, end: date | None = None,
               vendor: Annotated[str, Field(max_length=200)] = "", status: InboxStatus | None = None):
    today = purchases.korea_now().date()
    start = start or today.replace(day=1)
    end = end or today.replace(day=monthrange(today.year, today.month)[1])
    if start > end:
        raise HTTPException(422, "시작일은 종료일 이전이어야 합니다.")
    vendor_filter = vendor_name(vendor)
    result = []
    with purchases.Session() as db:
        matches = EvidenceMatches(db, purchases.effective_fields, record_ids=access.readable_ids)
        records = db.scalars(purchases.record_query().where(*accessible_records(access)))
        for row in records:
            fields = matches.rows[row.id]["fields"] if row.id in matches.rows else row.fields
            day = evidence_date(fields.get("transaction_date")) or row.created_at.date()
            if not start <= day <= end or vendor_filter not in vendor_name(fields.get("vendor")):
                continue
            match = matches.describe(row.id, full=True)
            if row.kind == "statement" and match["linked"]:
                continue
            item = inbox_row(row, fields, match, row.id in access.writable_ids)
            matches_status = item["status"] == status if status else item["status"] != "complete"
            if matches_status:
                result.append(item)
    result.sort(key=lambda item: (item["evidence_date"] or item["registered_date"], item["id"]), reverse=True)
    return {"start": start, "end": end, "rows": result, "can_read_memos": access.can_read_memos}


def memo_data(memo):
    return {"id": memo.id, "record_id": memo.record_id, "author": memo.author_name, "text": memo.text,
            "created_at": memo.created_at.isoformat() + "+09:00"}


@router.get("/{record_id}/memos")
def list_memos(record_id: str, access: MemoAccess):
    with purchases.Session() as db:
        visible = select(PurchaseRecord.id).where(*accessible_records(access))
        if db.scalar(visible.where(PurchaseRecord.id == record_id)) is None:
            raise HTTPException(404, "접근할 수 있는 증빙을 찾을 수 없습니다.")
        ids = {record_id}
        for invoice, statement in db.execute(select(PurchaseLink.invoice_id, PurchaseLink.statement_id).where(
            (PurchaseLink.invoice_id == record_id) | (PurchaseLink.statement_id == record_id),
        )):
            ids.add(statement if invoice == record_id else invoice)
        notes = db.scalars(select(PurchaseInboxMemo).where(
            PurchaseInboxMemo.organization_id == access.organization_id,
            PurchaseInboxMemo.record_id.in_(visible.where(PurchaseRecord.id.in_(ids))),
        ).order_by(PurchaseInboxMemo.created_at, PurchaseInboxMemo.id))
        return [memo_data(note) for note in notes]


@router.post("/{record_id}/memos", status_code=201)
def save_memo(record_id: str, body: MemoInput, access: MemoAccess):
    if record_id not in access.readable_ids or record_id not in access.writable_ids:
        raise HTTPException(404, "메모를 작성할 수 있는 증빙을 찾을 수 없습니다.")
    memo_id = uuid4().hex
    with purchases.Session.begin() as db:
        destination = select(
            literal(memo_id), PurchaseRecord.id, literal(access.organization_id), literal(access.author_id),
            literal(access.author_name), literal(body.text), literal(purchases.korea_now()),
        ).where(*accessible_records(access), PurchaseRecord.id == record_id)
        saved = db.execute(insert(PurchaseInboxMemo).from_select(
            ["id", "record_id", "organization_id", "author_id", "author_name", "text", "created_at"], destination,
        ))
        if saved.rowcount != 1:
            raise HTTPException(404, "메모를 작성할 수 있는 증빙을 찾을 수 없습니다.")
        return memo_data(db.get(PurchaseInboxMemo, memo_id))
