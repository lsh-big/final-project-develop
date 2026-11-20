import { ArrowDown, ArrowRight, ChartNoAxesCombined, Mail, Receipt } from 'lucide-react';
import { useEffect } from 'react';
import { publicDemo } from '../services/api.js';
import { PurchaseReviewView, type PurchaseReviewProps } from './PurchaseReviewView';
import { TodayWorkOverview } from './TodayWorkOverview';

const menuItems = [
  { label: '메일 업무', description: '읽지 않은 메일 요약 · 답장 작성', href: '?view=ai-mail', icon: Mail, arrow: ArrowRight },
  ...(!publicDemo ? [{
    label: '구매 증빙', description: '증빙 업로드 · 검토', href: '#purchase-evidence', icon: Receipt, arrow: ArrowDown,
  }] : []),
  { label: '기업 실적', description: '동종업체 공시 비교', href: '?view=peer-performance', icon: ChartNoAxesCombined, arrow: ArrowRight },
];

export function DocumentAiView(props: PurchaseReviewProps) {
  useEffect(() => {
    if (!props.historyWindow && !publicDemo && window.location.hash === '#purchase-evidence') {
      document.getElementById('purchase-evidence')?.scrollIntoView();
    }
  }, [props.historyWindow]);
  if (props.historyWindow) return <PurchaseReviewView {...props} />;

  return <div className="space-y-7 sm:space-y-9">
    <header className="space-y-1">
      <h1 className="text-2xl font-bold tracking-tight sm:text-3xl">AI 업무지원</h1>
      {publicDemo && <p className="text-sm text-slate-600">증빙 업무는 팀 업무 환경에서 제공됩니다.</p>}
    </header>
    <TodayWorkOverview reviewCount={props.reviewCount} revision={props.reviewRevision ?? 0} />
    <nav aria-labelledby="workspace-functions-heading" className="space-y-2">
      <h2 id="workspace-functions-heading" className="text-base font-semibold text-slate-900">주요 메뉴</h2>
      <ul className={`grid gap-2 sm:gap-3 ${publicDemo ? 'sm:grid-cols-2' : 'sm:grid-cols-3'}`}>
        {menuItems.map(({ label, description, href, icon: Icon, arrow: Arrow }) => (
          <li key={href}>
            <a href={href} className="flex h-full min-h-16 items-center gap-3 rounded-xl border border-slate-200 bg-white px-3 py-2.5 text-slate-900 hover:border-slate-400 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 sm:min-h-24 sm:px-4">
              <span className="grid size-10 shrink-0 place-items-center rounded-lg bg-slate-100 text-slate-700">
                <Icon aria-hidden="true" className="size-5" />
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-base font-semibold">{label}</span>
                <span className="block text-xs leading-snug text-slate-600 sm:mt-1 sm:text-sm">{description}</span>
              </span>
              <Arrow aria-hidden="true" className="size-4 shrink-0 text-slate-500" />
            </a>
          </li>
        ))}
      </ul>
    </nav>
    {!publicDemo && <PurchaseReviewView {...props} />}
  </div>;
}
