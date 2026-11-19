from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from itertools import groupby
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from sqlalchemy import select

from . import router as purchases
from .inbox import InboxAccess, local_evidence_access, verified_evidence_access
from .matching import evidence_date, normalized, registry_number, vendor_name
from .model import PurchaseRecord


router = APIRouter(prefix="/purchase-prices", tags=["purchase price history"])
RECENT_DAYS = 30
HISTORY_LIMIT = 10
TAX_BASES = {
    "exclusive": "exclusive", "별도": "exclusive", "부가세별도": "exclusive",
    "inclusive": "inclusive", "포함": "inclusive", "부가세포함": "inclusive",
    "exempt": "exempt", "면세": "exempt", "영세율": "zero_rated", "zero_rated": "zero_rated",
}


def require_price_access(request: Request) -> InboxAccess:
    access = verified_evidence_access(request) or local_evidence_access(
        request, "AUTOFLOW_LOCAL_PRICE_HISTORY", ("statement",), ("approved",),
    )
    if access is None:
        raise HTTPException(403, "매입 단가 이력을 열려면 서버에서 증빙 접근 권한을 확인할 수 있어야 합니다.")
    return access


Access = Annotated[InboxAccess, Depends(require_price_access)]


def text_value(value):
    return value.strip() if isinstance(value, str) else ""


def recorded_tax_basis(item, fields):
    explicit = set()
    for source in (item, fields):
        basis = text_value(source.get("tax_basis"))
        if basis:
            if (known := TAX_BASES.get(vendor_name(basis))) is None:
                return None
            explicit.add(known)
        included = source.get("tax_included")
        if type(included) is bool:
            explicit.add("inclusive" if included else "exclusive")
    return next(iter(explicit)) if len(explicit) == 1 else None


def recorded_price(value):
    if type(value) not in (int, float):
        return None
    price = Decimal(str(value))
    return price if price.is_finite() else None


@dataclass(frozen=True)
class PurchaseItem:
    record_id: str
    filename: str
    line: int
    name: str
    code: str
    vendor: str
    unit: str
    vat_basis: str | None
    day: date | None
    registered_day: date
    price: Decimal | None
    basis: tuple | None

    @property
    def id(self):
        return self.record_id, self.line

    @property
    def filter_day(self):
        return self.day or self.registered_day

    def as_row(self):
        return {
            "record_id": self.record_id, "filename": self.filename, "line": self.line,
            "item_name": self.name, "item_code": self.code, "vendor": self.vendor,
            "unit": self.unit, "tax_basis": self.vat_basis, "purchase_date": self.day,
            "unit_price": str(self.price) if self.price is not None else None,
        }


def saved_items(db, access):
    records = db.execute(select(
        PurchaseRecord.id, PurchaseRecord.filename, PurchaseRecord.fields, PurchaseRecord.created_at,
    ).where(
        PurchaseRecord.kind == "statement", PurchaseRecord.document_type == "trade_statement",
        PurchaseRecord.status == "approved", PurchaseRecord.id.in_(access.readable_ids),
    ))
    for record_id, filename, fields, created_at in records:
        vendor = text_value(fields.get("vendor"))
        business = registry_number(fields.get("business_number"))
        vendor_key = ("business", business) if business else ("name", vendor_name(vendor))
        day = evidence_date(fields.get("transaction_date"))
        items = fields.get("items")
        if not isinstance(items, list):
            continue
        for line, item in enumerate(items, 1):
            if not isinstance(item, dict):
                continue
            name = text_value(item.get("name"))
            code = text_value(item.get("item_code")) or text_value(item.get("code"))
            identity = ("code", code) if code else ("name", " ".join(normalized(name).split()))
            unit = text_value(item.get("unit"))
            basis = recorded_tax_basis(item, fields)
            comparison_key = (vendor_key, identity, " ".join(normalized(unit).split()), basis) if (
                day and vendor_key[1] and identity[1] and unit and basis
            ) else None
            yield PurchaseItem(record_id, filename, line, name, code, vendor, unit, basis, day, created_at.date(),
                               recorded_price(item.get("unit_price")), comparison_key)


class PriceHistory:
    def __init__(self, items):
        self.items = {item.id: item for item in items}
        self.groups = defaultdict(list)
        for item in self.items.values():
            if item.basis:
                self.groups[item.basis].append(item)
        self.previous = {}
        for group in self.groups.values():
            group.sort(key=lambda item: (item.day, item.id))
            preceding = None
            for _, day_items in groupby(group, key=lambda item: item.day):
                purchases_that_day = list(day_items)
                for item in purchases_that_day:
                    self.previous[item.id] = preceding
                prices = {item.price for item in purchases_that_day}
                # A date alone cannot distinguish between different prices paid on the same day.
                preceding = purchases_that_day[-1] if len(prices) == 1 else None

    def change(self, item):
        previous = self.previous.get(item.id)
        if previous is None or previous.price is None or item.price is None:
            return previous, None, None
        difference = item.price - previous.price
        rate = difference / abs(previous.price) * 100 if previous.price else None
        return previous, difference, rate

    def describe(self, item):
        previous, difference, rate = self.change(item)
        direction = ("insufficient" if difference is None else "rise" if difference > 0
                     else "fall" if difference < 0 else "unchanged")
        return {
            **item.as_row(),
            "previous_unit_price": str(previous.price) if previous and previous.price is not None else None,
            "difference": str(difference) if difference is not None else None,
            "rate": str(rate) if rate is not None else None,
            "direction": direction,
        }


@router.get("")
def list_prices(access: Access, start: date | None = None, end: date | None = None,
                vendor: Annotated[str, Query(max_length=200)] = "",
                item: Annotated[str, Query(max_length=200)] = "", rises_only: bool = False):
    today = purchases.korea_now().date()
    recent_start = today - timedelta(days=RECENT_DAYS - 1)
    start, end = start or recent_start, end or today
    if start > end:
        raise HTTPException(422, "시작일은 종료일 이전이어야 합니다.")
    vendor_filter, item_filter = vendor_name(vendor), vendor_name(item)
    with purchases.Session() as db:
        history = PriceHistory(saved_items(db, access))
    summary = {"rise": 0, "fall": 0, "insufficient": 0}
    ranked = []
    for purchase in history.items.values():
        change = history.describe(purchase)
        direction = change["direction"]
        if direction in summary and recent_start <= purchase.filter_day <= today:
            summary[direction] += 1
        if not start <= purchase.filter_day <= end or (rises_only and direction != "rise"):
            continue
        if vendor_filter not in vendor_name(purchase.vendor) or not any(
            item_filter in vendor_name(value) for value in (purchase.name, purchase.code)
        ):
            continue
        rate = Decimal(change["rate"]) if change["rate"] is not None else None
        # Undefined increases (from zero) still precede decreases.
        tier = (0 if direction == "rise" and rate is not None else 1 if direction == "rise"
                else 2 if direction == "unchanged" else 3 if direction == "fall" else 4)
        ranked.append(((tier, -(rate or Decimal(0)), -purchase.filter_day.toordinal(), purchase.id), change))
    return {"start": start, "end": end, "recent_start": recent_start, "recent_end": today, "summary": summary,
            "rows": [change for _, change in sorted(ranked, key=lambda entry: entry[0])]}


@router.get("/{record_id}/{line}")
def price_detail(record_id: str, line: Annotated[int, Path(ge=1)], access: Access):
    with purchases.Session() as db:
        history = PriceHistory(saved_items(db, access))
    item = history.items.get((record_id, line))
    if item is None:
        raise HTTPException(404, "조회할 수 있는 매입 품목을 찾을 수 없습니다.")
    previous, _, _ = history.change(item)
    recent = list(reversed(history.groups[item.basis][-HISTORY_LIMIT:])) if item.basis else [item]
    return {"current": item.as_row(), "previous": previous.as_row() if previous else None,
            "history": [purchase.as_row() for purchase in recent]}
