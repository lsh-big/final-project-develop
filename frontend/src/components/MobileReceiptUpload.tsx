import { useEffect, useState } from 'react';
import { request } from '../services/api.js';

type Ticket = { id: string; url: string; expires_at: string; qr_image: string };
type DeliveryItem =
  | { filename: string; record_id: string; duplicate: boolean; error?: never }
  | { filename: string; error: string; record_id?: never; duplicate?: never };
type Delivery = {
  state: 'waiting' | 'uploading' | 'expired' | 'received';
  record_id: string | null;
  duplicate: boolean;
  items?: DeliveryItem[];
};

const sessions = '/api/v1/receipts/mobile-sessions';
const button = 'rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-800 hover:bg-slate-50 disabled:opacity-50';

export function MobileReceiptUpload({ disabled, onReceived, onOpen }: {
  disabled: boolean;
  onReceived: () => void;
  onOpen: (recordId: string) => void;
}) {
  const [ticket, setTicket] = useState<Ticket | null>(null);
  const [delivery, setDelivery] = useState<Delivery | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!ticket) return;
    const abort = new AbortController();
    let timeout: ReturnType<typeof setTimeout>;
    async function poll(id: string) {
      try {
        const next: Delivery = await request(`${sessions}/${id}`, { signal: abort.signal });
        if (abort.signal.aborted) return;
        setDelivery(next);
        setError('');
        if (next.state === 'received') {
          onReceived();
          return;
        }
        if (next.state === 'expired') return;
      } catch {
        if (abort.signal.aborted) return;
        setError('PC에서 전송 상태를 확인하지 못했습니다. 연결을 확인하는 중입니다.');
      }
      timeout = setTimeout(() => void poll(id), 1500);
    }
    void poll(ticket.id);
    return () => {
      abort.abort();
      clearTimeout(timeout);
    };
  }, [ticket, onReceived]);

  async function start() {
    setCreating(true);
    setError('');
    try {
      const next: Ticket = await request(sessions, { method: 'POST' });
      setDelivery(null);
      setTicket(next);
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : 'QR을 만들지 못했습니다.');
    } finally {
      setCreating(false);
    }
  }

  const active = ticket && delivery?.state !== 'expired' && delivery?.state !== 'received';
  const items = delivery?.items ?? [];
  const newCount = items.filter((item) => item.record_id && !item.duplicate).length;

  return (
    <div className="mt-4 space-y-3">
      {!active && (
        <button type="button" className={button} disabled={disabled || creating} onClick={() => void start()}>
          {creating ? 'QR 만드는 중…' : '휴대폰으로 올리기'}
        </button>
      )}
      {active && (
        <div className="flex flex-wrap items-center gap-4 rounded-lg border border-slate-200 bg-slate-50 p-4">
          <img src={ticket.qr_image} alt="휴대폰 구매 영수증 업로드 QR" width="192" height="192" className="rounded-md bg-white" />
          <div className="space-y-2 text-sm">
            <p className="font-semibold">같은 Wi-Fi에 연결한 휴대폰으로 QR을 스캔해 주세요.</p>
            <p className="text-slate-600">QR은 5분 동안 사용할 수 있으며 사진을 최대 20장 보낼 수 있습니다.</p>
            <a href={ticket.url} target="_blank" rel="noreferrer" className="font-semibold text-blue-700 underline">업로드 링크 열기</a>
            <p role="status" className="text-slate-700">
              {delivery?.state === 'uploading' ? '사진을 받거나 OCR로 읽는 중입니다…' : '휴대폰 전송 대기 중…'}
            </p>
          </div>
        </div>
      )}
      {delivery?.state === 'expired' && <p role="status" className="text-sm text-slate-700">QR이 만료되었습니다. 새 QR을 열어 주세요.</p>}
      {delivery?.state === 'received' && (
        <div className="space-y-2">
          <p role="status" className="text-sm text-emerald-800">
            {items.length > 1
              ? `사진 ${items.length}장 처리 완료 · 검토 대기함에 ${newCount}장 추가`
              : delivery.duplicate ? '이미 등록된 사진입니다.' : '휴대폰 사진이 검토 대기함에 도착했습니다.'}
          </p>
          {items.length ? (
            <ul className="divide-y divide-slate-100">
              {items.map((item, index) => (
                <li key={index} className="flex flex-wrap items-center justify-between gap-2 py-2">
                  <div className="min-w-0">
                    <p className="break-all text-sm font-medium">{item.filename}</p>
                    {item.error && <p className="text-sm text-red-800">{item.error}</p>}
                    {item.duplicate && <p className="text-sm text-slate-600">이미 등록된 사진</p>}
                  </div>
                  {item.record_id && (
                    <button type="button" className={button} disabled={disabled}
                      aria-label={items.length > 1 ? `${item.filename} 검토 열기` : undefined}
                      onClick={() => onOpen(item.record_id!)}>
                      {item.duplicate ? '기존 영수증 열기' : '받은 영수증 검토'}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          ) : delivery.record_id && (
            <button type="button" className={button} disabled={disabled} onClick={() => onOpen(delivery.record_id!)}>
              {delivery.duplicate ? '기존 영수증 열기' : '받은 영수증 검토'}
            </button>
          )}
        </div>
      )}
      {error && <p role="alert" className="text-sm text-red-800">{error}</p>}
    </div>
  );
}
