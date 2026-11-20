/** @license SPDX-License-Identifier: Apache-2.0 */
import { PlatformProvider, usePlatform } from './context/PlatformContext';
import { TopNavAndToolbar } from './components/TopNavAndToolbar';
import { AuthModal } from './components/AuthModal';
import { useCallback, useEffect, useRef, useState } from 'react';
import { publicDemo, request } from './services/api.js';
import { ValidationToasts } from './components/ValidationToasts';
import DashboardPage from './pages/DashboardPage.jsx';
import TrendPage from './pages/TrendPage.jsx';
import FinancePage from './pages/FinancePage.jsx';
import DocumentPage from './pages/DocumentPage.jsx';
import { AIMailInbox } from './components/AIMailInbox';
import { PeerPerformance } from './components/PeerPerformance';
import { PurchaseInbox } from './components/PurchaseInbox';
import { PurchasePriceHistory } from './components/PurchasePriceHistory';
import DataOpsPage from './pages/DataOpsPage.jsx';
const pages = { dashboard: DashboardPage, 'news-sns': TrendPage, finance: FinancePage, dataops: DataOpsPage };
const purchaseHistoryParams = new URLSearchParams(window.location.search);
const historyWindow = purchaseHistoryParams.get('view') === 'purchase-history';
const inboxWindow = purchaseHistoryParams.get('view') === 'purchase-inbox';
const priceWindow = purchaseHistoryParams.get('view') === 'purchase-prices';
const mailWindow = purchaseHistoryParams.get('view') === 'ai-mail';
const peerWindow = purchaseHistoryParams.get('view') === 'peer-performance';

function Workspace() {
 const { activeTab } = usePlatform();
 const Page = pages[activeTab] || TrendPage;
 const [reviewCount, setReviewCount] = useState(null);
 const [reviewVersion, setReviewVersion] = useState(0);
 const reviewChannel = useRef(null);
 useEffect(() => {
   if (historyWindow) document.title = '처리 이력 | DataPulse AI';
   if (inboxWindow) document.title = '미처리 증빙함 | DataPulse AI';
   if (priceWindow) document.title = '매입 단가 이력 | DataPulse AI';
   if (mailWindow) document.title = 'AI 메일 업무함 | DataPulse AI';
   if (peerWindow) document.title = '동종업체 실적 비교 | DataPulse AI';
 }, []);
 const refreshReviewCount = useCallback(() => {
   setReviewVersion((version) => version + 1);
   reviewChannel.current?.postMessage('changed');
 }, []);
 useEffect(() => {
   function refresh() { setReviewVersion((version) => version + 1); }
   window.addEventListener('focus', refresh);
   const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('purchase-records') : null;
   if (channel) channel.addEventListener('message', refresh);
   reviewChannel.current = channel;
   return () => {
     window.removeEventListener('focus', refresh);
     channel?.close();
     reviewChannel.current = null;
   };
 }, []);
 useEffect(() => {
   if (publicDemo) return;
   const abort = new AbortController();
   request('/api/v1/documents/review-count', { signal: abort.signal })
     .then(({ count }) => { if (!abort.signal.aborted) setReviewCount(count); })
     .catch(() => { if (!abort.signal.aborted) setReviewCount(null); });
   return () => abort.abort();
 }, [activeTab, reviewVersion]);
 if (peerWindow) {
   return <main className="mx-auto min-h-screen max-w-[1440px] bg-slate-50 px-4 py-6 text-slate-900 sm:px-6">
     <PeerPerformance />
   </main>;
 }
 if (mailWindow) {
   return <main className="mx-auto min-h-screen max-w-[1440px] bg-slate-50 px-4 py-8 text-slate-900 sm:px-6">
     <AIMailInbox initialQueue={purchaseHistoryParams.get('queue')} />
   </main>;
 }
 if (publicDemo && (priceWindow || inboxWindow || historyWindow)) {
   return <main className="mx-auto max-w-2xl space-y-3 px-4 py-8 text-slate-900">
     <h1 className="text-xl font-bold">증빙 업무</h1>
     <p>증빙 업로드와 처리 이력은 팀 업무 환경에서 사용할 수 있습니다.</p>
     <a href="?tab=doc-ai" className="text-blue-800 underline">AI 업무지원으로 돌아가기</a>
   </main>;
 }
 if (priceWindow) {
   return <main className="mx-auto min-h-screen max-w-[1440px] bg-slate-50 px-4 py-8 text-slate-900 sm:px-6">
     <PurchasePriceHistory revision={reviewVersion} />
   </main>;
 }
 if (inboxWindow) {
   return <main className="mx-auto min-h-screen max-w-[1440px] bg-slate-50 px-4 py-8 text-slate-900 sm:px-6">
     <PurchaseInbox revision={reviewVersion} />
   </main>;
 }
 if (historyWindow) {
   return <main className="mx-auto min-h-screen max-w-[1440px] bg-slate-50 px-4 py-8 text-slate-900 sm:px-6">
     <DocumentPage historyWindow initialStatus={purchaseHistoryParams.get('status')} reviewRevision={reviewVersion}
       initialRecord={purchaseHistoryParams.get('record')} initialKind={purchaseHistoryParams.get('kind')}
       reviewCount={reviewCount} onReviewCountChange={refreshReviewCount} />
   </main>;
 }
 return <div className="min-h-screen bg-slate-50 text-slate-900">
   <TopNavAndToolbar reviewCount={reviewCount} />
   {activeTab !== 'doc-ai' && <div role="note" className="border-b border-amber-200 bg-amber-50 px-4 py-2 text-center text-xs text-amber-900">데모 모드 · 뉴스·시세·AI·OCR·로그인·자동화는 시뮬레이션입니다. 실제 API·DB에는 연결되지 않습니다.</div>}
   <main className={activeTab === 'dashboard' ? '' : 'mx-auto max-w-[1440px] px-4 py-8 sm:px-6'}>
     {activeTab === 'doc-ai'
       ? <DocumentPage reviewCount={reviewCount} onReviewCountChange={refreshReviewCount} reviewRevision={reviewVersion} />
       : <Page />}
   </main>
   <footer className="border-t border-slate-200 bg-white px-4 py-5 text-xs text-slate-600">
     <div className="mx-auto flex max-w-[1440px] flex-wrap items-center justify-between gap-4"><span>DataPulse AI · AutoFlow 통합 워크스페이스</span>
     <nav aria-label="기존 AutoFlow 화면" className="flex flex-wrap gap-4">
       <a href={`${import.meta.env.BASE_URL}legacy/index.html`} target="_blank" rel="noreferrer">기존 대시보드 ↗</a>
       <a href={`${import.meta.env.BASE_URL}legacy/upload.html`} target="_blank" rel="noreferrer">기존 문서 업로드 ↗</a>
       <a href={`${import.meta.env.BASE_URL}legacy/history.html`} target="_blank" rel="noreferrer">기존 처리 이력 ↗</a>
       <a href={`${import.meta.env.BASE_URL}legacy/admin_rpa.html`} target="_blank" rel="noreferrer">기존 RPA ↗</a>
     </nav></div>
   </footer>
   <AuthModal /><ValidationToasts />
 </div>;
}
export default function App() { return <PlatformProvider><Workspace /></PlatformProvider>; }
