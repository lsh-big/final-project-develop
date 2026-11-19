from collections import defaultdict
from datetime import date
import re
import unicodedata

from sqlalchemy import select

from .model import PurchaseLink, PurchaseRecord


MATCH_DATE_WINDOW_DAYS = 7
AMOUNT_FIELDS = ("supply_amount", "tax_amount", "total_amount")
MATCH_TYPES = {"document": "tax_invoice", "statement": "trade_statement"}


def normalized(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def vendor_name(value):
    return re.sub(r"\s+", "", normalized(value))


def registry_number(value):
    return re.sub(r"[-\s]", "", normalized(value))


def evidence_date(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def amount(fields, key):
    value = fields.get(key)
    return value if type(value) is int else None


def compare(invoice, statement):
    amounts = {}
    for key in AMOUNT_FIELDS:
        left, right = amount(invoice, key), amount(statement, key)
        amounts[key] = {"invoice": left, "statement": right,
                        "difference": left - right if left is not None and right is not None else None}
    supply, tax, total = (amounts[key]["statement"] for key in AMOUNT_FIELDS)
    tax_unclear = None in (supply, tax, total) or supply + tax != total
    required_missing = any(
        not vendor_name(fields.get("vendor")) or evidence_date(fields.get("transaction_date")) is None
        or not registry_number(fields.get("business_number"))
        for fields in (invoice, statement)
    )
    return {"amounts": amounts, "tax_unclear": tax_unclear, "required_missing": required_missing,
            "amount_review": "check" if tax_unclear or any(item["difference"] != 0 for item in amounts.values()) else "matched"}


def candidate_reason(current, other):
    left, right = current["fields"], other["fields"]
    numbers = [registry_number(fields.get("business_number")) for fields in (left, right)]
    dates = [evidence_date(fields.get("transaction_date")) for fields in (left, right)]
    if not all(dates):
        return None
    days = abs((dates[0] - dates[1]).days)
    if days > MATCH_DATE_WINDOW_DAYS:
        return None
    total = amount(left, "total_amount")
    exact_total = total is not None and total == amount(right, "total_amount")
    if all(numbers):
        if numbers[0] != numbers[1]:
            return None
        reasons = ["공급자 사업자등록번호 일치"]
    else:
        name = vendor_name(left.get("vendor"))
        if not name or name != vendor_name(right.get("vendor")) or days or not exact_total:
            return None
        missing = ["세금계산서" if row["kind"] == "document" else "거래명세서"
                   for row, number in zip((current, other), numbers) if not number]
        reasons = ["거래처명·날짜·합계금액 일치", f"{', '.join(missing)} 사업자등록번호 누락 · 동일 공급자 확인 필요"]
    reasons.extend([f"날짜 차이 {days}일", "합계금액 일치" if exact_total else (
        "합계금액 확인 필요" if total is None or amount(right, "total_amount") is None else "합계금액 차이 있음")])
    return (not exact_total, days, not all(numbers), other["id"]), reasons, "high" if all(numbers) else "low"


class EvidenceMatches:
    def __init__(self, db, effective_fields, *, record_ids=None):
        query = select(
            PurchaseRecord.id, PurchaseRecord.kind, PurchaseRecord.filename, PurchaseRecord.fields,
            PurchaseRecord.status, PurchaseRecord.document_type, PurchaseRecord.extraction,
        ).where(
            PurchaseRecord.status.in_(("review", "approved")),
            PurchaseRecord.document_type.in_(MATCH_TYPES.values()),
        )
        if record_ids is not None:
            query = query.where(PurchaseRecord.id.in_(record_ids))
        rows = db.execute(query).mappings()
        self.rows = {}
        self.by_number = defaultdict(list)
        self.by_name = defaultdict(list)
        for row in rows:
            if MATCH_TYPES.get(row.kind) != row.document_type:
                continue
            record = {key: row[key] for key in ("id", "kind", "filename")}
            record["fields"] = effective_fields(row)
            self.rows[row.id] = record
            number = registry_number(record["fields"].get("business_number"))
            name = vendor_name(record["fields"].get("vendor"))
            if number:
                self.by_number[row.kind, number].append(record)
            if name:
                self.by_name[row.kind, name].append(record)
        self.links = {}
        links = select(PurchaseLink.invoice_id, PurchaseLink.statement_id)
        if record_ids is not None:
            links = links.where(PurchaseLink.invoice_id.in_(record_ids), PurchaseLink.statement_id.in_(record_ids))
        for invoice_id, statement_id in db.execute(links):
            self.links[invoice_id] = statement_id
            self.links[statement_id] = invoice_id

    @staticmethod
    def comparison(current, other):
        invoice, statement = (current, other) if current["kind"] == "document" else (other, current)
        return compare(invoice["fields"], statement["fields"])

    def candidates(self, current):
        kind = "statement" if current["kind"] == "document" else "document"
        number = registry_number(current["fields"].get("business_number"))
        name = vendor_name(current["fields"].get("vendor"))
        possible = {row["id"]: row for row in (
            self.by_number[kind, number] + self.by_name[kind, name]
        )}
        ranked = []
        for other in possible.values():
            reason = candidate_reason(current, other) if other["id"] not in self.links else None
            if reason:
                order, reasons, confidence = reason
                ranked.append((order, {"record": other, "reasons": reasons, "confidence": confidence,
                                       "comparison": self.comparison(current, other)}))
        return [candidate for _, candidate in sorted(ranked, key=lambda item: item[0])]

    def describe(self, record_id, full=False):
        current = self.rows.get(record_id)
        if current is None:
            return {"status": "unlinked", "linked": None, "candidates": []}
        linked = self.rows.get(self.links.get(record_id))
        candidates = [] if linked else self.candidates(current)
        comparison = self.comparison(current, linked) if linked else candidates[0]["comparison"] if candidates else None
        fields = current["fields"]
        incomplete = (not vendor_name(fields.get("vendor")) or not registry_number(fields.get("business_number"))
                      or evidence_date(fields.get("transaction_date")) is None
                      or any(amount(fields, key) is None for key in AMOUNT_FIELDS))
        check = incomplete or len(candidates) > 1 or (
            comparison is not None and (comparison["amount_review"] == "check" or comparison["required_missing"])
        )
        result = {"status": "check" if check else "linked" if linked else "unlinked"}
        if full:
            result.update(linked={"record": linked, "comparison": comparison} if linked else None,
                          candidates=candidates)
        return result
