from base64 import b64encode
from contextlib import asynccontextmanager
from datetime import timedelta
from hashlib import sha256
from io import BytesIO
import logging
from pathlib import Path
import os
import re
from secrets import token_urlsafe
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from python_multipart.exceptions import MultipartParseError
import qrcode
from sqlalchemy import delete, select, update
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from app.db import Session
from .model import MobileReceiptBatchResult, MobileReceiptTicket
from .router import MAX_BYTES, clean_upload_filename, init_db, korea_now, read_upload, store_upload

logger = logging.getLogger(__name__)
LIFETIME = timedelta(minutes=5)
MAX_BATCH_FILES = 20
MAX_BATCH_BYTES = MAX_BATCH_FILES * MAX_BYTES + 65_536
UPLOAD_GRACE = timedelta(seconds=MAX_BATCH_FILES * 20 + 30)
ASSETS = Path(__file__).with_name("mobile_assets")
control_router = APIRouter(tags=["receipt-mobile"])


@asynccontextmanager
async def mobile_lifespan(app):
    init_db()
    yield


mobile_app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, lifespan=mobile_lifespan)


@mobile_app.middleware("http")
async def mobile_headers(request, call_next):
    response = await call_next(request)
    response.headers.update({
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
                                   "connect-src 'self'; img-src blob:; base-uri 'none'; frame-ancestors 'none'",
    })
    return response


def mobile_origin():
    origin = os.getenv("RECEIPT_MOBILE_URL", "").strip().rstrip("/")
    try:
        parsed = urlsplit(origin)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not (
            parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(503, "휴대폰 업로드 주소가 설정되지 않았습니다. README의 실행 방법을 확인해 주세요.")
    return origin


@control_router.post("/mobile-sessions")
def create_mobile_session(response: Response):
    origin = mobile_origin()
    token = token_urlsafe(32)
    url = f"{origin}/#{token}"
    expires_at = korea_now() + LIFETIME
    ticket = MobileReceiptTicket(id=uuid4().hex, token_hash=sha256(token.encode()).hexdigest(), expires_at=expires_at)
    output = BytesIO()
    qrcode.make(url).save(output, format="PNG")
    with Session() as db:
        expired = MobileReceiptTicket.expires_at < korea_now() - timedelta(days=1)
        db.execute(delete(MobileReceiptBatchResult).where(MobileReceiptBatchResult.ticket_id.in_(
            select(MobileReceiptTicket.id).where(expired)
        )))
        db.execute(delete(MobileReceiptTicket).where(expired))
        db.add(ticket)
        db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {
        "id": ticket.id, "url": url, "expires_at": expires_at.isoformat() + "+09:00",
        "qr_image": f"data:image/png;base64,{b64encode(output.getvalue()).decode('ascii')}",
    }


@control_router.get("/mobile-sessions/{ticket_id}")
def mobile_session_status(ticket_id: str, response: Response):
    with Session() as db:
        ticket = db.get(MobileReceiptTicket, ticket_id)
        if ticket is None:
            raise HTTPException(404, "전송 요청을 찾을 수 없습니다.")
        state = ticket.state
        deadline = ticket.expires_at + (UPLOAD_GRACE if state == "uploading" else timedelta())
        if state != "received" and deadline <= korea_now():
            state = "expired"
        response.headers["Cache-Control"] = "no-store"
        batch = db.get(MobileReceiptBatchResult, ticket_id) if state == "received" else None
        return {
            "state": state, "record_id": ticket.record_id, "duplicate": ticket.duplicate,
            "items": batch.items if batch else [],
        }


@mobile_app.get("/")
def mobile_page():
    return FileResponse(ASSETS / "index.html", media_type="text/html; charset=utf-8")


@mobile_app.get("/upload.js")
def mobile_script():
    return FileResponse(ASSETS / "upload.js", media_type="text/javascript; charset=utf-8")


def waiting_ticket(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise HTTPException(410, "QR이 만료되었거나 이미 사용되었습니다. PC에서 새 QR을 열어 주세요.")
    digest = sha256(token.encode()).hexdigest()
    with Session() as db:
        ticket = db.scalar(select(MobileReceiptTicket).where(MobileReceiptTicket.token_hash == digest))
        if ticket is None or ticket.state != "waiting" or ticket.expires_at <= korea_now():
            raise HTTPException(410, "QR이 만료되었거나 이미 사용되었습니다. PC에서 새 QR을 열어 주세요.")
        return ticket.id


def claim_ticket(ticket_id):
    with Session() as db:
        claimed = db.execute(update(MobileReceiptTicket).where(
            MobileReceiptTicket.id == ticket_id, MobileReceiptTicket.state == "waiting",
            MobileReceiptTicket.expires_at > korea_now(),
        ).values(state="uploading"))
        if claimed.rowcount != 1:
            raise HTTPException(410, "QR이 만료되었거나 이미 사용되었습니다. PC에서 새 QR을 열어 주세요.")
        db.commit()


def finish_ticket(ticket_id, first=None, items=None):
    with Session() as db:
        values = {"state": "waiting"}
        if first:
            values = {"state": "received", "record_id": first["record_id"], "duplicate": first["duplicate"]}
            if items is not None:
                db.add(MobileReceiptBatchResult(ticket_id=ticket_id, items=items))
        db.execute(update(MobileReceiptTicket).where(MobileReceiptTicket.id == ticket_id).values(**values))
        db.commit()


def receive(ticket_id, filename, content, confirm_suspected=False):
    claim_ticket(ticket_id)
    try:
        result = store_upload("receipt", filename, content, confirm_suspected)
    except Exception:
        finish_ticket(ticket_id)
        raise
    finish_ticket(ticket_id, {"record_id": result["record"]["id"], "duplicate": result["duplicate"]})
    return {"received": True, "duplicate": result["duplicate"]}


@mobile_app.post("/upload")
async def mobile_upload(request: Request, filename: str, confirm_suspected: bool = False):
    ticket_id = await run_in_threadpool(waiting_ticket, request)
    filename, content = await read_upload(request, filename)
    return await run_in_threadpool(receive, ticket_id, filename, content, confirm_suspected)


class BatchTooLarge(MultiPartException):
    pass


async def limited_batch_stream(request):
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BATCH_BYTES:
                raise BatchTooLarge("선택한 사진의 전체 용량이 너무 큽니다.")
            yield chunk
    except ClientDisconnect as exc:
        raise MultiPartException("사진 전송이 중단되었습니다.") from exc


def receive_batch(ticket_id, photos, confirm_suspected=False):
    items = []
    first = None
    for photo in photos:
        filename = photo.filename or ""
        try:
            filename = clean_upload_filename(filename)
            content = photo.file.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise HTTPException(413, "파일 크기는 최대 10 MB입니다.")
            result = store_upload("receipt", filename, content, confirm_suspected)
            item = {"filename": filename, "record_id": result["record"]["id"], "duplicate": result["duplicate"]}
            first = first or item
        except HTTPException as exc:
            detail = exc.detail
            item = {"filename": filename, "error": detail["message"] if isinstance(detail, dict) else str(detail)}
            if isinstance(detail, dict) and detail.get("code") == "suspected_duplicate":
                item["suspected_duplicate"] = True
        except Exception:
            logger.exception("Mobile receipt extraction or save failed")
            item = {"filename": filename, "error": "사진을 처리하지 못했습니다. 다시 시도해 주세요."}
        items.append(item)

    finish_ticket(ticket_id, first, items)
    return {
        "received": first is not None,
        "items": [{key: value for key, value in item.items() if key != "record_id"} for item in items],
    }


def release_uploading_ticket(ticket_id):
    with Session() as db:
        db.execute(update(MobileReceiptTicket).where(
            MobileReceiptTicket.id == ticket_id, MobileReceiptTicket.state == "uploading",
        ).values(state="waiting"))
        db.commit()


@mobile_app.post("/upload-batch")
async def mobile_upload_batch(request: Request, confirm_suspected: bool = False):
    ticket_id = await run_in_threadpool(waiting_ticket, request)
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data;"):
        raise HTTPException(415, "사진을 선택한 뒤 전송해 주세요.")
    declared_size = request.headers.get("content-length", "")
    if declared_size.isdigit() and int(declared_size) > MAX_BATCH_BYTES:
        raise HTTPException(413, "선택한 사진의 전체 용량이 너무 큽니다.")
    await run_in_threadpool(claim_ticket, ticket_id)
    form = None
    finished = False
    try:
        form = await MultiPartParser(
            request.headers, limited_batch_stream(request), max_files=MAX_BATCH_FILES, max_fields=0,
        ).parse()
        photos = form.getlist("photos")
        if not photos or len(photos) != len(form.multi_items()) or any(not isinstance(photo, UploadFile) for photo in photos):
            raise HTTPException(422, "전송할 사진을 선택해 주세요.")
        result = await run_in_threadpool(receive_batch, ticket_id, photos, confirm_suspected)
        finished = True
        return result
    except BatchTooLarge as exc:
        raise HTTPException(413, exc.message) from exc
    except (MultiPartException, MultipartParseError) as exc:
        raise HTTPException(422, f"사진을 {MAX_BATCH_FILES}장 이하로 선택한 뒤 다시 전송해 주세요.") from exc
    finally:
        if form is not None:
            await form.close()
        if not finished:
            await run_in_threadpool(release_uploading_ticket, ticket_id)
