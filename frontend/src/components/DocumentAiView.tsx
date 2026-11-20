import { ArrowRight, ChartNoAxesCombined, Mail } from 'lucide-react';
import { publicDemo } from '../services/api.js';
import { PurchaseReviewView, type PurchaseReviewProps } from './PurchaseReviewView';
import { TodayWorkOverview } from './TodayWorkOverview';

export function DocumentAiView(props: PurchaseReviewProps) {
  const overview = !props.historyWindow && (
    <TodayWorkOverview reviewCount={props.reviewCount} revision={props.reviewRevision ?? 0} />
  );
  const actions = !props.historyWindow && (
    <nav aria-label="AI 업무지원 메뉴" className="flex w-full flex-wrap gap-2 sm:w-auto">
      <a href="?view=peer-performance"
        className="flex min-h-14 w-full items-center gap-3 rounded-xl border border-slate-300 bg-white px-3 py-2 text-base font-semibold text-blue-900 transition-colors hover:bg-blue-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 sm:w-auto sm:px-4">
        <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-blue-50 text-blue-700">
          <ChartNoAxesCombined aria-hidden="true" className="size-5" />
        </span>
        <span className="flex-1">동종업체 실적 비교</span>
        <ArrowRight aria-hidden="true" className="size-4 shrink-0" />
      </a>
      <a href="?view=ai-mail"
        className="flex min-h-14 w-full items-center gap-3 rounded-xl bg-blue-700 px-3 py-2 text-base font-semibold text-white shadow-sm transition-colors hover:bg-blue-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 sm:w-auto sm:min-w-56 sm:px-4">
        <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-white/15">
          <Mail aria-hidden="true" className="size-5" />
        </span>
        <span className="flex-1">AI 메일 업무함</span>
        <ArrowRight aria-hidden="true" className="size-4 shrink-0" />
      </a>
    </nav>
  );
  if (publicDemo) {
    return <section className="space-y-3 rounded-xl border border-slate-200 bg-white p-4">
      <h1 className="text-xl font-bold">AI 업무지원</h1>
      <p className="text-sm text-slate-600">시연 메일과 기업 공시를 조회할 수 있습니다. 증빙 업무는 팀 업무 환경에서 제공됩니다.</p>
      {actions}
      {overview}
    </section>;
  }
  return <PurchaseReviewView {...props} workspaceOverview={overview} workspaceAction={actions} />;
}
