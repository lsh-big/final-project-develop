import { useEffect, useRef, useState } from 'react';
import { request } from '../services/api.js';

const api = '/api/v1/purchase-prices';
const button = 'rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-blue-800 hover:bg-slate-50 disabled:opacity-50';
const input = 'w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm';
const amountFormat = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 4 });
const percentFormat = new Intl.NumberFormat('ko-KR', { maximumFractionDigits: 2 });
const defaultFilters = { start: '', end: '', vendor: '', item: '' };

type Item = {
  record_id: string;
  filename: string;
  line: number;
  item_name: string;
  item_code: string;
  vendor: string;
  unit: string;
  tax_basis: string | null;
  purchase_date: string | null;
  unit_price: string | null;
};
type Change = Item & {
  previous_unit_price: string | null;
  difference: string | null;
  rate: string | null;
  direction: 'rise' | 'fall' | 'unchanged' | 'insufficient';
};
type PriceList = {
  start: string;
  end: string;
  recent_start: string;
  recent_end: string;
  summary: { rise: number; fall: number; insufficient: number };
  rows: Change[];
};
type PriceDetail = { current: Item; previous: Item | null; history: Item[] };

function itemKey(item: Item) {
  return `${item.record_id}/${item.line}`;
}

function unitPrice(value: string | null) {
  return value === null ? '확인 필요' : `${amountFormat.format(Number(value))}원`;
}

function rateText(value: string | null) {
  if (value === null || !Number.isFinite(Number(value))) return '—';
  const rate = Number(value);
  return `${rate > 0 ? '+' : ''}${percentFormat.format(rate)}%`;
}

const color = {
  rise: 'text-red-800',
  fall: 'text-blue-800',
  unchanged: 'text-slate-700',
  insufficient: 'text-slate-600',
};

function changeText(row: Change) {
  if (row.difference === null) return '비교 자료 부족';
  const difference = Number(row.difference);
  const change = difference > 0 ? `▲ ${unitPrice(row.difference)}` : difference < 0
    ? `▼ ${unitPrice(row.difference)}` : '0원';
  return `${change} (${rateText(row.rate)})`;
}

function statementLink(item: Item) {
  return `?${new URLSearchParams({ view: 'purchase-history', status: 'approved', kind: 'statement', record: item.record_id })}`;
}

function errorText(problem: unknown) {
  return problem instanceof Error ? problem.message : '매입 단가 이력을 불러오지 못했습니다.';
}

export function PurchasePriceHistory({ revision }: { revision: number }) {
  const [filters, setFilters] = useState(defaultFilters);
  const [risesOnly, setRisesOnly] = useState(false);
  const [query, setQuery] = useState('');
  const [data, setData] = useState<PriceList>();
  const [selected, setSelected] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError('');
    void request(`${api}?${query}`, { signal: abort.signal })
      .then((result: PriceList) => {
        if (abort.signal.aborted) return;
        setData(result);
        setFilters((current) => ({ ...current, start: current.start || result.start, end: current.end || result.end }));
      })
      .catch((problem: unknown) => {
        if (abort.signal.aborted) return;
        setData(undefined);
        setError(errorText(problem));
      })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [query, revision]);

  const active = data?.rows.find((row) => itemKey(row) === selected);
  const select = (row: Change) => setSelected((current) => current === itemKey(row) ? '' : itemKey(row));
  return (
    <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">매입 단가 이력</h1>
          <p className="mt-1 text-sm text-slate-600">승인된 거래명세서를 납품일 기준으로 비교합니다. 납품일이 없는 기록은 등록일로 조회하며 비교 자료 부족으로 표시합니다.</p>
        </div>
        <a className={button} href="?tab=doc-ai#purchase-evidence">구매 증빙 처리</a>
      </header>
      {data && (
        <section aria-label="최근 30일 단가 변동" className="space-y-2">
          <h2 className="text-sm font-semibold">최근 30일 전체 · {data.recent_start} ~ {data.recent_end}</h2>
          <dl className="grid grid-cols-3 gap-2 sm:gap-3">
            {([['rise', '▲ 인상'], ['fall', '▼ 인하'], ['insufficient', '비교 자료 부족']] as const).map(([key, label]) => (
              <div key={key} className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                <dt className={`text-xs font-semibold sm:text-sm ${color[key]}`}>{label}</dt>
                <dd className="mt-1 text-lg font-bold tabular-nums">{amountFormat.format(data.summary[key])}건</dd>
              </div>
            ))}
          </dl>
        </section>
      )}
      <form className="grid items-end gap-3 sm:grid-cols-2 xl:grid-cols-[1fr_1fr_1fr_1fr_auto_auto]"
        onSubmit={(event) => {
          event.preventDefault();
          const params = new URLSearchParams(Object.entries(filters).filter(([, value]) => value.trim()));
          if (risesOnly) params.set('rises_only', 'true');
          setSelected('');
          setQuery(params.toString());
        }}>
        {([['start', '시작일'], ['end', '종료일'], ['vendor', '거래처'], ['item', '품목명 또는 코드']] as const).map(([key, label]) => (
          <label key={key} className="space-y-1 text-sm font-medium"><span>{label}</span>
            <input className={input} type={key === 'start' || key === 'end' ? 'date' : 'search'}
              max={key === 'start' ? filters.end || undefined : undefined}
              min={key === 'end' ? filters.start || undefined : undefined}
              maxLength={key === 'vendor' || key === 'item' ? 200 : undefined}
              value={filters[key]} onChange={(event) => setFilters({ ...filters, [key]: event.target.value })} />
          </label>
        ))}
        <label className="flex items-center gap-2 self-center text-sm font-medium xl:self-end xl:pb-2.5">
          <input type="checkbox" checked={risesOnly} onChange={(event) => setRisesOnly(event.target.checked)} />인상만 보기
        </label>
        <button type="submit" className={button} disabled={loading}>조회</button>
      </form>
      {loading && <p className="text-sm text-slate-600" role="status">매입 단가 이력을 불러오는 중입니다…</p>}
      {error && <p className="text-sm text-red-800" role="alert">{error}</p>}
      {!loading && data?.rows.length === 0 && <p className="text-sm text-slate-600">조회 조건에 맞는 매입 품목이 없습니다.</p>}
      {!loading && !!data?.rows.length && (
        <>
          <div className="hidden overflow-x-auto md:block">
            <table className="w-full min-w-[680px] text-left text-sm">
              <thead className="bg-slate-50">
                <tr>{['품목', '거래처', '직전 단가', '이번 단가', '변동 금액 및 변동률', '최근 매입일'].map((label) =>
                  <th key={label} scope="col" className="p-2">{label}</th>)}</tr>
              </thead>
              <tbody>{data.rows.map((row) => (
                <tr key={itemKey(row)} className="cursor-pointer border-t border-slate-100 hover:bg-slate-50" onClick={() => select(row)}>
                  <td className="p-2"><button type="button" className="text-left font-semibold text-blue-800 underline"
                    aria-expanded={selected === itemKey(row)} onClick={(event) => { event.stopPropagation(); select(row); }}>
                    {row.item_name || row.item_code || '품목 확인 필요'}
                  </button></td>
                  <td className="p-2">{row.vendor || '확인 필요'}</td>
                  <td className="p-2 tabular-nums">{unitPrice(row.previous_unit_price)}</td>
                  <td className="p-2 tabular-nums">{unitPrice(row.unit_price)}</td>
                  <td className={`whitespace-nowrap p-2 font-semibold tabular-nums ${color[row.direction]}`}>{changeText(row)}</td>
                  <td className="p-2">{row.purchase_date || '확인 필요'}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
          <ul className="space-y-2 md:hidden">{data.rows.map((row) => (
            <li key={itemKey(row)}>
              <button type="button" className="w-full space-y-1 rounded-lg border border-slate-200 p-3 text-left focus-visible:outline-blue-700"
                aria-expanded={selected === itemKey(row)} onClick={() => select(row)}>
                <span className="block font-semibold">{row.item_name || row.item_code || '품목 확인 필요'}</span>
                <span className="block text-sm text-slate-600">{row.vendor || '거래처 확인 필요'} · {row.purchase_date || '매입일 확인 필요'}</span>
                <span className="block text-sm tabular-nums">{unitPrice(row.previous_unit_price)} → {unitPrice(row.unit_price)}</span>
                <span className={`block text-sm font-semibold tabular-nums ${color[row.direction]}`}>{changeText(row)}</span>
              </button>
            </li>
          ))}</ul>
        </>
      )}
      {active && <PurchaseItemDetail key={selected} row={active} revision={revision} />}
    </section>
  );
}

function PurchaseItemDetail({ row, revision }: { row: Change; revision: number }) {
  const path = `${api}/${itemKey(row)}`;
  const [detail, setDetail] = useState<PriceDetail>();
  const [error, setError] = useState('');
  const section = useRef<HTMLElement>(null);
  useEffect(() => { section.current?.scrollIntoView({ block: 'nearest' }); }, []);
  useEffect(() => {
    const abort = new AbortController();
    setError('');
    void request(path, { signal: abort.signal })
      .then((result: PriceDetail) => { if (!abort.signal.aborted) setDetail(result); })
      .catch((problem: unknown) => {
        if (!abort.signal.aborted) {
          setDetail(undefined);
          setError(errorText(problem));
        }
      });
    return () => abort.abort();
  }, [path, revision]);

  return (
    <section className="space-y-3 rounded-lg border border-slate-200 p-4 text-sm" ref={section}>
      <h2 className="font-semibold">최근 매입 이력 · {row.item_name || row.item_code || '품목 확인 필요'}</h2>
      {error && <p className="text-red-800" role="alert">{error}</p>}
      {!error && !detail && <p role="status">품목 이력을 불러오는 중입니다…</p>}
      {detail && (
        <>
          <div className="flex flex-wrap gap-2">
            {detail.previous && <a className={button} href={statementLink(detail.previous)}>직전 거래명세서</a>}
            <a className={button} href={statementLink(detail.current)}>이번 거래명세서</a>
          </div>
          {row.direction === 'insufficient' && <p className="text-slate-700">거래처, 품목, 단위와 부가세 기준이 같고 직전 매입일과 단가가 확인된 품목을 비교할 수 있습니다.</p>}
          <ul className="divide-y divide-slate-100">
            {detail.history.map((item) => (
              <li key={itemKey(item)} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
                <span>{item.purchase_date || '매입일 확인 필요'}</span>
                <span className="font-semibold tabular-nums">{unitPrice(item.unit_price)}{item.unit && ` / ${item.unit}`}</span>
                <a className="break-all text-blue-800 underline" href={statementLink(item)}>{item.filename}</a>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}
