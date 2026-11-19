from io import BytesIO
import json
from zipfile import ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx

from app.features import peer_performance as peer


def fake_reports(monkeypatch):
    monkeypatch.setenv("OPENDART_API_KEY", "test-only-key")
    monkeypatch.setattr(peer, "cache", peer.DartCache())
    codes = {stock: f"{index:08}" for index, stock in enumerate(peer.COMPANIES, 1)}
    archive = BytesIO()
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("CORPCODE.xml", "<result>" + "".join(
            f"<list><stock_code>{stock}</stock_code><corp_code>{corp}</corp_code></list>" for stock, corp in codes.items()
        ) + "</result>")
    calls = []

    def fetch(endpoint, key, **params):
        assert key == "test-only-key"
        calls.append((endpoint, params))
        if endpoint == "corpCode.xml":
            return archive.getvalue()
        year, corp, basis = params["bsns_year"], params["corp_code"], params["fs_div"]
        assert params["reprt_code"] == "11011"
        if corp == codes["131290"] and basis == "CFS":
            return b'{"status":"013"}'
        if corp == codes["080580"] and year == 2025 and basis == "CFS":
            return b'{"status":"800"}'
        revenue = {2022: 10, 2023: 0, 2024: 100, 2025: 125}[year]
        if corp == codes["058470"] and year == 2025:
            revenue = 150
        if basis == "OFS":
            revenue *= 2
        base = {"rcept_no": f"{year + 1}0301000001", "corp_code": corp, "bsns_year": str(year),
                "reprt_code": "11011", "sj_div": "IS", "currency": "KRW"}
        reported = [
            {**base, "account_id": "ifrs-full_Revenue", "account_nm": "수익(매출액)", "thstrm_amount": f"{revenue * 100_000_000:,}"},
            {**base, "account_id": "-표준계정코드 미사용-", "account_nm": "영업이익 (손실)",
             "thstrm_amount": "(1,200,000,000)" if year == 2025 else "2,000,000,000"},
        ]
        return json.dumps({"status": "000", "list": reported}).encode()

    monkeypatch.setattr(peer, "dart_get", fetch)
    return calls


def test_annual_comparison_is_cached_and_keeps_statement_bases_separate(monkeypatch):
    calls = fake_reports(monkeypatch)
    app = FastAPI()
    app.include_router(peer.router)
    with TestClient(app) as client:
        response = client.get("/peer-performance")
        assert response.is_success and "test-only-key" not in response.text
        data = response.json()
        rows = {(row["code"], row["year"]): row for row in data["rows"]}
        assert len(rows) == 15 and data["last_fetched_at"]
        assert not any(year == 2022 for _, year in rows)
        assert len(calls) == 21 and any(params.get("bsns_year") == 2022 for _, params in calls)
        assert rows[(peer.ISC, 2023)]["revenue"] == 0
        assert rows[(peer.ISC, 2023)]["revenue_growth"] == -100
        assert rows[(peer.ISC, 2023)]["operating_margin"] is None
        assert rows[(peer.ISC, 2024)]["revenue_growth"] is None
        assert rows[(peer.ISC, 2025)]["operating_profit"] == -1_200_000_000
        assert rows[(peer.ISC, 2025)]["report_url"] == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260301000001"
        assert rows[("131290", 2025)]["status"] == "missing"
        assert rows[("080580", 2025)]["status"] == "temporary_error"
        assert data["summary"]["rank"] == 2 and data["summary"]["tied"]
        assert data["summary"]["comparable_count"] == 3
        assert data["summary"]["margin_change"] == -29.6
        assert "매출은 증가했지만" in data["summary"]["sentences"]["margin"]

        narrowed = client.get("/peer-performance?companies=095340&start=2025").json()
        assert len(narrowed["rows"]) == 1 and len(calls) == 21
        assert narrowed["summary"]["margin_change"] == -29.6
        separate = client.get("/peer-performance?basis=OFS&companies=095340&start=2025").json()
        assert separate["rows"][0]["basis"] == "OFS"
        assert separate["rows"][0]["revenue"] == 25_000_000_000
        assert len(calls) == 23 and all(params["fs_div"] == "OFS" for _, params in calls[-2:])
        assert client.get("/peer-performance?companies=000000").status_code == 422


def test_parser_rejects_other_currency_year_basis_and_conflicting_receipts():
    corp = "00000001"
    date = peer.datetime.now(peer.timezone.utc)
    base = {"rcept_no": "20260301000001", "corp_code": corp, "bsns_year": "2025", "reprt_code": "11011",
            "sj_div": "CIS", "currency": "KRW", "fs_div": "CFS"}
    revenue = {**base, "account_id": "ifrs-full_Revenue", "thstrm_amount": "12,000"}
    profit = {**base, "account_id": "dart_OperatingIncomeLoss", "thstrm_amount": "0"}
    for changes in ({"currency": "USD"}, {"bsns_year": "2024"}, {"fs_div": "OFS"}, {"reprt_code": "11014"}):
        payload = {"status": "000", "list": [{**revenue, **changes}, profit]}
        filing = peer.parse_filing(payload, corp, 2025, "CFS", date)
        assert filing.status == "partial" and filing.revenue is None and filing.operating_profit == 0
    conflict = {"status": "000", "list": [{**revenue, "rcept_no": "20260301000002"}, profit]}
    assert peer.parse_filing(conflict, corp, 2025, "CFS", date).status == "not_comparable"


def test_missing_key_and_ai_summary_failure(monkeypatch):
    monkeypatch.delenv("OPENDART_API_KEY", raising=False)
    assert {row["status"] for row in peer.compare(list(peer.COMPANIES), 2023, 2025, "CFS")["rows"]} == {"key_missing"}
    fake_reports(monkeypatch)
    monkeypatch.setattr(peer, "summary_cache", peer.DartCache())
    monkeypatch.setenv("OPENAI_API_KEY", "test-ai-key")
    monkeypatch.setenv("PEER_AI_MODEL", "test-model")

    def provider(url, **options):
        payload = options["json"]
        facts = json.loads(payload["input"])
        assert payload["store"] is False and facts["comparable_count"] == 3
        assert facts["rank"] == 2 and facts["margin_change"] == -29.6
        return httpx.Response(200, request=httpx.Request("POST", url), json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": '{"order":["margin","rank","scope"]}'}]},
        ]})

    monkeypatch.setattr(peer.httpx, "post", provider)
    app = FastAPI()
    app.include_router(peer.router)
    with TestClient(app) as client:
        assert client.get("/peer-performance/summary").json()["source"] == "ai"
        monkeypatch.setenv("PEER_AI_MODEL", "different-model")
        def fail(*args, **kwargs):
            raise httpx.ConnectError("unreachable")
        monkeypatch.setattr(peer.httpx, "post", fail)
        fallback = client.get("/peer-performance/summary").json()
        assert fallback["source"] == "calculated" and len(fallback["sentences"]) == 3
        assert client.get("/peer-performance").is_success
