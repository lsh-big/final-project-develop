from datetime import datetime

from sqlalchemy import JSON, DateTime, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGBLOB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class PurchaseRecord(Base):
    __tablename__ = "purchase_records"
    __table_args__ = (
        UniqueConstraint("kind", "file_hash"),
        UniqueConstraint("kind", "approval_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    file_hash: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(32))
    original: Mapped[bytes] = mapped_column(LargeBinary().with_variant(LONGBLOB(), "mysql"))
    document_type: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16), default="review", index=True)
    fields: Mapped[dict] = mapped_column(JSON)
    extraction: Mapped[dict] = mapped_column(JSON)
    vendor: Mapped[str] = mapped_column(String(200), default="")
    approval_key: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime)


class MobileReceiptTicket(Base):
    __tablename__ = "mobile_receipt_tickets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    state: Mapped[str] = mapped_column(String(16), default="waiting")
    record_id: Mapped[str | None] = mapped_column(String(32))
    duplicate: Mapped[bool] = mapped_column(default=False)


class MobileReceiptBatchResult(Base):
    __tablename__ = "mobile_receipt_batch_results"

    ticket_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    items: Mapped[list[dict]] = mapped_column(JSON)
