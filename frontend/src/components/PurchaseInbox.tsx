import { useState, useEffect } from 'react';
import { request } from '../services/api.js';

const endpoint = '/api/v1/purchase-inbox';
const button = 'rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-blue-800 hover:bg-slate-50 disabled:opacity-50';
const control = 'w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm';
const statusLabels = {
  pending_invoice: '세금계산서 대기',
  candidate: '연결 후보 확인',
  difference: '금액 차이 확인',
  complete: '대조 완료',
  check: '확인 필요',
};

type InboxRow = {
  id: string;
  kind: 'document' | 'statement';
  record_status: 'review' | 'approved';
  vendor: string;
  evidence_date: string | null;
  registered_date: string;
  invoice_amount: number | null;
  statement_amount: number | null;
  difference: number | null;
  status: keyof typeof statusLabels;
  can_write_memo: boolean;
};
type InboxResponse = { start: string; end: string; rows: InboxRow[]; can_read_memos: boolean };
type Memo = { id: string; author: string; text: string; created_at: string };
const initialFilters = { start: '', end: '', vendor: '', status: '' };
const money = new Intl.NumberFormat('ko-KR');
const memoTime = new Intl.DateTimeFormat('ko-KR', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Seoul' });

function displayAmount(value: number | null, signed = false) {
  return value === null ? '확인 필요' : `${signed && value > 0 ? '+' : ''}${money.format(value)}원`;
}

function problemMessage(problem: unknown) {
  return problem instanceof Error ? problem.message : '요청을 처리하지 못했습니다.';
}

export function PurchaseInbox({ revision }: { revision: number }) {
  const [filters, setFilters] = useState(initialFilters);
  const [query, setQuery] = useState('');
  const [result, setResult] = useState<InboxResponse>();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [memoId, setMemoId] = useState('');

  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError('');
    void request(`${endpoint}?${query}`, { signal: abort.signal })
      .then((data: InboxResponse) => {
        if (abort.signal.aborted) return;
        setResult(data);
        setFilters((current) => ({ ...current, start: current.start || data.start, end: current.end || data.end }));
      })
      .catch((problem: unknown) => {
        if (abort.signal.aborted) return;
        setResult(undefined);
        setError(problemMessage(problem));
      })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [query, revision]);

  const memoRow = result?.can_read_memos ? result.rows.find((row) => row.id === memoId) : undefined;
  return (
    <section className="space-y-4 rounded-xl border border-slate-200 bg-white p-5">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold">미처리 증빙함</h1>
          <p className="mt-1 text-sm text-slate-600">증빙일은 세금계산서 작성일 또는 거래명세서 납품일입니다. 연결된 건은 작성일을, 증빙일이 없으면 등록일을 사용합니다.</p>
        </div>
        <a className={button} href="?tab=doc-ai#purchase-evidence">구매 증빙 처리</a>
      </header>
      <form className="grid items-end gap-3 sm:grid-cols-2 lg:grid-cols-[1fr_1fr_1fr_1fr_auto]"
        onSubmit={(event) => {
          event.preventDefault();
          setMemoId('');
          setQuery(new URLSearchParams(Object.entries(filters).filter(([, value]) => value.trim())).toString());
        }}>
        <label className="space-y-1 text-sm"><span>시작일</span>
          <input className={control} type="date" value={filters.start} max={filters.end || undefined}
            onChange={(event) => setFilters({ ...filters, start: event.target.value })} />
        </label>
        <label className="space-y-1 text-sm"><span>종료일</span>
          <input className={control} type="date" value={filters.end} min={filters.start || undefined}
            onChange={(event) => setFilters({ ...filters, end: event.target.value })} />
        </label>
        <label className="space-y-1 text-sm"><span>거래처</span>
          <input className={control} type="search" maxLength={200} value={filters.vendor}
            onChange={(event) => setFilters({ ...filters, vendor: event.target.value })} />
        </label>
        <label className="space-y-1 text-sm"><span>상태</span>
          <select className={control} value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
            <option value="">미처리 전체</option>
            {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <button type="submit" className={button} disabled={loading}>조회</button>
      </form>
      {loading && <p className="text-sm text-slate-600" role="status">미처리 증빙을 불러오는 중입니다…</p>}
      {error && <p className="text-sm text-red-800" role="alert">{error}</p>}
      {result?.can_read_memos === false && <p className="text-sm text-slate-600">로컬 읽기 전용입니다. 확인 메모에는 사용자 인증이 필요합니다.</p>}
      {!loading && result?.rows.length === 0 && <p className="text-sm text-slate-600">조회 조건에 맞는 증빙이 없습니다.</p>}
      {!loading && !!result?.rows.length && (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[860px] text-left text-sm">
            <thead className="bg-slate-50">
              <tr>{['거래처', '증빙일', '세금계산서 금액', '거래명세서 합계', '차액', '상태', '작업'].map((label) =>
                <th key={label} className="p-2" scope="col">{label}</th>)}</tr>
            </thead>
            <tbody>
              {result.rows.map((row) => {
                const detail = new URLSearchParams({ view: 'purchase-history', status: row.record_status, kind: row.kind, record: row.id });
                return (
                  <tr key={row.id} className="border-t border-slate-100">
                    <td className="p-2">{row.vendor || '확인 필요'}</td>
                    <td className="p-2">{row.evidence_date || `확인 필요 · 등록 ${row.registered_date}`}</td>
                    <td className="p-2 tabular-nums">{displayAmount(row.invoice_amount)}</td>
                    <td className="p-2 tabular-nums">{displayAmount(row.statement_amount)}</td>
                    <td className="p-2 tabular-nums">{displayAmount(row.difference, true)}</td>
                    <td className="p-2">{statusLabels[row.status]}</td>
                    <td className="p-2">
                      <div className="flex flex-wrap gap-2">
                        <a className={button} href={`?${detail}`}>증빙 확인</a>
                        {result.can_read_memos && <button className={button} type="button" aria-expanded={memoId === row.id}
                          onClick={() => setMemoId(memoId === row.id ? '' : row.id)}>확인 메모</button>}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {memoRow && <InboxMemos key={memoRow.id} row={memoRow} />}
    </section>
  );
}

function InboxMemos({ row }: { row: InboxRow }) {
  const path = `${endpoint}/${row.id}/memos`;
  const [notes, setNotes] = useState<Memo[]>();
  const [text, setText] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const abort = new AbortController();
    void request(path, { signal: abort.signal })
      .then((data: Memo[]) => { if (!abort.signal.aborted) setNotes(data); })
      .catch((problem: unknown) => { if (!abort.signal.aborted) setError(problemMessage(problem)); });
    return () => abort.abort();
  }, [path]);

  async function save() {
    if (saving || !notes || !row.can_write_memo || !text.trim()) return;
    setSaving(true);
    setError('');
    try {
      const memo: Memo = await request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }) });
      setNotes((current) => [...(current ?? []), memo]);
      setText('');
    } catch (problem) {
      setError(problemMessage(problem));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="space-y-3 rounded-lg border border-slate-200 p-4">
      <h2 className="font-semibold">확인 메모 · {row.vendor || '거래처 확인 필요'}</h2>
      {error && <p className="text-sm text-red-800" role="alert">{error}</p>}
      {!error && !notes && <p className="text-sm" role="status">메모를 불러오는 중입니다…</p>}
      {notes && (
        notes.length ? <ul className="divide-y divide-slate-200">{notes.map((note) =>
          <li key={note.id} className="space-y-1 py-2">
            <p className="text-sm font-medium">{note.author} · <time dateTime={note.created_at}>{memoTime.format(new Date(note.created_at))}</time></p>
            <p className="whitespace-pre-wrap break-words text-sm">{note.text}</p>
          </li>)}</ul> : <p className="text-sm text-slate-600">작성된 메모가 없습니다.</p>
      )}
      {row.can_write_memo && notes && (
        <form className="space-y-2" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <label className="block space-y-1 text-sm"><span>메모 내용</span>
            <textarea className={control} maxLength={1000} rows={3} value={text} onChange={(event) => setText(event.target.value)} required />
          </label>
          <button type="submit" className={button} disabled={saving || !text.trim()}>메모 저장</button>
        </form>
      )}
    </section>
  );
}
