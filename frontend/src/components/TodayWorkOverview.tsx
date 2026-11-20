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
    ...(!publicDemo ? [{ label: '증빙 검토 대기', count: reviewCount, href: '?view=purchase-history&status=review', icon: ClipboardCheck }] : []),
    { label: '답장 필요', count: mailCounts?.reply, href: '?view=ai-mail&queue=reply', icon: Mail },
    { label: '회신 기한 임박', count: mailCounts?.urgent, href: '?view=ai-mail&queue=urgent', icon: Clock, urgent: true },
  ];

  return (
    <section aria-labelledby="today-work-heading" className="w-full rounded-xl border border-slate-200 bg-white p-3 sm:px-4">
      <div className="mb-2 flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <h2 id="today-work-heading" className="text-sm font-semibold text-slate-900">오늘 확인할 업무</h2>
        <p className="text-xs text-slate-600">메일은 읽지 않은 대화 기준 · 기한 임박은 오늘부터 3일 이내</p>
      </div>
      <div className={`grid gap-1.5 sm:gap-2 ${publicDemo ? 'grid-cols-2' : 'grid-cols-3'}`}>
        {cards.map(({ label, count, href, icon: Icon, urgent }) => (
          <a key={label} href={href} className="grid min-w-0 grid-cols-[1fr_auto] items-center gap-x-1 rounded-lg border border-slate-200 bg-slate-50 p-2 text-slate-700 hover:border-blue-300 hover:bg-blue-50 focus-visible:outline-blue-700">
            <span className="flex min-w-0 items-center gap-1 text-xs font-semibold sm:text-sm">
              <Icon aria-hidden="true" className="hidden size-4 shrink-0 text-blue-700 sm:block" />{label}
            </span>
            <ArrowRight aria-hidden="true" className="size-3 text-slate-500" />
            <span className={`col-span-2 mt-0.5 text-base font-bold tabular-nums ${urgent && count ? 'text-amber-900' : 'text-slate-900'}`}>
              {count == null ? '—' : `${count}건`}
            </span>
          </a>
        ))}
      </div>
      {mailError && <p role="status" className="mt-2 text-xs text-amber-900">메일 업무 건수를 불러오지 못했습니다.</p>}
    </section>
  );
}
