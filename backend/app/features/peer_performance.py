from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
import re
from threading import Lock
from time import monotonic
from typing import Literal
from urllib.parse import urlencode
from urllib.request import urlopen
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

import httpx
from fastapi import APIRouter, HTTPException, Query


router = APIRouter(prefix="/peer-performance", tags=["peer performance"])
COMPANIES = {"095340": "ISC", "425420": "티에프이", "098120": "마이크로컨텍솔", "080580": "오킨스전자", "131290": "티에스이"}
ISC = "095340"
ANNUAL_REPORT = "11011"
Basis = Literal["CFS", "OFS"]
DATA_TTL = 6 * 60 * 60
ERROR_TTL = 30
REVENUE_IDS = {"ifrs-full_Revenue", "ifrs_Revenue", "dart_Revenue"}
OPERATING_IDS = {"dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"}
REVENUE_NAMES = {"매출", "매출액", "수익", "수익(매출액)", "영업수익", "매출액(수익)"}
OPERATING_NAMES = {"영업이익", "영업손실", "영업이익(손실)", "영업손익", "영업손실(이익)"}


class DartError(Exception):
    def __init__(self, status):
        self.status = status


@dataclass(frozen=True)
class Filing:
    status: str
    revenue: int | None = None
    operating_profit: int | None = None
    receipt: str | None = None
    fetched_at: datetime | None = None


def dart_get(endpoint, api_key, **params):
    query = urlencode({"crtfc_key": api_key, **params})
    # The request URL contains the API key. Keep transport URLs and upstream errors out of logs.
    try:
        with urlopen(f"https://opendart.fss.or.kr/api/{endpoint}?{query}", timeout=8) as response:
            content = response.read(16_000_001)
    except (OSError, ValueError):
        raise DartError("temporary_error") from None
    if len(content) > 16_000_000:
        raise DartError("temporary_error")
    return content


def check_dart_status(code):
    if code == "000":
        return
    if code in {"013", "014"}:
        raise DartError("missing")
    if code in {"010", "011", "012", "901"}:
        raise DartError("key_rejected")
    raise DartError("temporary_error")


def parse_corporations(content):
    try:
        with ZipFile(BytesIO(content)) as archive:
            xml = next(item for item in archive.infolist() if item.filename.lower().endswith(".xml"))
            if xml.file_size > 48_000_000:
                raise DartError("temporary_error")
            with archive.open(xml) as source:
                result = {}
                for _, row in ElementTree.iterparse(source, events=("end",)):
                    if row.tag == "list":
                        stock = (row.findtext("stock_code") or "").strip()
                        corp = (row.findtext("corp_code") or "").strip()
                        if stock in COMPANIES and re.fullmatch(r"[0-9]{8}", corp):
                            result[stock] = corp
                        row.clear()
                return result
    except BadZipFile:
        try:
            check_dart_status(ElementTree.fromstring(content).findtext("status"))
        except ElementTree.ParseError:
            pass
        raise DartError("temporary_error") from None
    except (ElementTree.ParseError, StopIteration, OSError, RuntimeError):
        raise DartError("temporary_error") from None


def parse_amount(value):
    text = str(value if value is not None else "").strip().replace(",", "")
    if re.fullmatch(r"\([0-9]+\)", text):
        text = f"-{text[1:-1]}"
    return int(text) if re.fullmatch(r"-?[0-9]{1,18}", text) else None


def account_value(rows, ids, names):
    for candidates in ([row for row in rows if row.get("account_id") in ids],
                       [row for row in rows if re.sub(r"\s+", "", row.get("account_nm") or "") in names]):
        if candidates:
            values = set()
            for row in candidates:
                receipt = str(row.get("rcept_no", ""))
                if str(row.get("currency", "")).strip().upper() != "KRW" or not re.fullmatch(r"[0-9]{14}", receipt):
                    return None, None
                values.add((parse_amount(row.get("thstrm_amount")), receipt))
            return next(iter(values)) if len(values) == 1 else (None, None)
    return None, None


def parse_filing(payload, corp, year, basis, timestamp):
    check_dart_status(payload.get("status"))
    eligible = [row for row in payload["list"] if row.get("corp_code") == corp
                and str(row.get("bsns_year")) == str(year) and row.get("reprt_code") == ANNUAL_REPORT
                and row.get("fs_div", basis) == basis and row.get("sj_div") in {"IS", "CIS"}]
    # The full statements endpoint specifies the basis in the request. Its documented rows do not include fs_div.
    rows = [row for row in eligible if row["sj_div"] == "IS"] or eligible
    revenue, revenue_source = account_value(rows, REVENUE_IDS, REVENUE_NAMES)
    profit, profit_source = account_value(rows, OPERATING_IDS, OPERATING_NAMES)
    if revenue is None and profit is None or revenue is not None and profit is not None and revenue_source != profit_source:
        return Filing("not_comparable", fetched_at=timestamp)
    receipt = revenue_source if revenue is not None else profit_source
    status = "ok" if revenue is not None and profit is not None else "partial"
    return Filing(status, revenue, profit, receipt, timestamp)


class DartCache:
    def __init__(self):
        self.entries = {}
        self.locks = {}
        self.guard = Lock()

    def get(self, slot, credential, loader):
        with self.guard:
            lock = self.locks.setdefault(slot, Lock())
        with lock:
            cached = self.entries.get(slot)
            if cached and cached[0] == credential and monotonic() < cached[1]:
                return cached[2]
            value = loader()
            temporary = value is None or isinstance(value, Filing) and value.status in {"temporary_error", "key_rejected"}
            self.entries[slot] = (credential, monotonic() + (ERROR_TTL if temporary else DATA_TTL), value)
            return value


cache = DartCache()


def load_filing(api_key, corp, year, basis):
    try:
        payload = json.loads(dart_get("fnlttSinglAcntAll.json", api_key, corp_code=corp, bsns_year=year,
                                     reprt_code=ANNUAL_REPORT, fs_div=basis))
        return parse_filing(payload, corp, year, basis, datetime.now(timezone.utc))
    except DartError as exc:
        return Filing(exc.status, fetched_at=datetime.now(timezone.utc) if exc.status == "missing" else None)
    except (ValueError, KeyError, AttributeError, TypeError):
        return Filing("temporary_error")


def operating_margin(filing):
    if filing and filing.operating_profit is not None and filing.revenue not in (None, 0):
        return filing.operating_profit * 100 / filing.revenue
    return None


def compare(codes, start, end, basis):
    api_key = os.getenv("OPENDART_API_KEY", "").strip()
    if not api_key:
        filings = {}
        unavailable = "key_missing"
    else:
        credential = sha256(api_key.encode()).digest()
        try:
            mapping = cache.get("corporations", credential, lambda: parse_corporations(dart_get("corpCode.xml", api_key)))
            keys = [(code, year) for code in codes for year in range(start - 1, end + 1) if code in mapping]
            with ThreadPoolExecutor(max_workers=5) as pool:
                filings = dict(zip(keys, pool.map(lambda key: cache.get(
                    (*key, basis), credential, lambda: load_filing(api_key, mapping[key[0]], key[1], basis)
                ), keys)))
            unavailable = "corporation_missing"
        except DartError as exc:
            filings = {}
            unavailable = "temporary_error" if exc.status == "missing" else exc.status
    rows = []
    for code in codes:
        for year in range(start, end + 1):
            current = filings.get((code, year), Filing(unavailable))
            previous = filings.get((code, year - 1), Filing(unavailable))
            growth = ((current.revenue - previous.revenue) * 100 / previous.revenue
                      if current.revenue is not None and previous.revenue not in (None, 0) else None)
            rows.append({"code": code, "name": COMPANIES[code], "year": year, "basis": basis,
                         "status": current.status, "revenue": current.revenue, "operating_profit": current.operating_profit,
                         "revenue_growth": growth, "operating_margin": operating_margin(current),
                         "report_url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={current.receipt}" if current.receipt else None})
    timestamp = max((filing.fetched_at for filing in filings.values() if filing.fetched_at), default=None)
    prior_margin = operating_margin(filings.get((ISC, end - 1)))
    upstream_issues = {filing.status for filing in filings.values()} | {row["status"] for row in rows}
    return {"rows": rows, "last_fetched_at": timestamp,
            "upstream_issues": sorted(upstream_issues & {"key_missing", "key_rejected", "temporary_error"}),
            "summary": summary_facts(rows, codes, end, prior_margin)}


def summary_facts(rows, codes, end, prior_margin):
    latest = [row for row in rows if row["year"] == end and row["revenue_growth"] is not None]
    current = next(row for row in rows if row["code"] == ISC and row["year"] == end)
    growth = current["revenue_growth"]
    margin = current["operating_margin"]
    change = margin - prior_margin if margin is not None and prior_margin is not None else None
    rank = 1 + sum(row["revenue_growth"] > growth for row in latest) if growth is not None else None
    tied = growth is not None and sum(row["revenue_growth"] == growth for row in latest) > 1
    sentences = {}
    if growth is None:
        sentences["rank"] = f"{end}년 ISC의 매출 증감률을 계산할 수 없어 순위를 확인할 수 없습니다."
    elif len(latest) == 1:
        sentences["rank"] = f"{end}년 ISC의 매출 증감률은 {growth:+.2f}%이며, 비교 가능한 다른 업체가 없습니다."
    else:
        sentences["rank"] = f"{end}년 ISC의 매출 증감률은 {growth:+.2f}%로, 비교 가능한 {len(latest)}개사 중 {'공동 ' if tied else ''}{rank}위입니다."
    if change is None:
        sentences["margin"] = "선택한 기간의 데이터로는 ISC 영업이익률의 전년 대비 변화를 확인할 수 없습니다."
    else:
        direction = "변동 없이 유지됐습니다" if abs(change) < .005 else f"{abs(change):.2f}%p {'상승했습니다' if change > 0 else '하락했습니다'}"
        diverged = growth is not None and abs(growth) >= .005 and abs(change) >= .005 and growth * change < 0
        sales = f"매출은 {'증가' if growth > 0 else '감소'}했지만, " if diverged else ""
        sentences["margin"] = f"{sales}ISC 영업이익률은 전년 대비 {direction}."
    if len(latest) < len(codes):
        sentences["scope"] = f"선택한 {len(codes)}개사 중 {len(latest)}개사의 매출 증감률을 계산했습니다."
    return {"year": end, "revenue_growth": growth, "rank": rank, "tied": tied, "operating_margin": margin,
            "margin_change": change, "comparable_count": len(latest), "selected_count": len(codes), "sentences": sentences}


def selection(companies, start, end):
    requested = list(COMPANIES) if companies is None else companies.split(",")
    if start > end or any(code not in COMPANIES for code in requested) or ISC not in requested:
        raise HTTPException(422, "사업연도와 비교 업체를 확인해 주세요.")
    return [code for code in COMPANIES if code in requested]


@router.get("")
def performance(basis: Basis = "CFS", start: int = Query(2023, ge=2023, le=2025),
                end: int = Query(2025, ge=2023, le=2025), companies: str | None = Query(None, max_length=34)):
    return compare(selection(companies, start, end), start, end, basis)


summary_cache = DartCache()


def ai_order(facts):
    key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("PEER_AI_MODEL", "").strip()
    if not key or not model or facts["comparable_count"] == 0:
        return None
    try:
        response = httpx.post("https://api.openai.com/v1/responses", timeout=10,
                              headers={"Authorization": f"Bearer {key}"}, json={
                                  "model": model, "store": False, "max_output_tokens": 400,
                                  "instructions": "Arrange the verified Korean summary sentences. Return JSON {\"order\":[...]} listing every sentence key exactly once. Keep rank and margin first, in the most helpful order. Do not add text, causes, or forecasts.",
                                  "input": json.dumps(facts, ensure_ascii=False),
                                  "text": {"format": {"type": "json_object"}},
                              })
        response.raise_for_status()
        result = response.json()
        if result.get("status") != "completed":
            return None
        text = "".join(part["text"] for item in result["output"] if item.get("type") == "message"
                       for part in item["content"] if part.get("type") == "output_text")
        order = json.loads(text)["order"]
        if (isinstance(order, list) and all(isinstance(item, str) for item in order)
                and len(order) == len(facts["sentences"]) and set(order) == set(facts["sentences"])):
            return order
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return None


@router.get("/summary")
def performance_summary(basis: Basis = "CFS", start: int = Query(2023, ge=2023, le=2025),
                        end: int = Query(2025, ge=2023, le=2025), companies: str | None = Query(None, max_length=34)):
    codes = selection(companies, start, end)
    facts = compare(codes, start, end, basis)["summary"]
    context = [facts, os.getenv("PEER_AI_MODEL"), os.getenv("OPENAI_API_KEY")]
    fingerprint = sha256(json.dumps(context, ensure_ascii=False, sort_keys=True).encode()).digest()
    order = summary_cache.get((tuple(codes), start, end, basis), fingerprint, lambda: ai_order(facts))
    sentences = facts["sentences"]
    return {"source": "ai" if order else "calculated", "sentences": [sentences[name] for name in order or sentences]}
