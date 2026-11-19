import { useEffect, useId, useRef, useState } from 'react';
import { request } from '../services/api.js';
import { availableAnalysis, inPeriod, latestByConversation, mailCategories as categories, mailDayFormat as dayKeyFormat,
  mailInboxEndpoint as endpoint, mailTimeZone as timeZone, scopeRows, shiftDay,
  type Analysis, type Inbox, type Mail, type MailEntry, type Period, type SummaryRow, type Thread } from '../services/mailInbox';
import { emptyMailWork, mailWorkEndpoint, mailWorkLabels, MailWorkflow, type MailWorkOverview } from './MailWorkflow';

const button = 'rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-xs font-semibold text-blue-800 hover:bg-slate-50 disabled:opacity-50';
const timeFormat = new Intl.DateTimeFormat('ko-KR', { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone });
const hourFormat = new Intl.DateTimeFormat('ko-KR', { hour: 'numeric', minute: '2-digit', timeZone });
const dateFormat = new Intl.DateTimeFormat('ko-KR', { year: 'numeric', month: 'long', day: 'numeric', timeZone });
const periods = { all: '전체 기간', recent: '최근 30일', older: '30일 이전' };

type QueueFilter = 'all' | 'reply' | 'urgent' | 'overdue';
type WorkStatus = keyof typeof mailWorkLabels;
type WorkFilter = 'all' | WorkStatus;
const workFilters: [WorkFilter, string][] = [['all', '전체'], ...Object.entries(mailWorkLabels) as [WorkStatus, string][]];
type Vendor = NonNullable<Mail['vendor']>;
type VendorGroup = { name: string; verified: boolean; vendor?: Vendor; days: Map<string, SummaryRow[]> };

function deadlineLabel(analysis: Analysis) {
  if (analysis.deadline_status === 'ambiguous') return '기한 확인 필요';
  return analysis.deadline ?? '기한 미기재';
}

function Deadline({ analysis, overdue, soon = false }: { analysis: Analysis; overdue: boolean; soon?: boolean }) {
  return <span className={overdue ? 'font-semibold text-red-800' : soon ? 'font-semibold text-amber-900' : undefined}>
    {deadlineLabel(analysis)}{overdue ? ' · 기한 지남' : soon ? ' · 기한 임박' : ''}
  </span>;
}

function displayDay(day: string, today: string) {
  const label = day === today ? '오늘' : day === shiftDay(today, -1) ? '어제' : '';
  const calendar = dateFormat.format(new Date(`${day}T00:00:00+09:00`));
  return label ? `${label} · ${calendar}` : calendar;
}

function analysisState(mail: Mail) {
  return mail.analysis_status === 'failed' ? '분석 실패' : '분석 대기';
}

function matchingMail(inbox: Inbox, period: Period, vendor?: Vendor) {
  return inbox.threads.flatMap((thread) => thread.messages
    .filter((mail) => (!vendor || mail.vendor?.id === vendor.id) && inPeriod(mail, period, inbox.recent_since))
    .map((mail) => ({ thread, mail })))
    .sort((left, right) => Date.parse(right.mail.received_at) - Date.parse(left.mail.received_at));
}

function groupMailDates(entries: MailEntry[]) {
  const dated = new Map<string, MailEntry[]>();
  for (const entry of entries) {
    const day = dayKeyFormat.format(new Date(entry.mail.received_at));
    if (!dated.has(day)) dated.set(day, []);
    dated.get(day)!.push(entry);
  }
  return dated;
}

function groupVendors(rows: SummaryRow[]) {
  const groups = new Map<string, VendorGroup>();
  for (const row of rows) {
    const vendor = row.mail.vendor?.id && row.mail.vendor.name ? row.mail.vendor : undefined;
    const key = vendor ? `vendor:${vendor.id}` : 'unknown';
    if (!groups.has(key)) groups.set(key, { name: vendor?.name ?? '거래처 미확인', verified: !!vendor, vendor, days: new Map() });
    const group = groups.get(key)!;
    if (!group.days.has(row.day)) group.days.set(row.day, []);
    group.days.get(row.day)!.push(row);
  }
  return [...groups.entries()].sort(([, left], [, right]) => Number(right.verified) - Number(left.verified));
}

function firstSentence(text: string) {
  return text.trim().split(/(?<=[.!?。！？])\s+|\n/)[0];
}

function briefAnalysis(analysis: Analysis) {
  return firstSentence(analysis.summary) || firstSentence(analysis.request ?? '');
}

function matchesSearch(mail: Mail, terms: string[]) {
  if (!terms.length) return true;
  const analysis = availableAnalysis(mail);
  const content = [mail.vendor?.name, mail.sender, mail.subject, analysis?.summary, analysis?.request]
    .join(' ').normalize('NFKC').toLowerCase();
  return terms.every((term) => content.includes(term));
}

export function AIMailInbox({ initialQueue }: { initialQueue?: string | null }) {
  const [inbox, setInbox] = useState<Inbox>();
  const [showAll, setShowAll] = useState(false);
  const [selectedVendor, setSelectedVendor] = useState<Vendor>();
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState('');
  const [loadError, setLoadError] = useState('');
  const [period, setPeriod] = useState<Period>('all');
  const [search, setSearch] = useState('');
  const [queueFilter, setQueueFilter] = useState<QueueFilter>(initialQueue === 'reply' || initialQueue === 'urgent' ? initialQueue : 'all');
  const [workFilter, setWorkFilter] = useState<WorkFilter>('all');
  const [page, setPage] = useState(1);
  const [wide, setWide] = useState(() => window.matchMedia('(min-width: 1024px)').matches);
  const [selected, setSelected] = useState<string>();
  const [selectedMail, setSelectedMail] = useState<string>();
  const [work, setWork] = useState<MailWorkOverview>();
  const [workError, setWorkError] = useState('');
  const detailRef = useRef<HTMLElement>(null);
  const workflowRef = useRef<HTMLDetailsElement>(null);
  const draftFormId = useId();
  const listRef = useRef<HTMLDivElement>(null);
  const demoRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    const abort = new AbortController();
    void request(endpoint, { signal: abort.signal })
      .then((data: Inbox) => { if (!abort.signal.aborted) setInbox(data); })
      .catch(() => { if (!abort.signal.aborted) setLoadError('받은편지함을 불러오지 못했습니다.'); });
    void request(mailWorkEndpoint, { signal: abort.signal })
      .then((data: MailWorkOverview) => { if (!abort.signal.aborted) setWork(data); })
      .catch(() => { if (!abort.signal.aborted) setWorkError('업무 기록을 불러오지 못했습니다.'); });
    return () => {
      abort.abort();
      demoRequest.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (selected) detailRef.current?.scrollIntoView({ block: 'start' });
  }, [selected, selectedMail]);
  useEffect(() => {
    const viewport = window.matchMedia('(min-width: 1024px)');
    const resize = () => { setWide(viewport.matches); setPage(1); setSelected(undefined); setSelectedMail(undefined); };
    viewport.addEventListener('change', resize);
    return () => viewport.removeEventListener('change', resize);
  }, []);

  async function openAllMail() {
    if (demoRequest.current) return;
    const abort = new AbortController();
    demoRequest.current = abort;
    setImporting(true);
    setImportError('');
    setLoadError('');
    try {
      const imported: Inbox = await request(endpoint, { method: 'POST', signal: abort.signal });
      if (abort.signal.aborted) return;
      setInbox(imported);
      changeView(true);
    } catch {
      if (!abort.signal.aborted) setImportError('전체 메일을 불러오지 못했습니다. 다시 시도해 주세요.');
    } finally {
      demoRequest.current = null;
      if (!abort.signal.aborted) setImporting(false);
    }
  }

  function changeView(all: boolean, vendor?: Vendor) {
    setShowAll(all);
    setSelectedVendor(vendor);
    setPeriod('all');
    setQueueFilter('all');
    if (vendor) setSearch('');
    setPage(1);
    setSelected(undefined);
    setSelectedMail(undefined);
  }

  const scopedRows = inbox ? scopeRows(inbox, period, work) : [];
  const terms = search.normalize('NFKC').toLowerCase().trim().split(/\s+/).filter(Boolean);
  const statusFor = (threadId: string) => work ? work.threads[threadId]?.status ?? emptyMailWork.status : undefined;
  const matchesFilters = ({ mail, thread }: MailEntry) =>
    (workFilter === 'all' || statusFor(thread.id) === workFilter) && matchesSearch(mail, terms);
  const searchedRows = scopedRows.filter(matchesFilters);
  const queueRows = latestByConversation(scopedRows).filter(matchesFilters);
  const urgentCount = queueRows.filter((row) => row.urgent).length;
  const overdueCount = queueRows.filter((row) => row.overdue).length;
  const rows = queueFilter === 'all' ? searchedRows : queueRows.filter((row) => row[queueFilter]);
  const conversationCount = new Set(rows.map((row) => row.thread.id)).size;
  const groups = groupVendors(rows);
  const mails = inbox && showAll ? matchingMail(inbox, period, selectedVendor)
    .filter(matchesFilters) : [];
  const total = showAll ? mails.length : rows.length;
  const pageSize = showAll ? (wide ? 20 : 8) : (wide ? 12 : 4);
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const current = Math.min(page, pageCount);
  const start = (current - 1) * pageSize;
  const end = Math.min(start + pageSize, total);
  const dated = groupMailDates(mails.slice(start, end));
  const visibleGroups = groupVendors(groups.flatMap(([, group]) => [...group.days.values()].flat()).slice(start, end));
  const conversation = inbox?.threads.find((thread) => thread.id === selected);
  const activeMail = conversation?.messages.find((mail) => mail.id === selectedMail);

  function openDraftPanel() {
    const panel = workflowRef.current;
    if (!panel) return;
    panel.open = true;
    panel.scrollIntoView({ block: 'start' });
    panel.querySelector('summary')?.focus({ preventScroll: true });
  }

  function goToPage(next: number) {
    if (next === current || next < 1 || next > pageCount) return;
    setPage(next);
    setSelected(undefined);
    setSelectedMail(undefined);
    listRef.current?.scrollIntoView({ block: 'start' });
  }

  function openMail(thread: Thread, mail: Mail) {
    const alreadyOpen = selectedMail === mail.id;
    setSelected(alreadyOpen ? undefined : thread.id);
    setSelectedMail(alreadyOpen ? undefined : mail.id);
  }

  function resetListPosition() {
    setPage(1);
    setSelected(undefined);
    setSelectedMail(undefined);
  }

  function toggleQueue(filter: QueueFilter) {
    setQueueFilter((current) => current === filter ? 'all' : filter);
    resetListPosition();
  }

  return (
    <section className="space-y-2 rounded-xl border border-slate-200 bg-white p-3 sm:p-4">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-bold">{showAll
            ? selectedVendor ? `${selectedVendor.name} 전체 메일` : '전체 메일'
            : '읽지 않은 메일 요약'}</h1>
        </div>
        <div className="flex flex-wrap gap-2">
          <a className={button} href="?tab=doc-ai">AI 업무지원으로 돌아가기</a>
        </div>
      </header>
      <section className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200 pb-2"
        aria-label="메일 보기">
        <div>
          <h2 className="text-xs font-semibold text-slate-600">회사 받은편지함 · 시연 연결</h2>
          {importError && <p role="alert" className="mt-1 text-sm text-red-800">{importError}</p>}
        </div>
        <div className="flex flex-wrap gap-2">
          {selectedVendor && <button type="button" className={button} onClick={() => changeView(true)}>모든 거래처 메일 보기</button>}
          {showAll ? <button type="button" className={button} onClick={() => changeView(false)}>읽지 않은 메일 요약으로</button>
            : <button type="button" className={button} disabled={importing || (!inbox && !loadError && !importError)}
                onClick={() => void openAllMail()}>{importing ? '불러오는 중…' : '전체 메일 보기'}</button>}
        </div>
      </section>
      {loadError && <p role="alert" className="text-sm text-red-800">{loadError}</p>}
      {workError && <p role="alert" className="text-sm text-red-800">{workError}</p>}
      {!loadError && !importError && !inbox && <p role="status" className="text-sm text-slate-600">받은편지함을 불러오는 중입니다…</p>}
      {inbox && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm font-semibold text-slate-700">{showAll
              ? `메일 ${total}건` : '시연 분석 결과'}</p>
            <div role="group" aria-label="수신 기간" className="flex flex-wrap gap-2">
              {(Object.entries(periods) as [Period, string][]).map(([key, label]) => (
                <button key={key} type="button" className={`${button} ${period === key ? 'ring-1 ring-blue-600' : ''}`}
                  aria-pressed={period === key} onClick={() => {
                    setPeriod(key); setPage(1); setSelected(undefined); setSelectedMail(undefined);
                  }}>{label}</button>
              ))}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex min-w-0 flex-1 flex-wrap items-center gap-2 text-xs font-semibold text-slate-700 sm:max-w-xl">
              <span>메일 검색</span>
              <input type="search" value={search} maxLength={200} placeholder="거래처, 제목, 요약 검색"
                className="min-w-0 flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm font-normal"
                onChange={(event) => { setSearch(event.target.value); resetListPosition(); }} />
            </label>
            {search && <button type="button" className={button} onClick={() => {
              setSearch(''); resetListPosition();
            }}>검색 지우기</button>}
            {queueFilter !== 'all' && (
              <button type="button" className={button} onClick={() => toggleQueue('all')}>
                {queueFilter === 'reply' ? '답장 필요' : queueFilter === 'urgent' ? '회신 기한 임박' : '기한 지남'} 필터 해제
              </button>
            )}
          </div>
          <div role="group" aria-label="업무 처리 상태 필터" className="flex flex-wrap items-center gap-1.5">
            <span className="mr-0.5 text-xs font-semibold text-slate-700">처리 상태</span>
            {workFilters.map(([key, label]) => (
              <button key={key} type="button" aria-pressed={workFilter === key} disabled={key !== 'all' && !work}
                className={`${button} ${workFilter === key ? 'bg-blue-50 ring-1 ring-blue-600' : ''}`} onClick={() => {
                  setWorkFilter(key); resetListPosition();
                }}>{label}</button>
            ))}
          </div>
          {!showAll && <dl className="grid grid-cols-3 divide-x divide-slate-200 rounded-md border border-slate-200 bg-slate-50" aria-label="현재 조회 범위 집계">
            <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 px-2 py-1">
              <dt className="text-xs font-semibold text-slate-700 sm:text-sm">읽지 않은 메일</dt>
              <dd className="text-sm font-bold tabular-nums">{rows.length}건</dd>
              <dd className="text-xs text-slate-600">메일별 요약 {rows.length}건 · 대화 {conversationCount}건</dd>
            </div>
            <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 px-2 py-1">
              <dt className="text-xs font-semibold text-slate-700 sm:text-sm">거래처</dt>
              <dd className="text-sm font-bold tabular-nums">{groups.filter(([, group]) => group.verified).length}곳</dd>
              {groups.some(([, group]) => !group.verified) && <dd className="text-xs text-slate-600">미확인 제외</dd>}
            </div>
            <div className="min-w-0 px-2 py-1">
              <dt className="text-xs font-semibold text-slate-700 sm:text-sm">회신 기한</dt>
              <dd className="mt-0.5 flex flex-wrap gap-1">
                <button type="button" className={`${button} ${queueFilter === 'urgent' ? 'ring-1 ring-blue-600' : ''}`}
                  aria-label={`회신 기한 임박 ${urgentCount}건`} aria-pressed={queueFilter === 'urgent'}
                  onClick={() => toggleQueue('urgent')}>임박 {urgentCount}건</button>
                <button type="button" className={`${button} ${queueFilter === 'overdue' ? 'ring-1 ring-blue-600' : ''}`}
                  aria-label={`회신 기한 지남 ${overdueCount}건`} aria-pressed={queueFilter === 'overdue'}
                  onClick={() => toggleQueue('overdue')}>기한 지남 {overdueCount}건</button>
              </dd>
            </div>
          </dl>}
          <div ref={listRef} className="scroll-mt-4" />
          {!total && <p className="text-sm text-slate-600">{workFilter === 'all' ? '해당하는 메일이 없습니다.' : '선택한 조건에 해당하는 메일이 없습니다.'}</p>}
          {showAll ? <div className="divide-y divide-slate-200 rounded-lg border border-slate-200" aria-label="날짜별 전체 메일">
            {[...dated.entries()].map(([day, entries]) => (
              <section key={day} aria-label={displayDay(day, inbox.today)}
                className="grid min-w-0 gap-x-2 sm:grid-cols-[9rem_minmax(0,1fr)] sm:px-2">
                <h2 className="bg-slate-50 px-2 py-1 text-xs font-semibold text-slate-700 sm:bg-transparent sm:px-0 sm:py-2">
                  {displayDay(day, inbox.today)}
                </h2>
                <ul className="min-w-0 divide-y divide-slate-100">
                  {entries.map(({ thread, mail }) => {
                    const vendor = mail.vendor?.id && mail.vendor.name ? mail.vendor : undefined;
                    const selected = selectedMail === mail.id;
                    const status = statusFor(thread.id);
                    return (
                      <li key={mail.id} className={`grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-2 px-2 py-1 text-sm sm:min-h-8 sm:grid-cols-[clamp(7rem,18vw,12rem)_minmax(0,1fr)_auto] ${selected ? 'bg-blue-50' : 'hover:bg-slate-50'}`}>
                        {vendor && !selectedVendor ? (
                          <button type="button" aria-label={`${vendor.name} 전체 메일 보기`} title={vendor.name}
                            className="truncate text-left font-semibold text-blue-900 hover:underline focus-visible:outline-blue-700"
                            onClick={() => changeView(true, vendor)}>{vendor.name}</button>
                        ) : (
                          <span className="truncate font-semibold text-slate-800" title={vendor?.name ?? mail.sender}>
                            {vendor?.name ?? `거래처 미확인 · ${mail.sender}`}
                          </span>
                        )}
                        <button type="button" aria-label={mail.subject} aria-expanded={selected}
                          onClick={() => openMail(thread, mail)} title={mail.subject}
                          className="col-span-2 row-start-2 truncate rounded text-left text-slate-800 hover:text-blue-800 hover:underline focus-visible:outline-blue-700 sm:col-span-1 sm:row-start-auto">
                          {mail.subject}
                        </button>
                        <span className="col-start-2 row-start-1 whitespace-nowrap text-right text-xs text-slate-600 sm:col-start-auto sm:row-start-auto">
                          <MailTime value={mail.received_at} short /> · <span className={mail.unread ? 'font-semibold text-blue-800' : undefined}>
                            {mail.unread ? '안 읽음' : '읽음'}
                          </span>
                          {status && <span> · {mailWorkLabels[status]}</span>}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))}
          </div> : <div className="divide-y divide-slate-200 rounded-lg border border-slate-200" aria-label="거래처별 요약">
            {visibleGroups.map(([key, group]) => (
              <section key={key} aria-label={group.name}
                className="grid min-w-0 gap-x-2 px-2 lg:grid-cols-[8rem_minmax(0,1fr)]">
                <h2 className="break-words pt-1.5 text-sm font-semibold lg:pt-2">
                  {group.vendor ? (
                    <button type="button" className="text-left text-blue-800 underline hover:text-blue-600 focus-visible:outline-blue-700"
                      onClick={() => changeView(true, group.vendor)}>{group.name}</button>
                  ) : group.name}
                </h2>
                <div className="min-w-0 divide-y divide-slate-100">
                {[...group.days.entries()].map(([day, entries]) => (
                  <div key={day} className="grid min-w-0 gap-x-2 lg:grid-cols-[9rem_minmax(0,1fr)]">
                    <h3 className="pt-0.5 text-xs font-semibold text-slate-600 lg:pt-2">{displayDay(day, inbox.today)}</h3>
                    <ul className="min-w-0 divide-y divide-slate-100">
                      {entries.map((row) => (
                        <li key={row.mail.id}>
                          <SummaryCard row={row} today={inbox.today} showSender={!group.verified}
                            status={statusFor(row.thread.id)} selected={selectedMail === row.mail.id}
                            onSelect={() => openMail(row.thread, row.mail)} />
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
                </div>
              </section>
            ))}
          </div>}
          {pageCount > 1 && (
            <nav aria-label="메일 목록 페이지" className="flex flex-wrap items-center justify-between gap-2 py-1">
              <p className="text-xs tabular-nums text-slate-600" role="status">전체 {total}건 중 {start + 1}–{end}건</p>
              <div className="flex flex-wrap items-center gap-1">
                <button type="button" className={button} disabled={current === 1} onClick={() => goToPage(current - 1)}>이전</button>
                {Array.from({ length: pageCount }, (_, index) => index + 1).map((number) => (
                  <button key={number} type="button" className={`${button} ${number === current ? 'ring-1 ring-blue-600' : ''}`}
                    aria-label={`${number}페이지`} aria-current={number === current ? 'page' : undefined}
                    onClick={() => goToPage(number)}>{number}</button>
                ))}
                <button type="button" className={button} disabled={current === pageCount} onClick={() => goToPage(current + 1)}>다음</button>
              </div>
            </nav>
          )}
          {conversation && activeMail && (
            <section className="scroll-mt-4 space-y-4 rounded-lg border border-slate-200 p-3 sm:p-4" ref={detailRef}>
              <h2 className="font-semibold">대화 원문 및 분석 · {conversation.messages.length}건</h2>
              <MailOriginal mail={activeMail} draftFormId={work ? draftFormId : undefined} onDraft={openDraftPanel} />
              <MailAnalysisPanel mail={activeMail} today={inbox.today} />
              {conversation.messages.filter((mail) => mail.id !== activeMail.id).map((mail) => (
                <details key={`${activeMail.id}:${mail.id}`} className="rounded-lg border border-slate-200">
                  <summary className="cursor-pointer break-words px-3 py-2 text-sm text-slate-700">
                    <span className="font-semibold">다른 메일 · {mail.subject}</span>
                    <span className="ml-2 text-xs"><MailTime value={mail.received_at} /></span>
                  </summary>
                  <div className="space-y-3 border-t border-slate-200 p-3">
                    <MailOriginal mail={mail} draftFormId={work ? draftFormId : undefined} onDraft={openDraftPanel} />
                    <MailAnalysisPanel mail={mail} today={inbox.today} />
                  </div>
                </details>
              ))}
              {work && (
                <details key={conversation.id} ref={workflowRef} className="scroll-mt-4 rounded-lg border border-slate-200">
                  <summary className="cursor-pointer px-3 py-2 text-sm font-semibold text-blue-900">답장 작성 및 업무 처리</summary>
                  <div className="border-t border-slate-200 p-3">
                    <MailWorkflow threadId={conversation.id} messages={conversation.messages} viewedMail={activeMail}
                      draftFormId={draftFormId}
                      actor={work.actor} record={work.threads[conversation.id] ?? emptyMailWork}
                      onSaved={(record) => setWork((current) => current && ({
                        ...current, threads: { ...current.threads, [conversation.id]: record },
                      }))} />
                  </div>
                </details>
              )}
            </section>
          )}
        </>
      )}
    </section>
  );
}

function MailTime({ value, short = false }: { value: string; short?: boolean }) {
  return <time dateTime={value}>{(short ? hourFormat : timeFormat).format(new Date(value))}</time>;
}

function MailOriginal({ mail, draftFormId, onDraft }: { mail: Mail; draftFormId?: string; onDraft: () => void }) {
  return (
    <article id={`mail-original-${mail.id}`} className="rounded-lg border border-slate-200 bg-white p-4 sm:p-5">
      <header className="space-y-1 border-b border-slate-200 pb-3">
        <p className="text-xs font-semibold text-slate-600">메일 원문</p>
        <h3 className="break-words text-lg font-semibold text-slate-900">{mail.subject}</h3>
        <p className="break-words text-sm text-slate-600">
          {mail.sender} · <MailTime value={mail.received_at} />{mail.unread && ' · 안 읽음'}
        </p>
      </header>
      <div className="mt-4 max-w-prose space-y-3 text-sm leading-relaxed text-slate-900 sm:text-base" aria-label="메일 원문">
        {mail.sentences.map((paragraph, index) => <p key={index} className="whitespace-pre-wrap break-words">{paragraph}</p>)}
      </div>
      {draftFormId && (
        <button type="submit" className={`${button} mt-4`} form={draftFormId} value={mail.id} onClick={onDraft}>
          이 메일에 답장 작성
        </button>
      )}
    </article>
  );
}

function MailAnalysisPanel({ mail, today }: { mail: Mail; today: string }) {
  const analysis = availableAnalysis(mail);
  const brief = analysis ? briefAnalysis(analysis) : '';
  return (
    <section aria-label="AI 요약" className="space-y-1 rounded-md border border-blue-100 bg-blue-50 px-3 py-2 text-sm text-slate-800">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
        <h3 className="font-semibold text-blue-950">AI 요약</h3>
        {analysis && (
          <span className="text-xs text-slate-700">
            {analysis.deadline_status === 'confirmed' && analysis.deadline && (analysis.category === 'reply' ? '회신 기한 ' : '기한 ')}
            <Deadline analysis={analysis}
              overdue={analysis.deadline_status === 'confirmed' && !!analysis.deadline && analysis.deadline < today} />
          </span>
        )}
      </div>
      {analysis ? (
        <>
          <p className="break-words">{brief}</p>
          <details key={mail.id} className="text-xs">
            <summary className="w-fit cursor-pointer rounded text-blue-800 hover:underline focus-visible:outline-blue-700">요약 근거와 다음 할 일</summary>
            <MailAnalysisDetails analysis={analysis} brief={brief} />
          </details>
        </>
      ) : <p className="text-xs text-slate-600">{analysisState(mail)}</p>}
    </section>
  );
}

function SummaryCard({ row, today, showSender, status, selected, onSelect }: {
  row: SummaryRow; today: string; showSender: boolean; status?: WorkStatus; selected: boolean;
  onSelect: () => void;
}) {
  const analysis = availableAnalysis(row.mail);
  const line = analysis ? briefAnalysis(analysis) : `${analysisState(row.mail)} · 원문 보기`;
  const description = `mail-summary-${row.mail.id}`;
  return (
    <button type="button" aria-label={row.mail.subject} aria-describedby={description} aria-expanded={selected}
      onClick={onSelect} className="grid w-full min-w-0 items-center gap-x-3 rounded-md px-1 py-1 text-left text-sm hover:bg-blue-50 focus-visible:outline-blue-700 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)_auto]">
      <span className="flex min-w-0 items-baseline gap-x-2 lg:block">
        <span className="block min-w-0 truncate font-semibold text-blue-900" title={row.mail.subject}>{row.mail.subject}</span>
        <span className="block shrink-0 text-xs text-slate-600">
          안 읽음{status && ` · ${mailWorkLabels[status]}`}
        </span>
      </span>
      <span id={description} className="min-w-0 text-xs text-slate-800 sm:text-sm">
        {showSender && <span className="block truncate text-xs text-slate-600" title={row.mail.sender}>{row.mail.sender}</span>}
        <span className="line-clamp-2 break-words lg:line-clamp-1" title={line}>{line}</span>
      </span>
      {analysis && <span className="block break-words text-xs text-slate-700 lg:whitespace-nowrap">
        {analysis.category === 'reply' ? '회신 기한' : '기한'} · <Deadline analysis={analysis} soon={row.urgent}
          overdue={analysis.deadline_status === 'confirmed' && !!analysis.deadline && analysis.deadline < today} />
      </span>}
    </button>
  );
}

function MailAnalysisDetails({ analysis, brief }: { analysis: Analysis; brief: string }) {
  return (
    <dl className="mt-1.5 space-y-1.5 break-words border-t border-blue-100 pt-1.5">
      <div><dt className="inline font-semibold">분류</dt><dd className="inline"> · {categories[analysis.category]} · 시연 분석 결과</dd></div>
      {analysis.summary !== brief && <div><dt className="font-semibold">전체 요약</dt><dd>{analysis.summary}</dd></div>}
      {analysis.request && analysis.request !== brief && (
        <div><dt className="font-semibold">요청 사항</dt><dd>{analysis.request}</dd></div>
      )}
      {analysis.request_evidence && <div><dt className="sr-only">요청 근거</dt><dd><Evidence label="요청 근거" quote={analysis.request_evidence} /></dd></div>}
      {analysis.deadline_evidence && <div><dt className="sr-only">기한 근거</dt><dd><Evidence label="기한 근거" quote={analysis.deadline_evidence} /></dd></div>}
      {analysis.next_action && <div><dt className="font-semibold">다음 할 일 제안</dt><dd>{analysis.next_action}</dd></div>}
    </dl>
  );
}

function Evidence({ label, quote }: { label: string; quote: string }) {
  return (
    <blockquote className="mt-1 rounded-r-md border-l-2 border-blue-300 bg-white px-3 py-2 text-slate-700">
      <p className="text-xs font-semibold text-slate-600">원문 인용 · {label}</p>
      <p className="mt-0.5 break-words">“{quote}”</p>
    </blockquote>
  );
}
