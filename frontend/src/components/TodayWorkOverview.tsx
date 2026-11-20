import { ArrowRight, ClipboardCheck, Clock, Mail } from 'lucide-react';
import { useEffect, useState } from 'react';
import { publicDemo, request } from '../services/api.js';
import { latestByConversation, mailInboxEndpoint, scopeRows, type Inbox } from '../services/mailInbox';
import { mailWorkEndpoint, type MailWorkOverview } from './MailWorkflow';

export function TodayWorkOverview({ reviewCount, revision }: { reviewCount: number | null; revision: number }) {
  const [mailCounts, setMailCounts] = useState<{ reply: number; urgent: number }>();
  const [mailError, setMailError] = useState(false);

  useEffect(() => {
    const abort = new AbortController();
    const options = { signal: abort.signal };
    void Promise.all([request(mailInboxEndpoint, options) as Promise<Inbox>, request(mailWorkEndpoint, options) as Promise<MailWorkOverview>])
      .then(([inbox, work]) => {
        if (abort.signal.aborted) return;
        const rows = latestByConversation(scopeRows(inbox, 'all', work));
        setMailCounts({ reply: rows.filter((row) => row.reply).length, urgent: rows.filter((row) => row.urgent).length });
        setMailError(false);
      }).catch(() => {
        if (abort.signal.aborted) return;
        setMailCounts(undefined);
        setMailError(true);
      });
    return () => abort.abort();
  }, [revision]);

  const cards = [
    { label: '회신 기한 임박', count: mailCounts?.urgent, href: '?view=ai-mail&queue=urgent', icon: Clock, urgent: true },
    { label: '답장 필요', count: mailCounts?.reply, href: '?view=ai-mail&queue=reply', icon: Mail },
    ...(!publicDemo ? [{ label: '증빙 검토 대기', count: reviewCount, href: '?view=purchase-history&status=review', icon: ClipboardCheck }] : []),
  ];

  return (
    <section aria-labelledby="today-work-heading" className="w-full space-y-2">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <h2 id="today-work-heading" className="text-base font-semibold text-slate-900">오늘 할 일</h2>
        <p className="text-xs text-slate-600">메일은 읽지 않은 대화 기준 · 회신 기한 임박은 오늘부터 3일 이내</p>
      </div>
      <div className={`grid gap-2 sm:gap-3 ${publicDemo ? 'grid-cols-2' : 'grid-cols-3'}`}>
        {cards.map(({ label, count, href, icon: Icon, urgent }) => {
          const highlighted = urgent && count != null && count > 0;
          return (
            <a key={label} href={href} className={`flex min-w-0 flex-col justify-between gap-2 rounded-xl border p-2.5 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 sm:p-4 ${highlighted ? 'border-amber-300 bg-amber-50 text-amber-950 hover:bg-amber-100' : 'border-slate-200 bg-white text-slate-900 hover:border-slate-400'}`}>
              <span className="flex items-center gap-1.5 text-xs font-semibold sm:text-sm">
                <Icon aria-hidden="true" className="hidden size-4 shrink-0 sm:block" />{label}
                <ArrowRight aria-hidden="true" className="ml-auto hidden size-4 shrink-0 sm:block" />
              </span>
              <span className="text-xl font-bold leading-none tabular-nums sm:text-3xl">
                {count == null ? '—' : <>{count}<span className="ml-0.5 text-xs font-medium sm:text-sm">건</span></>}
              </span>
            </a>
          );
        })}
      </div>
      {mailError && <p role="status" className="mt-2 text-xs text-amber-900">메일 업무 건수를 불러오지 못했습니다.</p>}
    </section>
  );
}
