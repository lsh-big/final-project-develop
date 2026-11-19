import { useEffect, useState } from 'react';
import { request } from '../services/api.js';

const amounts = { supply_amount: '공급가액', tax_amount: '부가세', total_amount: '합계금액' } as const;
type AmountKey = keyof typeof amounts;
type EvidenceFields = Partial<Record<AmountKey, number | null>> & {
  vendor?: string;
  transaction_date?: string;
};
type Kind = 'document' | 'statement';
type Evidence = { id: string; filename: string; kind: Kind; fields: EvidenceFields };
type Pair = {
  record: Evidence;
  comparison: {
    amounts: Record<AmountKey, { invoice: number | null; statement: number | null; difference: number | null }>;
    tax_unclear: boolean;
    required_missing: boolean;
    amount_review: 'check' | 'matched';
  };
};
type Candidate = Pair & { reasons: string[]; confidence: 'high' | 'low' };
export const matchStatusLabels = { linked: '연결 완료', check: '확인 필요', unlinked: '미연결' };
type Matching = {
  status: keyof typeof matchStatusLabels;
  linked: Pair | null;
  candidates: Candidate[];
};

const won = new Intl.NumberFormat('ko-KR');
function money(value: number | null | undefined) {
  return value == null ? '확인 필요' : `${won.format(value)}원`;
}

type Props = {
  path: string;
  kind: Kind;
  fields: EvidenceFields;
  revision: number;
  disabled: boolean;
  primaryClass: string;
  secondaryClass: string;
  onChange: () => void;
};

export function EvidenceComparison({ path, kind, fields, revision, disabled, primaryClass, secondaryClass, onChange }: Props) {
  const [matching, setMatching] = useState<Matching>();
  const [selectedId, setSelectedId] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    void request(path, { signal: abort.signal })
      .then((latest: Matching) => { if (!abort.signal.aborted) setMatching(latest); })
      .catch((problem: Error) => { if (!abort.signal.aborted) setError(problem.message); });
    return () => abort.abort();
  }, [path, revision, refresh]);

  const candidate = matching?.candidates.find(({ record }) => record.id === selectedId) ?? matching?.candidates[0];
  const pair = matching?.linked ?? candidate;
  const currentIsInvoice = kind === 'document';
  const invoice = currentIsInvoice ? fields : pair?.record.fields;
  const statement = currentIsInvoice ? pair?.record.fields : fields;
  const review = pair?.comparison;

  async function changeLink(remove: boolean) {
    if (busy || disabled || (!remove && !candidate)) return;
    setBusy(true);
    setError('');
    try {
      const updated: Matching | null = await request(path, remove ? { method: 'DELETE' } : {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ candidate_id: candidate?.record.id }),
      });
      if (updated) setMatching(updated);
      else setMatching(undefined);
      setRefresh((current) => current + 1);
      onChange();
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : '증빙 연결을 변경하지 못했습니다.');
      setRefresh((current) => current + 1);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-5 space-y-3 rounded-lg border border-slate-200 p-4 text-sm" aria-label="증빙 대조">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="font-semibold">증빙 대조</h3>
        <button type="button" className={secondaryClass} disabled={disabled || busy} onClick={() => {
          setError('');
          setRefresh((current) => current + 1);
        }}>후보 새로고침</button>
      </div>
      {error && <p className="text-red-800" role="alert">{error}</p>}
      {!matching && !error && <p role="status">증빙 대조 정보를 불러오는 중입니다…</p>}
      {matching && (
        <p className="text-slate-700" role="status">
          연결 상태 · {matching.linked ? '연결 완료' : '미연결'}　금액 검토 · {review?.amount_review === 'matched' ? '일치' : '확인 필요'}
        </p>
      )}
      {matching?.linked && <p className="break-all font-medium">연결된 증빙 · {matching.linked.record.filename}</p>}
      {!!matching?.candidates.length && (
        <>
          <label className="block space-y-1">
            <span className="font-medium">연결 후보 ({matching.candidates.length}건)</span>
            <select className="block w-full rounded-lg border border-slate-300 bg-white p-2"
              value={candidate?.record.id ?? ''} disabled={disabled || busy} onChange={(event) => setSelectedId(event.target.value)}>
              {matching.candidates.map(({ record }) => (
                <option key={record.id} value={record.id}>{record.filename} · {record.fields.vendor || '거래처 확인 필요'}</option>
              ))}
            </select>
          </label>
          {candidate && <p className="text-slate-700">{candidate.confidence === 'low' && '낮은 신뢰도 · '}{candidate.reasons.join(' · ')}</p>}
        </>
      )}
      {matching && !pair && <p className="text-slate-600">연결할 증빙 후보가 없습니다. 거래처, 사업자등록번호, 날짜를 확인해 주세요.</p>}
      <div className="overflow-x-auto">
        <table className="w-full min-w-[420px] text-left">
          <thead className="bg-slate-50">
            <tr>{['항목', '매입 세금계산서', '거래명세서', '차액'].map((label) => <th key={label} scope="col" className="p-2">{label}</th>)}</tr>
          </thead>
          <tbody>
            {([['거래처', 'vendor'], ['날짜', 'transaction_date']] as const).map(([label, key]) => (
              <tr key={key} className="border-t border-slate-100">
                <th scope="row" className="p-2 font-medium">{label}</th>
                <td className="p-2">{invoice?.[key] || '확인 필요'}</td>
                <td className="p-2">{statement?.[key] || '확인 필요'}</td>
                <td className="p-2">—</td>
              </tr>
            ))}
            {(Object.keys(amounts) as AmountKey[]).map((key) => {
              const values = review?.amounts[key];
              const difference = values?.difference;
              return (
                <tr key={key} className="border-t border-slate-100">
                  <th scope="row" className="p-2 font-medium">{amounts[key]}</th>
                  <td className="p-2 tabular-nums">{money(values ? values.invoice : invoice?.[key])}</td>
                  <td className="p-2 tabular-nums">{money(values ? values.statement : statement?.[key])}</td>
                  <td className={`p-2 tabular-nums ${difference !== 0 ? 'font-semibold text-amber-900' : ''}`}>
                    {difference == null ? '확인 필요' : `${difference > 0 ? '+' : ''}${money(difference)}`}
                    {key === 'total_amount' && review?.tax_unclear && difference != null && ' · 확인 필요'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="text-slate-600">차액 기준은 세금계산서 금액에서 거래명세서 금액을 뺀 값입니다.</p>
      {review?.tax_unclear && <p className="text-amber-900">거래명세서의 부가세 포함 여부와 합계금액을 확인해 주세요.</p>}
      {review?.required_missing && <p className="text-amber-900">거래처, 사업자등록번호 또는 날짜에 누락된 값이 있습니다.</p>}
      {matching?.linked ? (
        <button type="button" className={secondaryClass} disabled={disabled || busy} onClick={() => void changeLink(true)}>연결 해제</button>
      ) : candidate && (
        <button type="button" className={primaryClass} disabled={disabled || busy} onClick={() => void changeLink(false)}>연결 확정</button>
      )}
    </section>
  );
}
