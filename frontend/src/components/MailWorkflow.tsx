import { useEffect, useRef, useState } from 'react';
import { request } from '../services/api.js';
import { MailSendReview, type ReplyEnvelope } from './MailSendReview';

export const mailWorkEndpoint = '/api/v1/ai-mail/demo-work';
export const mailWorkLabels = { pending: '미처리', in_progress: '처리 중', done: '처리 완료' };
const fieldLabels = { status: '상태', assignee: '담당자', next_action: '다음 할 일', memo: '처리 메모' };
const action = 'rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-semibold text-blue-800 hover:bg-slate-50 disabled:opacity-50';
const input = 'w-full rounded-lg border border-slate-300 px-3 py-2';
const factLimit = 2000;
const fallbackFactExample = { label: '확인 중 안내', text: '문의하신 내용을 확인 중입니다. 확인이 끝나는 대로 회신드리겠습니다.' };
const factExamples = new Map([
  ['실제 납기와 가능한 일정', { label: '납기·일정', text: '가능한 납품 일정을 확인 중입니다. 일정이 확정되는 대로 안내드리겠습니다.' }],
  ['회신할 가격과 수량', { label: '가격·수량', text: '요청하신 가격과 수량을 확인 중입니다. 확인이 끝나는 대로 회신드리겠습니다.' }],
  ['내부 검토 결과와 승인 여부', { label: '검토·승인', text: '현재 내부 검토 중입니다. 검토 결과가 나오는 대로 안내드리겠습니다.' }],
  ['보낼 자료와 실제 첨부 여부', { label: '자료 전달', text: '요청하신 자료의 전달 가능 여부를 확인 중입니다. 확인이 끝나는 대로 안내드리겠습니다.' }],
]);
const clock = new Intl.DateTimeFormat('ko-KR', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Seoul' });
type WorkStatus = keyof typeof mailWorkLabels;
type WorkEvent = { field: keyof typeof fieldLabels; before: string; after: string; actor: string; at: string };
export type MailWork = { version: number; status: WorkStatus; assignee: string; next_action: string; history: WorkEvent[] };
export type MailWorkOverview = { actor: string; threads: Record<string, MailWork> };
export const emptyMailWork: MailWork = { version: 0, status: 'pending', assignee: '', next_action: '', history: [] };
type Draft = { text: string; checks: string[]; source: 'assisted' | 'demo'; reply: ReplyEnvelope };
type ReplyMail = { id: string; subject: string; sender: string };

function message(problem: unknown, fallback: string) {
  return problem instanceof Error ? problem.message : fallback;
}

export function MailWorkflow({ threadId, messages, viewedMail, draftFormId, record, actor, onSaved }: {
  threadId: string;
  messages: ReplyMail[];
  viewedMail: ReplyMail;
  draftFormId: string;
  record: MailWork;
  actor: string;
  onSaved: (record: MailWork) => void;
}) {
  const path = `${mailWorkEndpoint}/${encodeURIComponent(threadId)}`;
  const [choice, setChoice] = useState<{ viewedId: string; targetId: string }>();
  const target = (choice?.viewedId === viewedMail.id && messages.find((mail) => mail.id === choice.targetId)) || viewedMail;
  const [draft, setDraft] = useState<Draft & { targetId: string }>();
  const [draftText, setDraftText] = useState('');
  const [factInputs, setFactInputs] = useState<Record<string, string>>({});
  const confirmedFacts = factInputs[target.id] ?? '';
  const relevantExamples = draft?.checks.flatMap((check) => factExamples.get(check) ?? []) ?? [];
  const examples = relevantExamples.length ? relevantExamples : [fallbackFactExample];
  const factInputRef = useRef<HTMLTextAreaElement>(null);
  const [draftError, setDraftError] = useState<{ targetId: string; text: string }>();
  const [draftingTarget, setDraftingTarget] = useState<string>();
  const drafting = draftingTarget === target.id;
  const [form, setForm] = useState({ status: record.status, assignee: record.assignee, next_action: record.next_action, memo: '' });
  const [saveError, setSaveError] = useState('');
  const [saveNotice, setSaveNotice] = useState('');
  const [saving, setSaving] = useState(false);
  const draftRequest = useRef<{ controller: AbortController; targetId: string } | null>(null);

  useEffect(() => () => draftRequest.current?.controller.abort(), []);

  async function generateDraft(targetId: string) {
    if (draftRequest.current?.targetId === targetId) return;
    draftRequest.current?.controller.abort();
    const abort = new AbortController();
    draftRequest.current = { controller: abort, targetId };
    setDraftingTarget(targetId);
    setDraftError(undefined);
    try {
      const response: Draft = await request(`${path}/draft`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message_id: targetId, confirmed_facts: factInputs[targetId] ?? '' }), signal: abort.signal,
      });
      if (!abort.signal.aborted) {
        setDraft({ ...response, targetId });
        setDraftText(response.text);
      }
    } catch (problem) {
      if (!abort.signal.aborted) setDraftError({ targetId, text: message(problem, '답장 초안을 만들지 못했습니다.') });
    } finally {
      if (draftRequest.current?.controller === abort) {
        draftRequest.current = null;
        if (!abort.signal.aborted) setDraftingTarget(undefined);
      }
    }
  }

  async function save() {
    if (saving) return;
    setSaving(true);
    setSaveError('');
    setSaveNotice('');
    try {
      const updated: MailWork = await request(path, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...form, version: record.version }),
      });
      setForm({ status: updated.status, assignee: updated.assignee, next_action: updated.next_action, memo: '' });
      onSaved(updated);
      setSaveNotice('업무 기록을 저장했습니다.');
    } catch (problem) {
      setSaveError(message(problem, '업무 기록을 저장하지 못했습니다.'));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <section className="min-w-0 space-y-3 rounded-lg border border-slate-200 p-3 text-sm" aria-label="답장 작성">
        <h3 className="font-semibold">답장 작성</h3>
        <form id={draftFormId} className="space-y-3" onSubmit={(event) => {
          event.preventDefault();
          const submitter = (event.nativeEvent as SubmitEvent).submitter;
          const mailId = submitter instanceof HTMLButtonElement ? submitter.value : '';
          if (mailId) {
            if (!messages.some((mail) => mail.id === mailId)) return;
            setChoice({ viewedId: viewedMail.id, targetId: mailId });
            if (draft?.targetId === mailId) return;
          }
          void generateDraft(mailId || target.id);
        }}>
          <label className="block space-y-1">답장할 메일
            <select className={input} value={target.id} disabled={drafting}
              onChange={(event) => setChoice({ viewedId: viewedMail.id, targetId: event.target.value })}>
              {messages.map((mail) => <option key={mail.id} value={mail.id}>{mail.subject}</option>)}
            </select>
          </label>
          <div className="break-words rounded-md bg-slate-50 p-2 text-slate-800" aria-label="답장 대상">
            <p><span className="font-semibold">제목</span> {target.subject}</p>
            <p><span className="font-semibold">발신자</span> {target.sender}</p>
          </div>
          {draft?.targetId !== target.id && (
            <button type="submit" className={action} disabled={drafting}>
              {drafting ? '답장 작성 중…' : '답장 작성 시작'}
            </button>
          )}
        </form>
        {draft?.targetId === target.id && (
          <>
            <div className="rounded-md bg-amber-50 p-2 text-amber-950">
              <h4 className="font-semibold">답장에 필요한 확인 사항</h4>
              {draft.checks.length ? <ul className="list-disc pl-4">
                {draft.checks.map((check) => <li key={check}>{check}</li>)}
              </ul> : <p>회신에 포함할 내용이 있으면 아래에 적어 주세요.</p>}
            </div>
            <label className="block space-y-1">답장에 반영할 확인 내용
              <textarea ref={factInputRef} className={`${input} resize-y leading-relaxed`} rows={3} maxLength={factLimit}
                placeholder={`예: ${examples[0].text}`}
                value={confirmedFacts} disabled={drafting}
                onChange={(event) => setFactInputs({ ...factInputs, [target.id]: event.target.value })} />
            </label>
            <div role="group" aria-label="확인 내용 예시 넣기" className="flex flex-wrap items-center gap-1.5">
              <span className="text-xs font-semibold text-slate-700">예시 넣기</span>
              {examples.map(({ label, text }) => {
                const separator = confirmedFacts && !confirmedFacts.endsWith('\n') ? '\n' : '';
                const updated = `${confirmedFacts}${separator}${text}`;
                const alreadyAdded = confirmedFacts.split('\n').some((line) => line.trim() === text);
                return (
                  <button key={label} type="button" className="rounded-md border border-slate-300 px-2 py-1 text-xs text-blue-800 hover:bg-blue-50 disabled:opacity-50"
                    aria-label={`${label} 예시 넣기`} title={text} disabled={drafting || alreadyAdded || updated.length > factLimit}
                    onClick={() => {
                      setFactInputs((current) => ({ ...current, [target.id]: updated }));
                      factInputRef.current?.focus();
                    }}>{label}</button>
                );
              })}
            </div>
            <p className="text-xs text-slate-600">삽입한 예시는 실제 상황에 맞게 수정할 수 있습니다.</p>
            <button type="button" className={action} disabled={drafting || !confirmedFacts.trim()}
              onClick={() => void generateDraft(target.id)}>{drafting ? '답장 작성 중…' : '확인 내용으로 답장 완성'}</button>
            <label className="block space-y-1">답장 내용
              <span className="ml-2 text-xs text-slate-600">{draft.source === 'assisted' ? 'AI 보조 작성' : '시연 답장'}</span>
              <textarea className={`${input} resize-y leading-relaxed`} rows={12} maxLength={12000} value={draftText}
                aria-label="답장 내용" disabled={drafting} onChange={(event) => setDraftText(event.target.value)} />
            </label>
          </>
        )}
        {draftError?.targetId === target.id && <p role="alert" className="text-red-800">{draftError.text}</p>}
        <MailSendReview path={`${path}/sends`} reply={!drafting && draft?.targetId === target.id && draftText.trim()
          ? { ...draft.reply, message_id: target.id, text: draftText.trim() } : undefined} />
      </section>
      <section className="space-y-3 rounded-lg border border-slate-200 p-3 text-sm" aria-label="업무 처리">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="font-semibold">업무 처리 · {mailWorkLabels[record.status]}</h3>
          <span className="text-xs text-slate-600">{actor}</span>
        </div>
        <form className="space-y-2" onSubmit={(event) => { event.preventDefault(); void save(); }}>
          <label className="block space-y-1">담당자
            <input className={input} value={form.assignee} maxLength={100} disabled={saving}
              onChange={(event) => setForm({ ...form, assignee: event.target.value })} />
          </label>
          <label className="block space-y-1">처리 상태
            <select className={input} aria-label="처리 상태" value={form.status} disabled={saving}
              onChange={(event) => setForm({ ...form, status: event.target.value as WorkStatus })}>
              {Object.entries(mailWorkLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
          </label>
          <label className="block space-y-1">다음 할 일
            <textarea className={input} rows={2} value={form.next_action} maxLength={500} disabled={saving}
              onChange={(event) => setForm({ ...form, next_action: event.target.value })} />
          </label>
          <label className="block space-y-1">처리 메모
            <textarea className={input} rows={2} value={form.memo} maxLength={1000} disabled={saving}
              onChange={(event) => setForm({ ...form, memo: event.target.value })} />
          </label>
          <button type="submit" className={action} disabled={saving}>{saving ? '저장 중…' : '업무 기록 저장'}</button>
        </form>
        {saveError && <p role="alert" className="text-red-800">{saveError}</p>}
        {saveNotice && <p role="status" className="text-emerald-800">{saveNotice}</p>}
        {!!record.history.length && (
          <section className="space-y-2">
            <h4 className="font-semibold">변경 기록</h4>
            <ul className="max-h-64 divide-y divide-slate-100 overflow-auto">
              {[...record.history].reverse().map((event, index) => (
                <li key={index} className="py-2">
                  <p className="break-words">{fieldLabels[event.field]} · {
                    event.field === 'status' ? `${mailWorkLabels[event.before as WorkStatus]} → ${mailWorkLabels[event.after as WorkStatus]}`
                      : event.after || '지움'
                  }</p>
                  <p className="text-xs text-slate-600">{event.actor} · <time dateTime={event.at}>{clock.format(new Date(event.at))}</time></p>
                </li>
              ))}
            </ul>
          </section>
        )}
      </section>
    </div>
  );
}
