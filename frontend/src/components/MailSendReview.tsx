import { useEffect, useRef, useState } from 'react';
import { apiFetch, request } from '../services/api.js';

export type ReplyEnvelope = { sender: string; recipient: string; subject: string };
type Reply = ReplyEnvelope & { message_id: string; text: string };
type SendRecord = Reply & { request_id: string; status: 'simulated'; actor: string; at: string };

const button = 'rounded-lg border border-slate-300 bg-white px-3 py-2 font-semibold text-blue-800 hover:bg-slate-50 disabled:opacity-50';
const date = new Intl.DateTimeFormat('ko-KR', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Seoul' });

export function MailSendReview({ path, reply }: { path: string; reply?: Reply }) {
  const [review, setReview] = useState<Reply & { request_id: string }>();
  const [records, setRecords] = useState<SendRecord[]>([]);
  const [working, setWorking] = useState(false);
  const [uncertain, setUncertain] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [historyError, setHistoryError] = useState(false);
  const inFlight = useRef(false);
  const confirmation = useRef<HTMLElement>(null);

  useEffect(() => {
    const abort = new AbortController();
    request(path, { signal: abort.signal }).then((rows: SendRecord[]) => {
      if (abort.signal.aborted) return;
      setRecords((current) => [...current, ...rows.filter((row) => !current.some((item) => item.request_id === row.request_id))]);
    }).catch(() => { if (!abort.signal.aborted) setHistoryError(true); });
    return () => abort.abort();
  }, [path]);

  useEffect(() => {
    if (review) confirmation.current?.focus();
  }, [review]);

  function complete(receipt: SendRecord) {
    setRecords((current) => [receipt, ...current.filter((row) => row.request_id !== receipt.request_id)]);
    setReview(undefined);
    setUncertain(false);
    setNotice('시연 발송을 기록했습니다.');
  }

  async function send() {
    if (!review || inFlight.current || uncertain) return;
    inFlight.current = true;
    setWorking(true);
    setError('');
    try {
      const { request_id, message_id, text } = review;
      const response = await apiFetch(path, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id, message_id, text }), signal: AbortSignal.timeout(12000),
      });
      if (response.status >= 400 && response.status < 500) {
        const body = await response.json().catch(() => null);
        setError(typeof body?.detail === 'string' ? body.detail : '발송 요청이 거부되었습니다. 내용을 확인해 주세요.');
        return;
      }
      if (!response.ok) throw new Error('Send result unavailable');
      complete(await response.json());
    } catch {
      setUncertain(true);
      setError('발송 결과를 확인할 수 없습니다. 발송 기록을 조회해 주세요.');
    } finally {
      inFlight.current = false;
      setWorking(false);
    }
  }

  async function checkResult() {
    if (!review || inFlight.current) return;
    inFlight.current = true;
    setWorking(true);
    setError('');
    try {
      const rows: SendRecord[] = await request(path, { signal: AbortSignal.timeout(12000) });
      const receipt = rows.find((row) => row.request_id === review.request_id);
      setRecords(rows);
      setHistoryError(false);
      if (receipt) complete(receipt);
      else {
        setUncertain(false);
        setError('이 요청의 발송 기록이 없습니다. 확인 화면에서 다시 보낼 수 있습니다.');
      }
    } catch {
      setError('발송 기록을 조회하지 못했습니다. 잠시 후 다시 조회해 주세요.');
    } finally {
      inFlight.current = false;
      setWorking(false);
    }
  }

  return (
    <div className="space-y-3">
      {reply && !review && (
        <button type="button" className={button} onClick={() => {
          setNotice('');
          setError('');
          setReview({ ...reply, request_id: crypto.randomUUID() });
        }}>메일 보내기</button>
      )}
      {review && (
        <section tabIndex={-1} ref={confirmation} aria-label="메일 발송 확인"
          className="space-y-3 rounded-lg border border-blue-200 bg-blue-50 p-3">
          <h4 className="font-semibold text-blue-950">메일 발송 확인</h4>
          <p className="text-slate-700">시연 발송은 AutoFlow에 기록됩니다. 실제 회사 메일을 보내려면 회사 계정 연결과 발송 권한이 필요합니다.</p>
          <dl className="space-y-2 break-words">
            <div><dt className="font-semibold">발신 계정</dt><dd>{review.sender}</dd></div>
            <div><dt className="font-semibold">수신자</dt><dd>{review.recipient}</dd></div>
            <div><dt className="font-semibold">제목</dt><dd>{review.subject}</dd></div>
            <div><dt className="font-semibold">본문</dt><dd className="max-h-64 overflow-y-auto whitespace-pre-wrap rounded bg-white p-2">{review.text}</dd></div>
          </dl>
          <div className="flex flex-wrap gap-2">
            {uncertain ? (
              <button type="button" className={button} disabled={working} onClick={() => void checkResult()}>발송 기록 확인</button>
            ) : (
              <button type="button" className={button} disabled={working} onClick={() => void send()}>시연 보내기</button>
            )}
            <button type="button" className={button} disabled={working || uncertain} onClick={() => {
              setReview(undefined);
              setError('');
            }}>편집으로 돌아가기</button>
          </div>
        </section>
      )}
      {error && <p role="alert" className="text-red-800">{error}</p>}
      {notice && <p role="status" className="text-emerald-800">{notice}</p>}
      {historyError && <p className="text-red-800">이전 시연 발송 기록을 불러오지 못했습니다.</p>}
      {records.length > 0 && (
        <details className="rounded-md border border-slate-200 p-2">
          <summary className="cursor-pointer font-semibold">시연 발송 기록 · {records.length}건</summary>
          <ul className="max-h-64 divide-y divide-slate-100 overflow-y-auto break-words">
            {records.map((record) => (
              <li key={record.request_id} className="space-y-1 py-2">
                <p className="font-semibold">{record.subject}</p>
                <p>{record.recipient}</p>
                <p className="text-xs text-slate-600"><time dateTime={record.at}>{date.format(new Date(record.at))}</time> · {record.actor}</p>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
