import { useEffect, useState } from 'react';
import { ArrowLeft, ArrowUpRight } from 'lucide-react';
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { request } from '../services/api.js';

const endpoint = '/api/v1/peer-performance';
const years = [2023, 2024, 2025];
const companies = [
  { code: '095340', name: 'ISC', color: '#1d4ed8', dash: undefined },
  { code: '425420', name: '티에프이', color: '#0f766e', dash: '6 3' },
  { code: '098120', name: '마이크로컨텍솔', color: '#7e22ce', dash: '3 3' },
  { code: '080580', name: '오킨스전자', color: '#a16207', dash: '8 3 2 3' },
  { code: '131290', name: '티에스이', color: '#be185d', dash: '2 3' },
];
const iscCode = companies[0].code;
const basisNames = { CFS: '연결', OFS: '별도' } as const;
const basisScopes = { CFS: '그룹 전체', OFS: '모회사 기준' } as const;
const metricNames = { revenue: '매출액', revenue_growth: '매출 증감률', operating_margin: '영업이익률' } as const;
type Metric = keyof typeof metricNames;
const metrics = Object.keys(metricNames) as Metric[];
type Basis = keyof typeof basisNames;
type FilingStatus = 'ok' | 'partial' | 'missing' | 'not_comparable' | 'key_missing' | 'key_rejected' | 'temporary_error' | 'corporation_missing';
type Row = {
  code: string; name: string; year: number; basis: Basis; status: FilingStatus;
  revenue: number | null; operating_profit: number | null; revenue_growth: number | null;
  operating_margin: number | null; report_url: string | null;
};
type Comparison = {
  rows: Row[]; last_fetched_at: string | null; upstream_issues: FilingStatus[];
  summary: { comparable_count: number; sentences: Record<string, string> };
};
type Summary = { source: 'ai' | 'calculated'; sentences: string[] };

const panel = 'rounded-xl border border-slate-200 bg-white p-4 sm:p-5';
const button = 'rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50 focus-visible:outline-blue-700';
const amountFormat = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 2 });
const smallAmountFormat = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 8 });
const percentFormat = new Intl.NumberFormat('ko-KR', { minimumFractionDigits: 1, maximumFractionDigits: 2 });
const dateFormat = new Intl.DateTimeFormat('ko-KR', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Seoul' });
const wonPerUnit = 100_000_000;

function amount(value: number | null | undefined) {
  if (value == null) return '—';
  if (value === 0) return '0원';
  const units = value / wonPerUnit;
  return `${(Math.abs(units) < .01 ? smallAmountFormat : amountFormat).format(units)}억 원`;
}

function percent(value: number | null | undefined, signed = false) {
  return value == null ? '—' : `${signed && value > 0 ? '+' : ''}${percentFormat.format(value)}%`;
}

function performanceFields(row: Row) {
  return [
    [metricNames.revenue, amount(row.revenue)], [metricNames.revenue_growth, percent(row.revenue_growth, true)],
    ['영업이익', amount(row.operating_profit)], [metricNames.operating_margin, percent(row.operating_margin)],
  ];
}

function filingLabel(row: Row) {
  switch (row.status) {
    case 'ok': return '';
    case 'missing': return `${basisNames[row.basis]} 공시 없음`;
    case 'partial': return '일부 항목 확인 불가';
    case 'not_comparable': return '비교 가능한 원화 손익 항목 없음';
    case 'corporation_missing': return '공시 기업코드 없음';
    case 'key_missing': return 'API 키 미설정';
    case 'key_rejected': return 'API 키 또는 접근 설정 확인';
    case 'temporary_error': return '일시적 조회 오류';
  }
}

function ReportLink({ row }: { row: Row }) {
  return row.report_url ? (
    <a className="inline-flex items-center gap-1 font-semibold text-blue-700 underline underline-offset-2"
      href={row.report_url} target="_blank" rel="noopener noreferrer" aria-label={`${row.name} ${row.year}년 공시 원문`}>
      공시 원문 <ArrowUpRight aria-hidden="true" className="size-3" />
    </a>
  ) : <span className="text-slate-500">—</span>;
}

export function PeerPerformance() {
  const [basis, setBasis] = useState<Basis>('CFS');
  const [start, setStart] = useState(2023);
  const [end, setEnd] = useState(2025);
  const [selected, setSelected] = useState(companies.map(({ code }) => code));
  const [chartMetric, setChartMetric] = useState<Metric>('revenue');
  const [sortMetric, setSortMetric] = useState<Metric>('revenue');
  const [revision, setRevision] = useState(0);
  const [loaded, setLoaded] = useState<{ key: string; value: Comparison }>();
  const [analysis, setAnalysis] = useState<{ key: string; value: Summary }>();
  const [failure, setFailure] = useState('');
  const query = new URLSearchParams({ basis, start: String(start), end: String(end), companies: selected.join(',') }).toString();
  const key = `${query}:${revision}`;
  const data = loaded?.key === key ? loaded.value : undefined;
  const summary = analysis?.key === key ? analysis.value : undefined;
  const failed = failure === key;

  useEffect(() => {
    const abort = new AbortController();
    request(`${endpoint}?${query}`, { signal: abort.signal }).then((value: Comparison) => {
      if (abort.signal.aborted) return;
      setLoaded({ key, value });
      if (!value.summary.comparable_count) return;
      request(`${endpoint}/summary?${query}`, { signal: abort.signal }).then((result: Summary) => {
        if (!abort.signal.aborted) setAnalysis({ key, value: result });
      }).catch(() => { /* The server-calculated summary remains visible. */ });
    }).catch(() => { if (!abort.signal.aborted) setFailure(key); });
    return () => abort.abort();
  }, [key, query]);

  const rows = data?.rows ?? [];
  const visibleCompanies = companies.filter(({ code }) => selected.includes(code));
  const latest = rows.find((row) => row.code === iscCode && row.year === end);
  const hasRevenue = rows.some((row) => row.revenue != null);
  const latestRows = rows.filter((row) => row.year === end).sort((left, right) => {
    const leftValue = left[sortMetric] ?? -Infinity;
    const rightValue = right[sortMetric] ?? -Infinity;
    return leftValue === rightValue ? 0 : rightValue - leftValue;
  });
  const chartIsAmount = chartMetric === 'revenue';
  const hasChartValues = rows.some((row) => row[chartMetric] != null);
  const chart = years.filter((year) => year >= start && year <= end).map((year) => ({
    year,
    ...Object.fromEntries(rows.filter((row) => row.year === year).map((row) => {
      const value = row[chartMetric];
      return [row.code, chartIsAmount && value != null ? value / wonPerUnit : value];
    })),
  }));
  const keyMissing = data?.upstream_issues.includes('key_missing');
  const keyRejected = data?.upstream_issues.includes('key_rejected');
  const temporary = failed || data?.upstream_issues.includes('temporary_error');
  const summaryText = summary?.sentences ?? (data ? Object.values(data.summary.sentences) : []);

  return (
    <section className="space-y-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">동종업체 실적 비교</h1>
          <p className="mt-1 text-sm text-slate-600">반도체 검사용 소켓·접촉 부품 업체의 기업 전체 공시 실적을 비교합니다. 업체별 제품 구성과 사업 범위가 다릅니다.</p>
        </div>
        <a href="?tab=doc-ai" className={`${button} inline-flex items-center gap-2`}><ArrowLeft aria-hidden="true" className="size-4" />AI 업무지원으로 돌아가기</a>
      </header>

      <section className={panel} aria-label="ISC 최신 표시 연도 실적">
        <h2 className="font-semibold">ISC <span className="font-normal text-slate-600">{end}년 · {basisNames[basis]}</span></h2>
        <dl className="mt-3 grid grid-cols-3 gap-2 sm:gap-4">
          {[["매출액", amount(latest?.revenue)], ["매출 증감률", percent(latest?.revenue_growth, true)], ["영업이익률", percent(latest?.operating_margin)]].map(([label, value]) => (
            <div key={label} className="min-w-0 rounded-lg bg-blue-50 px-2 py-2 sm:px-3">
              <dt className="text-xs text-slate-600 sm:text-sm">{label}</dt>
              <dd className="mt-1 break-words font-bold tabular-nums text-blue-950 sm:text-xl">{value}</dd>
            </div>
          ))}
        </dl>
        {latest && filingLabel(latest) && <p className="mt-2 text-sm text-slate-700">{filingLabel(latest)}</p>}
      </section>

      <section aria-labelledby="peer-summary" className="rounded-xl border border-blue-100 bg-blue-50 px-4 py-3">
        <h2 id="peer-summary" className="font-semibold text-blue-950">{summary?.source === 'ai' ? 'AI 요약' : '실적 요약'}</h2>
        {keyMissing || keyRejected || (temporary && !hasRevenue) ? (
          <p className="mt-1 text-sm text-slate-700">공시 데이터를 불러오면 비교 요약을 표시합니다.</p>
        ) : summaryText.length ? (
          <>
            <p className="mt-1 text-sm leading-relaxed text-slate-800">{summaryText.join(' ')}</p>
            {summary?.source !== 'ai' && <p className="mt-1 text-xs text-slate-600">공시 지표로 계산한 요약</p>}
          </>
        ) : <p className="mt-1 text-sm text-slate-600">공시를 조회하는 중입니다…</p>}
      </section>

      <section className={panel} aria-label="실적 조회 조건">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
          <fieldset className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <legend className="mb-1 text-xs font-semibold text-slate-600">재무제표 범위</legend>
            {(Object.keys(basisNames) as Basis[]).map((option) => (
              <button key={option} type="button" aria-pressed={basis === option} onClick={() => setBasis(option)}
                className={`${button} ${basis === option ? 'border-blue-600 bg-blue-50 text-blue-900' : ''}`}>
                {basisNames[option]}({basisScopes[option]})
              </button>
            ))}
          </fieldset>
          <fieldset className="flex min-w-0 items-center gap-2">
            <legend className="mb-1 text-xs font-semibold text-slate-600">사업연도</legend>
            <select aria-label="시작 연도" value={start} onChange={(event) => setStart(Number(event.target.value))} className={button}>
              {years.filter((year) => year <= end).map((year) => <option key={year} value={year}>{year}년</option>)}
            </select>
            <span aria-hidden="true">–</span>
            <select aria-label="종료 연도" value={end} onChange={(event) => setEnd(Number(event.target.value))} className={button}>
              {years.filter((year) => year >= start).map((year) => <option key={year} value={year}>{year}년</option>)}
            </select>
          </fieldset>
        </div>
        <fieldset className="mt-3 flex min-w-0 flex-wrap gap-2">
          <legend className="mb-1 text-xs font-semibold text-slate-600">비교업체</legend>
          <span className="rounded-lg bg-blue-50 px-3 py-2 text-sm font-semibold text-blue-900">ISC · 기준 회사</span>
          {companies.slice(1).map(({ code, name, color }) => (
            <label key={code} className="flex cursor-pointer items-center gap-2 rounded-lg border border-slate-200 px-3 py-2 text-sm">
              <input type="checkbox" checked={selected.includes(code)} className="accent-blue-700" onChange={(event) => {
                const checked = event.target.checked;
                setSelected((current) => companies.filter((company) => company.code === code ? checked : current.includes(company.code)).map((company) => company.code));
              }} />
              <span aria-hidden="true" className="size-2 rounded-full" style={{ backgroundColor: color }} />{name}
            </label>
          ))}
        </fieldset>
        <p className="mt-3 text-xs text-slate-600">{data?.last_fetched_at ? `마지막 OpenDART 조회 ${dateFormat.format(new Date(data.last_fetched_at))}` : 'OpenDART 조회 시각 —'}</p>
        {keyMissing && <p role="status" className="mt-2 text-sm text-amber-900">OpenDART API 키가 서버에 설정되지 않았습니다.</p>}
        {keyRejected && <p role="status" className="mt-2 text-sm text-amber-900">OpenDART에서 요청을 허용하지 않았습니다. 서버의 API 키 상태와 접근 설정을 확인해 주세요.</p>}
        {temporary && <div role="status" className="mt-2 flex flex-wrap items-center gap-2 text-sm text-amber-900">
          <span>일시적 조회 오류가 발생했습니다.</span><button type="button" className={button} onClick={() => setRevision((value) => value + 1)}>다시 조회</button>
        </div>}
        {!data && !failed && <p role="status" className="mt-2 text-sm text-slate-600">기업 공시를 조회하는 중입니다…</p>}
      </section>

      <section className={panel} aria-labelledby="peer-comparison">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="peer-comparison" className="font-semibold">
            {end}년 실적 비교 <span className="text-sm font-normal text-slate-600">· {basisNames[basis]}</span>
          </h2>
          <label className="flex items-center gap-2 text-sm text-slate-700">
            정렬
            <select className={button} value={sortMetric} onChange={(event) => setSortMetric(event.target.value as Metric)}>
              {metrics.map((metric) => <option key={metric} value={metric}>{metricNames[metric]} 높은 순</option>)}
            </select>
          </label>
        </div>
        {latestRows.length > 0 && <LatestPerformanceTable rows={latestRows} sortMetric={sortMetric} />}
      </section>

      <section className={panel} aria-labelledby="peer-trend">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="peer-trend" className="font-semibold">
            {metricNames[chartMetric]} 추이 <span className="text-xs font-normal text-slate-600">{chartIsAmount ? '억 원' : '%'}</span>
          </h2>
          <div role="group" aria-label="차트 지표" className="flex flex-wrap gap-1">
            {metrics.map((metric) => (
              <button key={metric} type="button" aria-pressed={chartMetric === metric} onClick={() => setChartMetric(metric)}
                className={`${button} ${chartMetric === metric ? 'border-blue-600 bg-blue-50 text-blue-900' : ''}`}>{metricNames[metric]}</button>
            ))}
          </div>
        </div>
        {hasChartValues ? (
          <div className="mt-3 h-64 min-w-0 sm:h-80">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={chart} margin={{ top: 8, right: 14, left: 0, bottom: 0 }}>
                <CartesianGrid stroke="#e2e8f0" vertical={false} />
                <XAxis dataKey="year" tick={{ fontSize: 12 }} />
                <YAxis width={56} tick={{ fontSize: 11 }}
                  tickFormatter={(value) => chartIsAmount ? amountFormat.format(value) : `${amountFormat.format(value)}%`} />
                <Tooltip formatter={(value) => chartIsAmount ? amount(Number(value) * wonPerUnit) : percent(Number(value), chartMetric === 'revenue_growth')}
                  labelFormatter={(year) => `${year}년`} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                {visibleCompanies.map(({ code, name, color, dash }) => (
                  <Line key={code} type="linear" dataKey={code} name={name} stroke={color} strokeDasharray={dash}
                    strokeWidth={code === iscCode ? 4 : 2} dot={{ r: code === iscCode ? 4 : 3 }} connectNulls={false} isAnimationActive={false} />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : <p className="mt-2 text-sm text-slate-600">{data || failed ? '표시할 데이터가 없습니다.' : '공시 데이터를 불러오는 중입니다…'}</p>}
      </section>

      {start < end && rows.length > 0 && (
        <section className={panel} aria-labelledby="peer-history">
          <h2 id="peer-history" className="font-semibold">기업별 공시 내역</h2>
          <PerformanceHistory rows={rows} />
        </section>
      )}
    </section>
  );
}

function LatestPerformanceTable({ rows, sortMetric }: { rows: Row[]; sortMetric: Metric }) {
  return (
    <div className="mt-3 overflow-x-auto focus-visible:outline-blue-700" tabIndex={0} role="region" aria-label="비교표 스크롤">
      <table aria-labelledby="peer-comparison" className="w-full whitespace-nowrap text-left text-sm">
        <thead className="bg-slate-50 text-xs text-slate-700"><tr>
          <th scope="col" className="px-2 py-2">회사명</th>
          {[metricNames.revenue, metricNames.revenue_growth, '영업이익', metricNames.operating_margin].map((label) => (
            <th key={label} scope="col" aria-sort={label === metricNames[sortMetric] ? 'descending' : undefined}
              className="px-2 py-2 text-right">{label}</th>
          ))}
          <th scope="col" className="px-2 py-2">공시 원문</th>
        </tr></thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((row) => (
            <tr key={row.code} className={row.code === iscCode ? 'bg-blue-50/60' : undefined}>
              <th scope="row" className="px-2 py-2 font-semibold">
                {row.name}
                {filingLabel(row) && <span className="block whitespace-normal text-xs font-normal text-slate-600">{filingLabel(row)}</span>}
              </th>
              {performanceFields(row).map(([label, value]) => <td key={label} className="px-2 py-2 text-right tabular-nums">{value}</td>)}
              <td className="px-2 py-2"><ReportLink row={row} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PerformanceHistory({ rows }: { rows: Row[] }) {
  return (
    <div className="mt-3 space-y-2">
      {companies.map(({ code, name }) => {
        const filings = rows.filter((row) => row.code === code).reverse();
        if (!filings.length) return null;
        return (
          <details key={code} className="rounded-lg border border-slate-200 px-3 py-2">
            <summary className="cursor-pointer rounded font-semibold focus-visible:outline-blue-700">{name} <span className="text-xs font-normal text-slate-600">{code}</span></summary>
            <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {filings.map((row) => (
                <section key={row.year} className="space-y-1 rounded-lg bg-slate-50 p-2 text-sm">
                  <div className="flex flex-wrap items-center justify-between gap-x-2">
                    <h3 className="font-semibold">{row.year}년 <span className="font-normal text-slate-600">· {basisNames[row.basis]}</span></h3>
                    <ReportLink row={row} />
                  </div>
                  {filingLabel(row) && <p className="text-xs text-slate-600">{filingLabel(row)}</p>}
                  <dl className="grid grid-cols-2 gap-x-2 gap-y-1">
                    {performanceFields(row).map(([label, value]) => <div key={label}><dt className="text-xs text-slate-600">{label}</dt><dd className="font-medium tabular-nums">{value}</dd></div>)}
                  </dl>
                </section>
              ))}
            </div>
          </details>
        );
      })}
    </div>
  );
}
