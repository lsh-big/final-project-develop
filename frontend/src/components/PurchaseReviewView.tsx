import { ArrowUpRight, ChartNoAxesCombined, Check, ClipboardCheck, FileSpreadsheet, FileText, History, Inbox, Receipt, Trash2, type LucideIcon } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, MouseEvent, ReactNode } from 'react';
import { apiUrl, downloadWorkbook, request } from '../services/api.js';
import { MobileReceiptUpload } from './MobileReceiptUpload';
import { EvidenceComparison, matchStatusLabels } from './EvidenceComparison';

type DocumentType = 'receipt' | 'tax_invoice' | 'trade_statement' | 'unknown';
type Status = 'review' | 'approved' | 'trashed';
type StatementItem = { name: string; quantity: number | null; unit_price: number | null };
type StatementItemDraft = { key: number; name: string; quantity: string; unit_price: string };
type Fields = {
  vendor?: string;
  total_amount?: number | null;
  items?: StatementItem[];
  [name: string]: string | number | StatementItem[] | null | undefined;
};
type FieldSpec = { name: string; label: string; format?: 'amount' | 'date' | 'datetime-local'; optional?: boolean };
type PurchaseKind = 'document' | 'receipt' | 'statement';
type RecordSummary = {
  id: string;
  filename: string;
  kind: PurchaseKind;
  document_type: DocumentType;
  status: Status;
  fields: Fields;
  created_at: string;
  match_status?: keyof typeof matchStatusLabels;
};
type RecordDetail = RecordSummary & {
  page_count: number;
  draft_saved_at: string | null;
  confirmed_type: boolean;
  extraction: { text: string; method: string; warning: string | null; revision?: number };
};
type Filters = { start: string; end: string; vendor: string; document_type: string; status: string };

const typeLabels = {
  receipt: '영수증',
  tax_invoice: '매입 세금계산서',
  trade_statement: '거래명세서',
  unknown: '유형 확인 필요',
};
const statusLabels = { review: '검토 대기', approved: '승인 완료', trashed: '휴지통' };
const extractionLabels: Record<string, string> = {
  ocr: 'OCR 결과',
  pdf_text: 'PDF 텍스트',
  'ocr+pdf_text': 'PDF 텍스트와 OCR 결과',
};
const initialFilters = { start: '', end: '', vendor: '', document_type: '', status: 'approved' };
const control = 'w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 disabled:bg-slate-100';
const primary = 'rounded-lg bg-blue-700 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-800 disabled:opacity-50';
const secondary = 'rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-semibold text-slate-800 hover:bg-slate-50 disabled:opacity-50';

function recordPath(row: RecordSummary) {
  return `${purchaseApi(recordKind(row))}/${row.id}`;
}

function recordKind(row: RecordSummary): PurchaseKind {
  return row.kind;
}

const endpoints: Record<PurchaseKind, string> = { document: 'documents', receipt: 'receipts', statement: 'statements' };
const documentTypes: Record<PurchaseKind, DocumentType> = {
  document: 'tax_invoice', receipt: 'receipt', statement: 'trade_statement',
};

function purchaseApi(kind: PurchaseKind) {
  return `/api/v1/${endpoints[kind]}`;
}

const uploadTypes = {
  document: { label: typeLabels.tax_invoice, format: 'PDF', icon: FileText },
  statement: { label: typeLabels.trade_statement, format: 'PDF', icon: FileSpreadsheet },
  receipt: { label: '구매 영수증', format: 'JPG·PNG', icon: Receipt },
} satisfies Record<PurchaseKind, { label: string; format: string; icon: LucideIcon }>;
const uploadKinds = Object.keys(uploadTypes) as PurchaseKind[];
const historyApi = purchaseApi('document');

function message(error: unknown) {
  return error instanceof Error ? error.message : '요청을 처리하지 못했습니다.';
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('ko-KR', {
    dateStyle: 'short', timeStyle: 'short', timeZone: 'Asia/Seoul',
  }).format(new Date(value));
}

type Review = {
  record: RecordDetail;
  draft: Record<string, string>;
  page: number;
  confirmedType: boolean;
  items: StatementItemDraft[];
};

function missingFields(kind: PurchaseKind, draft: Review['draft']) {
  return fieldSpecs[kind].filter(({ name, optional }) => !optional && !(draft[name] ?? '').trim());
}

function reviewInputId(name: string) {
  return `purchase-review-${name}`;
}

function firstMissingItemInput(items: StatementItemDraft[]) {
  for (const [index, item] of items.entries()) {
    const field = statementItemFields.find(([name]) => !item[name].trim());
    if (field) return reviewInputId(`item-${index}-${field[0]}`);
  }
}

function uploadReviewChecks(review: Review) {
  if (review.record.status !== 'review') return [];
  const checks = missingFields(review.record.kind, review.draft).map(({ name, label }) => ({ name, label }));
  if (review.record.kind === 'statement' && firstMissingItemInput(review.items)) {
    checks.push({ name: 'items', label: '품목 정보' });
  }
  if (review.record.document_type === 'unknown' && !review.confirmedType) {
    checks.unshift({ name: 'confirmed_type', label: '증빙 유형' });
  }
  return checks;
}

const statementItemLimit = 200;
let nextStatementItemKey = 0;

function itemDraft(item?: StatementItem): StatementItemDraft {
  return {
    key: nextStatementItemKey++,
    name: item?.name ?? '',
    quantity: String(item?.quantity ?? ''),
    unit_price: String(item?.unit_price ?? ''),
  };
}

function draftNumber(value: string) {
  return value.trim() ? Number(value) : null;
}

type UploadItem =
  | { filename: string; state: 'waiting' | 'uploading' }
  | { filename: string; state: 'failed'; error: string }
  | { filename: string; state: 'uploaded'; recordId: string; duplicate: boolean; duplicateReason?: string };

export type PurchaseReviewProps = {
  reviewCount: number | null;
  onReviewCountChange: () => void;
  onProcessingCountChange?: (count: number) => void;
  historyWindow?: boolean;
  initialStatus?: string | null;
  initialRecord?: string | null;
  initialKind?: string | null;
  reviewRevision?: number;
};

function PopupWindowLink({ href, children, icon: Icon, className = '' }: {
  href: string; children: ReactNode; icon?: LucideIcon; className?: string;
}) {
  function openWindow(event: MouseEvent<HTMLAnchorElement>) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const popup = window.open('about:blank', '_blank', 'popup,width=1250,height=850,resizable,scrollbars');
    if (popup) {
      event.preventDefault();
      popup.opener = null;
      popup.location.replace(event.currentTarget.href);
    }
  }
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" onClick={openWindow}
      className={Icon ? `inline-flex min-h-10 items-center gap-1.5 rounded-md px-2 text-sm font-medium text-slate-700 hover:bg-slate-100 hover:text-slate-950 focus-visible:outline-blue-700 ${className}` : secondary}>
      {Icon && <Icon aria-hidden="true" className="size-4 shrink-0 text-slate-500" />}
      {children}
      {Icon && <><ArrowUpRight aria-hidden="true" className="size-3 shrink-0 text-slate-500" /><span className="sr-only"> (새 창)</span></>}
    </a>
  );
}

export function PurchaseReviewView({
  reviewCount, onReviewCountChange, onProcessingCountChange, historyWindow = false,
  initialStatus, initialRecord, initialKind, reviewRevision = 0,
}: PurchaseReviewProps) {
  const [kind, setKind] = useState<PurchaseKind>('document');
  const [reviews, setReviews] = useState<Record<string, Review>>({});
  const [selected, setSelected] = useState<Partial<Record<PurchaseKind, string>>>({});
  const selectedId = selected[kind];
  const { record, draft = {}, page = 1, confirmedType, items = [] } = (selectedId && reviews[selectedId]) || {};
  const [batches, setBatches] = useState<Partial<Record<PurchaseKind, UploadItem[]>>>({});
  const [mobileProcessing, setMobileProcessing] = useState(false);
  const batch = batches[kind] ?? [];
  const desktopProcessing = Object.values(batches).some((items) => items?.some(({ state }) => state === 'uploading'));
  const processingCount = Number(desktopProcessing) + Number(mobileProcessing);
  useEffect(() => {
    onProcessingCountChange?.(processingCount);
  }, [onProcessingCountChange, processingCount]);
  useEffect(() => () => onProcessingCountChange?.(0), [onProcessingCountChange]);
  const batchRequest = useRef<AbortController | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const [draggingFiles, setDraggingFiles] = useState(false);
  const isReceipt = kind === 'receipt';
  const uploadType = uploadTypes[kind];
  const recordSpecs = fieldSpecs[kind];
  const initialHistoryFilters = {
    ...initialFilters,
    status: initialStatus === 'review' || initialStatus === 'trashed' ? initialStatus : 'approved',
  };
  const [filters, setFilters] = useState<Filters>(initialHistoryFilters);
  const [activeFilters, setActiveFilters] = useState<Filters>(initialHistoryFilters);
  const [rows, setRows] = useState<RecordSummary[]>([]);
  const [version, setVersion] = useState(0);
  const [busy, setBusy] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [trashCount, setTrashCount] = useState<number | null>(null);
  const [historyError, setHistoryError] = useState('');
  const [historyNotice, setHistoryNotice] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [conflictId, setConflictId] = useState('');
  const historySection = useRef<HTMLElement>(null);
  const recordSection = useRef<HTMLDivElement>(null);
  const [reviewFocus, setReviewFocus] = useState<{ recordId: string; field: string }>();
  const receiveMobileReceipt = useCallback(() => {
    setVersion((current) => current + 1);
    onReviewCountChange();
  }, [onReviewCountChange]);

  const query = new URLSearchParams();
  Object.entries(activeFilters).forEach(([name, value]) => {
    if (value.trim()) query.set(name, value.trim());
  });
  const queryString = query.toString();
  const trashLabel = trashCount === null ? '휴지통' : trashCount === 0 ? '휴지통 · 비어 있음' : `휴지통 · ${trashCount}건`;
  const emptyHistoryMessage = activeFilters.status !== 'trashed'
    ? '조회된 처리 기록이 없습니다.'
    : trashCount === 0 || !Object.entries(activeFilters).some(([name, value]) => name !== 'status' && value.trim())
      ? '휴지통이 비어 있습니다.'
      : '검색 조건에 맞는 휴지통 항목이 없습니다.';

  useEffect(() => {
    const abort = new AbortController();
    request(`${historyApi}/trash/count`, { signal: abort.signal })
      .then(({ count }: { count: number }) => { if (!abort.signal.aborted) setTrashCount(count); })
      .catch(() => { if (!abort.signal.aborted) setTrashCount(null); });
    return () => abort.abort();
  }, [reviewRevision]);

  useEffect(() => {
    if (!historyWindow) return;
    const abort = new AbortController();
    setHistoryLoading(true);
    setHistoryError('');
    request(`${historyApi}/history?${queryString}`, { signal: abort.signal })
      .then((result: RecordSummary[]) => setRows(result))
      .catch((problem: unknown) => {
        if (!abort.signal.aborted) {
          setRows([]);
          setHistoryError(message(problem));
        }
      })
      .finally(() => {
        if (!abort.signal.aborted) setHistoryLoading(false);
      });
    return () => abort.abort();
  }, [historyWindow, queryString, reviewRevision, version]);

  useEffect(() => () => batchRequest.current?.abort(), []);

  useEffect(() => {
    if (busy || !reviewFocus || record?.id !== reviewFocus.recordId) return;
    const id = reviewFocus.field === 'items' ? firstMissingItemInput(items) : reviewInputId(reviewFocus.field);
    const input = id ? document.getElementById(id) : null;
    if (record.status === 'review' && input instanceof HTMLInputElement) {
      input.scrollIntoView({ block: 'center' });
      input.focus({ preventScroll: true });
    } else {
      recordSection.current?.scrollIntoView({ block: 'center' });
    }
    setReviewFocus(undefined);
  }, [busy, items, record, reviewFocus]);

  function show(next: RecordDetail, select = true, preserveDraft = false) {
    const nextKind = recordKind(next);
    const incomingItems = nextKind === 'statement' ? (next.fields.items?.length ? next.fields.items.map(itemDraft) : [itemDraft()]) : [];
    setReviews((current) => {
      const previous = current[next.id];
      if (preserveDraft && previous?.record.status === next.status
          && previous.record.kind === next.kind && previous.record.extraction.revision === next.extraction.revision
          && previous.record.draft_saved_at === next.draft_saved_at) {
        return { ...current, [next.id]: { ...previous, record: next } };
      }
      return { ...current, [next.id]: {
        record: next,
        draft: Object.fromEntries(fieldSpecs[nextKind].map(({ name }) => [name, String(next.fields[name] ?? '')])),
        page: 1,
        confirmedType: next.confirmed_type,
        items: incomingItems,
      } };
    });
    if (select) {
      setSelected((current) => ({ ...current, [nextKind]: next.id }));
      setKind(nextKind);
    }
  }

  function updateReview(changes: Partial<Omit<Review, 'record'>>) {
    if (selectedId) setReviews((current) => ({
      ...current, [selectedId]: { ...current[selectedId], ...changes },
    }));
  }

  function selectTab(next: PurchaseKind) {
    setKind(next);
    setError('');
    setNotice('');
    setConflictId('');
  }

  async function upload(files: File[]) {
    if (!files.length || batchRequest.current) return;
    const abort = new AbortController();
    batchRequest.current = abort;
    const items: UploadItem[] = files.map(({ name }) => ({ filename: name, state: 'waiting' }));
    let firstRecord: RecordDetail | undefined;
    setBatches((current) => ({ ...current, [kind]: [...items] }));
    setBusy(true);
    setError('');
    setNotice('');
    setConflictId('');
    function updateItem(index: number, item: UploadItem) {
      items[index] = item;
      const snapshot = [...items];
      setBatches((current) => ({ ...current, [kind]: snapshot }));
    }

    try {
      for (const [index, file] of files.entries()) {
        const filename = file.name;
        updateItem(index, { filename, state: 'uploading' });
        try {
          const params = new URLSearchParams({ filename });
          const send = () => request(`${purchaseApi(kind)}/uploads?${params}`, {
            method: 'POST', headers: { 'Content-Type': file.type || 'application/octet-stream' }, body: file, signal: abort.signal,
          });
          const result: { record: RecordDetail; duplicate: boolean; duplicate_reason?: string } = await send().catch((problem: unknown) => {
            if ((problem as { code?: string })?.code !== 'suspected_duplicate' || abort.signal.aborted) throw problem;
            if (!window.confirm(`중복 의심 · ${filename}\n${message(problem)}\n별도 거래로 등록할까요?`)) {
              throw new Error('중복 의심으로 등록을 취소했습니다.');
            }
            params.set('confirm_suspected', 'true');
            return send();
          });
          if (abort.signal.aborted) return;
          updateItem(index, { filename, state: 'uploaded', recordId: result.record.id, duplicate: result.duplicate,
            duplicateReason: result.duplicate_reason });
          show(result.record, false, true);
          if (result.record.kind === kind || items.length === 1) firstRecord ??= result.record;
        } catch (problem) {
          if (abort.signal.aborted) return;
          updateItem(index, { filename, state: 'failed', error: message(problem) });
        }
      }
      if (firstRecord) {
        const firstId = firstRecord.id;
        const nextKind = firstRecord.kind;
        setSelected((current) => ({ ...current, [nextKind]: firstId }));
        if (nextKind !== kind) {
          setKind(nextKind);
          setBatches((current) => ({ ...current, [kind]: undefined, [nextKind]: [...items] }));
        }
        setVersion((current) => current + 1);
        onReviewCountChange();
      } else if (items.some((item) => item.state === 'uploaded')) {
        onReviewCountChange();
      }
      if (items.length === 1) {
        const [item] = items;
        if (item.state === 'failed') setError(item.error);
        if (item.state === 'uploaded' && firstRecord && firstRecord.kind !== kind) {
          setNotice(`${typeLabels[firstRecord.document_type]} 유형으로 열린 증빙입니다.`);
        } else if (item.state === 'uploaded' && item.duplicate) {
          setNotice(firstRecord?.status === 'trashed'
            ? '중복 증빙이 휴지통에 있습니다. 기존 기록을 복원할 수 있습니다.'
            : item.duplicateReason === 'approval_number' ? '같은 승인번호의 증빙이 이미 있습니다.' : '동일 파일이 이미 있습니다.');
        }
      }
    } finally {
      batchRequest.current = null;
      if (!abort.signal.aborted) setBusy(false);
    }
  }

  async function open(path: string, field?: string) {
    setBusy(true);
    setError('');
    setNotice('');
    setConflictId('');
    if (historyWindow) setHistoryError('');
    try {
      const next: RecordDetail = await request(path);
      show(next, true, true);
      if (field) setReviewFocus({ recordId: next.id, field });
      else if (historyWindow) requestAnimationFrame(() => recordSection.current?.scrollIntoView({ block: 'start' }));
    } catch (problem) {
      if (historyWindow) setHistoryError(message(problem));
      else setError(message(problem));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (!historyWindow || !initialRecord || !/^[a-f0-9]{32}$/.test(initialRecord)
        || (initialKind !== 'document' && initialKind !== 'statement')) return;
    const abort = new AbortController();
    setBusy(true);
    void request(`${purchaseApi(initialKind)}/${initialRecord}`, { signal: abort.signal })
      .then((next: RecordDetail) => {
        if (abort.signal.aborted) return;
        show(next);
        requestAnimationFrame(() => recordSection.current?.scrollIntoView({ block: 'start' }));
      })
      .catch((problem: unknown) => { if (!abort.signal.aborted) setError(message(problem)); })
      .finally(() => { if (!abort.signal.aborted) setBusy(false); });
    return () => abort.abort();
  }, [historyWindow, initialKind, initialRecord]);

  async function saveReview(approve: boolean) {
    if (!record || busy || record.status !== 'review' || (approve && !confirmedType)) return;
    setBusy(true);
    setError('');
    setNotice('');
    setConflictId('');
    const fields: Record<string, string | number | null | StatementItem[]> = Object.fromEntries(recordSpecs.map(({ name, format }) => {
      const value = (draft[name] ?? '').trim();
      return [name, format === 'amount' ? draftNumber(value) : value];
    }));
    if (kind === 'statement') {
      fields.items = items.map(({ name, quantity, unit_price }) => ({
        name: name.trim(), quantity: draftNumber(quantity), unit_price: draftNumber(unit_price),
      }));
    }
    try {
      const saved = await request(`${recordPath(record)}/${approve ? 'approve' : 'draft'}`, {
        method: approve ? 'POST' : 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(approve
          ? { fields, document_type: documentTypes[kind] }
          : { fields, confirmed_type: confirmedType }),
      });
      show(saved);
      setNotice(approve ? '승인 완료' : '임시저장 완료');
      setVersion((current) => current + 1);
      onReviewCountChange();
    } catch (problem) {
      setError(message(problem));
      setConflictId((problem as { recordId?: string } | null)?.recordId ?? '');
    } finally {
      setBusy(false);
    }
  }

  function approve(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void saveReview(true);
  }

  async function changeTrashStatus(restore: boolean, row?: RecordSummary) {
    const target = row ?? record;
    if (!target || busy || (target.status === 'trashed') !== restore) return;
    if (!restore && !window.confirm(`‘${target.filename}’을(를) 휴지통으로 이동할까요? 처리 이력과 엑셀에서 제외되며 휴지통에서 복원할 수 있습니다.`)) return;
    setBusy(true);
    setError('');
    setNotice('');
    setConflictId('');
    setHistoryNotice('');
    if (row) setHistoryError('');
    try {
      const path = recordPath(target);
      const saved: RecordDetail = await request(restore ? `${path}/restore` : path, { method: restore ? 'POST' : 'DELETE' });
      show(saved, !row);
      const success = restore ? `${saved.status === 'approved' ? '승인 완료 목록' : '검토 대기함'}으로 복원했습니다.` : '휴지통으로 이동했습니다.';
      if (row) setHistoryNotice(success);
      else setNotice(success);
      setVersion((current) => current + 1);
      onReviewCountChange();
    } catch (problem) {
      if (row) setHistoryError(message(problem));
      else setError(message(problem));
    } finally {
      setBusy(false);
    }
  }

  function openHistory(status: Status) {
    setHistoryNotice('');
    const nextFilters = { ...initialFilters, status };
    setFilters(nextFilters);
    setActiveFilters(nextFilters);
    setVersion((current) => current + 1);
    historySection.current?.scrollIntoView({ block: 'start' });
  }

  async function permanentlyDelete(row?: RecordSummary) {
    if (busy) return;
    const prompt = row
      ? `‘${row.filename}’의 원본과 기록을 영구 삭제할까요? 삭제 후 복원할 수 없습니다.`
      : '검색 조건과 관계없이 휴지통에 있는 모든 원본과 기록을 영구 삭제할까요? 삭제 후 복원할 수 없습니다.';
    if (!window.confirm(prompt)) return;

    setBusy(true);
    setHistoryError('');
    setHistoryNotice('');
    try {
      const result: { deleted: number } | null = await request(
        row ? `${recordPath(row)}/permanent` : `${historyApi}/trash`,
        { method: 'DELETE' },
      );
      const removed = new Set(row ? [row.id] : Object.values(reviews)
        .filter(({ record: cached }) => cached.status === 'trashed')
        .map(({ record: cached }) => cached.id));
      setReviews((current) => Object.fromEntries(Object.entries(current).filter(([id]) => !removed.has(id))));
      setRows((current) => current.filter((item) => row ? item.id !== row.id : item.status !== 'trashed'));
      setHistoryNotice(row ? '기록을 영구 삭제했습니다.' : `휴지통에서 ${result?.deleted ?? 0}건을 영구 삭제했습니다.`);
      setVersion((current) => current + 1);
      onReviewCountChange();
    } catch (problem) {
      setHistoryError(message(problem));
    } finally {
      setBusy(false);
    }
  }

  async function exportHistory() {
    setExporting(true);
    setHistoryError('');
    try {
      await downloadWorkbook(`${historyApi}/export?${queryString}`);
    } catch (problem) {
      setHistoryError(message(problem));
    } finally {
      setExporting(false);
    }
  }

  const original = record ? apiUrl(`${recordPath(record)}/original`) : '';
  const missingRequired = missingFields(kind, draft).length > 0
    || (kind === 'statement' && Boolean(firstMissingItemInput(items)));

  return (
    <section id={historyWindow ? undefined : 'purchase-evidence'} aria-labelledby="purchase-review-heading"
      tabIndex={historyWindow ? undefined : -1}
      className={`space-y-6 ${historyWindow ? '' : 'scroll-mt-48 border-t border-slate-200 pt-6 xl:scroll-mt-32'}`}>
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          {historyWindow
            ? <h1 id="purchase-review-heading" className="text-2xl font-bold">처리 이력</h1>
            : <h2 id="purchase-review-heading" className="text-xl font-bold">구매 증빙 처리</h2>}
          {!historyWindow && <p className="text-sm text-slate-600">구매 증빙을 업로드하고 원본과 추출값을 대조해 승인합니다.</p>}
        </div>
        {historyWindow ? (
          <div className="flex flex-wrap gap-2">
            <button className={secondary} type="button" onClick={() => openHistory('review')}>
              검토 대기함{reviewCount !== null && ` · ${reviewCount}건`}
            </button>
            <PopupWindowLink href="?view=purchase-inbox">미처리 증빙함 ↗</PopupWindowLink>
            <button className={secondary} type="button" onClick={() => openHistory('approved')}>승인 완료</button>
            <PopupWindowLink href="?view=purchase-prices">매입 단가 이력 ↗</PopupWindowLink>
            <button className={secondary} type="button" onClick={() => openHistory('trashed')}>{trashLabel}</button>
          </div>
        ) : (
          <nav aria-labelledby="purchase-shortcuts-heading" className="flex w-full flex-wrap items-center gap-x-3 rounded-lg border border-slate-200 bg-white px-2 py-1">
            <h3 id="purchase-shortcuts-heading" className="w-full px-2 pt-1 text-xs font-semibold text-slate-600 lg:w-auto lg:pt-0">증빙함 바로가기</h3>
            <PopupWindowLink href="?view=purchase-history&status=review" icon={ClipboardCheck}>
              검토 대기함
              {reviewCount !== null && (
                <span className={`whitespace-nowrap rounded-full px-1.5 text-xs font-semibold tabular-nums ${reviewCount > 0 ? 'bg-blue-100 text-blue-800' : 'bg-slate-100 text-slate-600'}`}>
                  {reviewCount}건
                </span>
              )}
            </PopupWindowLink>
            <PopupWindowLink href="?view=purchase-inbox" icon={Inbox}>미처리 증빙함</PopupWindowLink>
            <PopupWindowLink href="?view=purchase-history&status=approved" icon={History}>처리 이력</PopupWindowLink>
            <PopupWindowLink href="?view=purchase-prices" icon={ChartNoAxesCombined}>매입 단가 이력</PopupWindowLink>
            <PopupWindowLink href="?view=purchase-history&status=trashed" icon={Trash2} className="sm:ml-auto">{trashLabel}</PopupWindowLink>
          </nav>
        )}
      </header>
      {!historyWindow && <>
        <div className="space-y-2">
          <h3 id="purchase-types-heading" className="text-sm font-semibold text-slate-800">업로드할 증빙 선택</h3>
          <div role="group" aria-labelledby="purchase-types-heading" className="grid gap-2 sm:grid-cols-3 sm:gap-3">
            {uploadKinds.map((option) => {
              const { label, format, icon: Icon } = uploadTypes[option];
              const active = kind === option;
              return (
                <button key={option} type="button" disabled={busy} aria-pressed={active} onClick={() => selectTab(option)}
                  className={`flex min-h-16 min-w-0 items-center gap-2 rounded-xl border px-3 py-2 text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-700 disabled:opacity-50 sm:min-h-20 sm:px-4 ${active ? 'border-blue-600 bg-blue-50 text-blue-950' : 'border-slate-200 bg-white text-slate-800 hover:bg-slate-50'}`}>
                  <Icon aria-hidden="true" className={`size-5 shrink-0 ${active ? 'text-blue-700' : 'text-slate-500'}`} />
                  <span className="flex min-w-0 flex-1 flex-wrap items-center gap-x-2 gap-y-0.5">
                    <span className="text-sm font-semibold">{label}</span>
                    <span className={`whitespace-nowrap rounded px-1.5 text-xs font-medium ${active ? 'bg-blue-100 text-blue-800' : 'bg-slate-100 text-slate-600'}`}>{format}</span>
                  </span>
                  {active && <Check aria-hidden="true" className="size-4 shrink-0 text-blue-700" />}
                </button>
              );
            })}
          </div>
        </div>
        <section className="rounded-xl border border-slate-200 bg-white p-4 sm:p-5" aria-labelledby="purchase-upload-heading">
          <div className={`space-y-3 rounded-lg border-2 border-dashed p-4 sm:p-5 ${
            draggingFiles && !busy ? 'border-blue-600 bg-blue-50' : 'border-slate-300 bg-slate-50'
          }`}
            onDragOver={(event) => {
              if (!event.dataTransfer.types.includes('Files')) return;
              event.preventDefault();
              event.dataTransfer.dropEffect = busy ? 'none' : 'copy';
              if (!busy) setDraggingFiles(true);
            }}
            onDragLeave={(event) => {
              if (!(event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget))) {
                setDraggingFiles(false);
              }
            }}
            onDrop={(event) => {
              event.preventDefault();
              setDraggingFiles(false);
              if (!busy) void upload(Array.from(event.dataTransfer.files));
            }}>
            <div className="space-y-1">
              <h2 id="purchase-upload-heading" className="text-base font-semibold">
                {uploadType.label} 불러오기
              </h2>
              <p className="text-sm text-slate-600">파일을 끌어놓거나 버튼으로 선택하세요. 선택하면 바로 인식이 시작됩니다.</p>
            </div>
            <button type="button" className={`${primary} min-h-11 w-full sm:w-auto`} disabled={busy}
              aria-describedby="purchase-upload-limit" onClick={() => fileInput.current?.click()}>
              {uploadType.label} {isReceipt ? '이미지' : 'PDF'} 선택
            </button>
            <p id="purchase-upload-limit" className="text-xs text-slate-600">
              {isReceipt ? 'JPG·PNG · 여러 파일 선택 가능 · 파일당 10 MB 이하'
                : 'PDF · 여러 파일 선택 가능 · 파일당 12쪽, 10 MB 이하'}
            </p>
          </div>
          <input ref={fileInput} id="purchase-upload" type="file" hidden multiple
            accept={isReceipt ? '.jpg,.jpeg,.png' : '.pdf'} disabled={busy}
            onChange={(event) => {
              const files = Array.from(event.target.files ?? []);
              event.target.value = '';
              void upload(files);
            }} />
          <div hidden={!isReceipt}>
            <MobileReceiptUpload disabled={busy} onReceived={receiveMobileReceipt} onProcessingChange={setMobileProcessing}
              onOpen={(id) => void open(`${purchaseApi('receipt')}/${id}`)} />
          </div>
          {busy && !batch.some(({ state }) => state === 'waiting' || state === 'uploading')
            && <p className="mt-3 text-sm text-slate-600" role="status">처리 중입니다…</p>}
          {notice && <p className="mt-3 text-sm text-emerald-800" role="status">{notice}</p>}
          {error && <p className="mt-3 text-sm text-red-800" role="alert">{error}</p>}
          {conflictId && record && (
            <button className={`${secondary} mt-2`} disabled={busy}
              onClick={() => void open(`${purchaseApi(kind)}/${conflictId}`)}>
              기존 승인 기록 열기
            </button>
          )}
        </section>

        {batch.length > 0 && (
          <section className="space-y-3 rounded-xl border border-slate-200 bg-white p-5" aria-label="파일별 업로드 결과">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 className="font-semibold">파일별 업로드 결과</h2>
              <span className="text-sm text-slate-600" role="status">
                {batch.filter(({ state }) => state === 'uploaded' || state === 'failed').length} / {batch.length}개 처리 완료
              </span>
            </div>
            <ul className="divide-y divide-slate-100">
              {batch.map((item, index) => {
                const review = item.state === 'uploaded' ? reviews[item.recordId] : undefined;
                const uploaded = review?.record;
                const checks = review ? uploadReviewChecks(review) : [];
                return (
                  <li key={index} className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
                    <div className="min-w-0">
                      <p className="break-all font-medium">{item.filename}</p>
                      <p className={item.state === 'failed' ? 'text-red-800' : 'text-slate-600'}>
                        {item.state === 'failed' ? `실패 · ${item.error}`
                          : item.state === 'uploading' ? '인식 중'
                          : uploaded ? `${item.state === 'uploaded' && item.duplicate
                            ? item.duplicateReason === 'approval_number' ? '승인번호 중복 · ' : '동일 파일 · '
                            : '불러오기 완료 · '}${statusLabels[uploaded.status]}`
                          : '업로드 대기'}
                      </p>
                      {!!checks.length && uploaded && (
                        <div className="mt-1 flex flex-wrap items-baseline gap-1.5">
                          <span className="text-xs font-semibold text-amber-900">확인 필요</span>
                          {checks.map(({ name, label }) => (
                            <button key={name} type="button" disabled={busy} aria-label={`${item.filename} ${label} 확인`}
                              className="rounded bg-amber-50 px-2 py-1 text-xs font-medium text-amber-950 underline underline-offset-2 hover:bg-amber-100 focus-visible:outline-blue-700 disabled:opacity-50"
                              onClick={() => void open(recordPath(uploaded), name)}>{label}</button>
                          ))}
                        </div>
                      )}
                    </div>
                    {uploaded && (
                      <button type="button" className={selectedId === uploaded.id ? primary : secondary} disabled={busy}
                        aria-label={`${item.filename} ${item.state === 'uploaded' && item.duplicate ? '기존 증빙 보기' : '검토 열기'}`}
                        aria-pressed={selectedId === uploaded.id}
                        onClick={() => void open(recordPath(uploaded))}>
                        {item.state === 'uploaded' && item.duplicate ? '기존 증빙 보기' : selectedId === uploaded.id ? '선택됨' : '검토 열기'}
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        )}
      </>}

      {historyWindow && (
        <section ref={historySection} className="scroll-mt-32 space-y-4 rounded-xl border border-slate-200 bg-white p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="font-semibold">{activeFilters.status ? statusLabels[activeFilters.status as Status] : '전체 기록'}</h2>
            {activeFilters.status === 'trashed' ? (
              <button className={`${secondary} border-red-200 text-red-800 hover:bg-red-50`} type="button"
                disabled={busy || historyLoading || trashCount === 0} onClick={() => void permanentlyDelete()}>
                전체 삭제하기
              </button>
            ) : (
              <button className={secondary} disabled={exporting || historyLoading} onClick={() => void exportHistory()}>엑셀 다운로드</button>
            )}
          </div>
          <form className="grid items-end gap-3 sm:grid-cols-3 lg:grid-cols-[1fr_1fr_1fr_1fr_1fr_auto]"
            onSubmit={(event) => {
              event.preventDefault();
              setHistoryNotice('');
              setActiveFilters({ ...filters });
              setVersion((current) => current + 1);
            }}>
            {(['start', 'end', 'vendor'] as const).map((name) => (
              <label key={name} className="space-y-1 text-sm font-medium">
                <span>{{ start: '등록 시작일', end: '등록 종료일', vendor: '거래처' }[name]}</span>
                <input className={control} type={name === 'vendor' ? 'search' : 'date'} value={filters[name]}
                  onChange={(event) => setFilters({ ...filters, [name]: event.target.value })} />
              </label>
            ))}
            <label className="space-y-1 text-sm font-medium">
              <span>문서 유형</span>
              <select className={control} value={filters.document_type} onChange={(event) => setFilters({ ...filters, document_type: event.target.value })}>
                <option value="">전체</option>
                {Object.entries(typeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label className="space-y-1 text-sm font-medium">
              <span>처리 상태</span>
              <select className={control} value={filters.status} onChange={(event) => setFilters({ ...filters, status: event.target.value })}>
                <option value="">전체</option>
                {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <button className={primary} type="submit">조회</button>
          </form>
          {historyNotice && <p className="text-sm text-emerald-800" role="status">{historyNotice}</p>}
          {historyError && <p className="text-sm text-red-800" role="alert">{historyError}</p>}
          {historyLoading && <p className="text-sm text-slate-600" role="status">이력을 불러오는 중입니다…</p>}
          {!historyLoading && !historyError && !rows.length && <p className="text-sm text-slate-600" role="status">{emptyHistoryMessage}</p>}
          {!!rows.length && (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[740px] text-left text-sm">
                <thead className="bg-slate-50">
                  <tr>{['원본 파일', '문서 유형', '거래처', '합계금액', '처리 상태', '증빙 대조', '등록일시', '삭제'].map((label) => <th key={label} scope="col" className="px-3 py-2">{label}</th>)}</tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id} className="border-t border-slate-100">
                      <td className="px-3 py-2"><button className="text-left font-semibold text-blue-700 underline" disabled={busy} onClick={() => void open(recordPath(row))}>{row.filename}</button></td>
                      <td className="px-3 py-2">{typeLabels[row.document_type]}</td>
                      <td className="px-3 py-2">{row.fields.vendor || '—'}</td>
                      <td className="px-3 py-2 tabular-nums">{row.fields.total_amount?.toLocaleString('ko-KR') ?? '—'}</td>
                      <td className="px-3 py-2">{statusLabels[row.status]}</td>
                      <td className="px-3 py-2">{row.match_status ? matchStatusLabels[row.match_status] : '—'}</td>
                      <td className="px-3 py-2">{formatDate(row.created_at)}</td>
                      <td className="px-3 py-2">
                        <button type="button" className="whitespace-nowrap rounded border border-red-200 px-2 py-1 text-sm font-semibold text-red-800 hover:bg-red-50 disabled:opacity-50"
                          aria-label={`${row.filename} 삭제`} disabled={busy}
                          onClick={() => void (row.status === 'trashed' ? permanentlyDelete(row) : changeTrashStatus(false, row))}>
                          삭제
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      <div ref={recordSection} className={record || (historyWindow && (error || notice)) ? 'space-y-3 scroll-mt-4' : 'hidden'}>
        {historyWindow && (
          <>
            {busy && <p className="text-sm text-slate-600" role="status">처리 중입니다…</p>}
            {notice && <p className="text-sm text-emerald-800" role="status">{notice}</p>}
            {error && <p className="text-sm text-red-800" role="alert">{error}</p>}
            {conflictId && record && (
              <button type="button" className={secondary} disabled={busy}
                onClick={() => void open(`${purchaseApi(kind)}/${conflictId}`)}>기존 승인 기록 열기</button>
            )}
          </>
        )}
        {record && (
          <div className="grid gap-4 lg:grid-cols-2">
            <section className="min-w-0 rounded-xl border border-slate-200 bg-white p-5">
              <div className="mb-3 flex items-center justify-between gap-3">
                <h2 className="font-semibold">원본 미리보기</h2>
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <a className="text-sm font-semibold text-blue-700 underline" href={original} target="_blank" rel="noreferrer">원본 열기</a>
                  {record.status === 'review' && (
                    <button type="button" className={`${secondary} text-red-800`} disabled={busy}
                      onClick={() => void changeTrashStatus(false)}>휴지통으로 이동</button>
                  )}
                  {record.status === 'trashed' && (
                    <button type="button" className={secondary} disabled={busy}
                      onClick={() => void changeTrashStatus(true)}>복원</button>
                  )}
                </div>
              </div>
              <p className="mb-3 break-all text-xs text-slate-600">{record.filename}</p>
              {record.document_type === 'receipt'
                ? <img className="max-h-[680px] w-full object-contain" src={original} alt="업로드한 영수증 원본" />
                : <>
                    <img className="max-h-[680px] w-full object-contain" alt={`${kind === 'statement' ? '거래명세서' : '세금계산서'} PDF 원본 ${page}쪽`}
                      src={apiUrl(`${recordPath(record)}/preview/${page}`)} />
                    {record.page_count > 1 && (
                      <div className="mt-3 flex items-center justify-between gap-2 text-sm">
                        <button className={secondary} disabled={page <= 1} onClick={() => updateReview({ page: page - 1 })}>이전 쪽</button>
                        <span>{page} / {record.page_count}</span>
                        <button className={secondary} disabled={page >= record.page_count} onClick={() => updateReview({ page: page + 1 })}>다음 쪽</button>
                      </div>
                    )}
                  </>}
            </section>
            <section className="min-w-0 rounded-xl border border-slate-200 bg-white p-5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="font-semibold">추출값 검토</h2>
                <span className="text-sm text-slate-600">{typeLabels[record.document_type]} · {statusLabels[record.status]}</span>
              </div>
              {record.extraction.warning && <p className="mt-3 rounded-lg bg-amber-50 p-3 text-sm text-amber-900">{record.extraction.warning}</p>}
              {record.extraction.text && (
                <details open={record.document_type === 'receipt'} className="my-3 rounded-lg bg-slate-50 p-3 text-sm">
                  <summary className="cursor-pointer font-medium">{extractionLabels[record.extraction.method] ?? '추출 텍스트'}</summary>
                  <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words text-xs">{record.extraction.text}</pre>
                </details>
              )}
              <form className="mt-4 space-y-3" onSubmit={approve}>
                {record.document_type === 'unknown' && (
                  <label className="flex items-center gap-2 text-sm font-medium">
                    <input id={reviewInputId('confirmed_type')} type="checkbox" checked={confirmedType} disabled={record.status !== 'review' || busy} onChange={(event) => updateReview({ confirmedType: event.target.checked })} />
                    원본이 {typeLabels[documentTypes[kind]]}임을 확인했습니다
                  </label>
                )}
                <fieldset className="grid gap-3 sm:grid-cols-2" disabled={record.status !== 'review' || busy}>
                  {recordSpecs.map(({ name, label, format, optional }) => (
                    <label key={name} className="space-y-1 text-sm font-medium">
                      <span>{label}</span>
                      <input id={reviewInputId(name)} className={control} value={draft[name] ?? ''} name={name}
                        type={format === 'amount' ? 'number' : format === 'date' ? 'date' : 'text'}
                        placeholder={format === 'datetime-local' ? 'YYYY-MM-DD HH:MM' : undefined}
                        required={!optional}
                        onChange={(event) => updateReview({ draft: { ...draft, [name]: event.target.value } })} />
                    </label>
                  ))}
                </fieldset>
                {kind === 'statement' && (
                  <StatementItems items={items} readOnly={record.status !== 'review'} busy={busy}
                    onChange={(nextItems) => updateReview({ items: nextItems })} />
                )}
                {record.status === 'review' && (
                  <>
                    {missingRequired &&
                      <p className="text-sm text-amber-900">빈 항목은 원본에서 확인해 입력해 주세요.</p>}
                    <div className="flex flex-wrap items-center gap-2">
                      <button className={primary} type="submit" disabled={busy || !confirmedType}>승인 및 저장</button>
                      <button className={secondary} type="button" disabled={busy} onClick={() => void saveReview(false)}>임시저장</button>
                      {record.draft_saved_at && <span className="text-xs text-slate-600">마지막 임시저장 {formatDate(record.draft_saved_at)}</span>}
                    </div>
                  </>
                )}
              </form>
              {kind !== 'receipt' && record.status !== 'trashed' && (
                <EvidenceComparison key={record.id} path={`${recordPath(record)}/matching`} kind={kind}
                  fields={record.fields} revision={version + reviewRevision} disabled={busy}
                  primaryClass={primary} secondaryClass={secondary} onChange={onReviewCountChange} />
              )}
            </section>
          </div>
        )}
      </div>


    </section>
  );
}

const receiptFields: FieldSpec[] = [
  { name: 'vendor', label: '거래처명' },
  { name: 'transaction_date', label: '거래일시', format: 'datetime-local' },
  { name: 'approval_number', label: '카드 승인번호' },
  { name: 'total_amount', label: '합계금액 (원)', format: 'amount' },
];

const invoiceFields: FieldSpec[] = [
  { name: 'vendor', label: '공급자 상호' },
  { name: 'business_number', label: '공급자 사업자등록번호' },
  { name: 'transaction_date', label: '작성일자', format: 'date' },
  { name: 'approval_number', label: '세금계산서 승인번호' },
  { name: 'item_summary', label: '품목 요약', optional: true },
  { name: 'supply_amount', label: '공급가액 (원)', format: 'amount' },
  { name: 'tax_amount', label: '세액 (원)', format: 'amount' },
  { name: 'total_amount', label: '합계금액 (원)', format: 'amount' },
];

const fieldSpecs: Record<PurchaseKind, FieldSpec[]> = {
  document: invoiceFields,
  receipt: receiptFields,
  statement: [
    { name: 'vendor', label: '거래처명' },
    { name: 'business_number', label: '공급자 사업자등록번호 (선택)', optional: true },
    { name: 'transaction_date', label: '납품일자', format: 'date' },
    { name: 'statement_number', label: '명세서 번호 (선택)', optional: true },
    { name: 'supply_amount', label: '공급가액 (원, 선택)', format: 'amount', optional: true },
    { name: 'tax_amount', label: '부가세 (원, 선택)', format: 'amount', optional: true },
    { name: 'total_amount', label: '합계금액 (원, 선택)', format: 'amount', optional: true },
  ],
};

const statementItemFields = [
  ['name', '품목명'],
  ['quantity', '수량'],
  ['unit_price', '단가 (원)'],
] as const;

function StatementItems({ items, readOnly, busy, onChange }: {
  items: StatementItemDraft[];
  readOnly: boolean;
  busy: boolean;
  onChange: (items: StatementItemDraft[]) => void;
}) {
  return (
    <fieldset className="space-y-2 rounded-lg border border-slate-200 p-3" disabled={readOnly || busy}>
      <legend className="px-1 text-sm font-semibold">품목별 수량과 단가</legend>
      {items.map((item, index) => (
        <div key={item.key} className="grid items-end gap-2 border-b border-slate-100 pb-2 sm:grid-cols-[2fr_1fr_1fr_auto]">
          {statementItemFields.map(([name, label]) => (
            <label key={name} className="space-y-1 text-sm">
              <span>{label}</span>
              <input id={reviewInputId(`item-${index}-${name}`)} className={control} aria-label={`${index + 1}번 품목 ${label}`}
                type={name === 'name' ? 'text' : 'number'} step={name === 'name' ? undefined : 'any'}
                maxLength={name === 'name' ? 200 : undefined} value={item[name]} required
                onChange={(event) => onChange(items.map((row) => row.key === item.key ? { ...row, [name]: event.target.value } : row))} />
            </label>
          ))}
          {!readOnly && (
            <button className={secondary} type="button" aria-label={`${index + 1}번 품목 삭제`} disabled={items.length === 1}
              onClick={() => onChange(items.filter((row) => row.key !== item.key))}>삭제</button>
          )}
        </div>
      ))}
      {!readOnly && (
        <button className={secondary} type="button" disabled={items.length >= statementItemLimit}
          onClick={() => onChange([...items, itemDraft()])}>품목 추가</button>
      )}
    </fieldset>
  );
}
