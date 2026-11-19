import type { MailWorkOverview } from '../components/MailWorkflow';

export const mailInboxEndpoint = '/api/v1/ai-mail/demo-import';
export const mailCategories = { reply: '답장 필요', task: '업무 처리 필요', reference: '참고' };
export const mailTimeZone = 'Asia/Seoul';
export const mailDayFormat = new Intl.DateTimeFormat('sv-SE', { year: 'numeric', month: '2-digit', day: '2-digit', timeZone: mailTimeZone });

export type Period = 'all' | 'recent' | 'older';
export type Analysis = {
  summary: string;
  category: keyof typeof mailCategories;
  request: string | null;
  request_evidence: string | null;
  deadline_status: 'confirmed' | 'ambiguous' | 'none';
  deadline: string | null;
  deadline_evidence: string | null;
  next_action: string | null;
};
export type Mail = {
  id: string;
  sender: string;
  subject: string;
  received_at: string;
  unread: boolean;
  handled: boolean;
  sentences: string[];
  vendor?: { id: string; name: string } | null;
  analysis: Analysis | null;
  analysis_status?: 'ready' | 'pending' | 'failed';
};
export type Thread = { id: string; messages: Mail[] };
export type MailEntry = { thread: Thread; mail: Mail };
export type Inbox = { today: string; recent_since: string; threads: Thread[] };
export type SummaryRow = MailEntry & { day: string; reply: boolean; urgent: boolean; overdue: boolean };

export function shiftDay(day: string, offset: number) {
  const date = new Date(`${day}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}

export function availableAnalysis(mail: Mail) {
  return mail.analysis_status === 'failed' || mail.analysis_status === 'pending' ? null : mail.analysis;
}

export function inPeriod(mail: Mail, period: Period, recentSince: string) {
  const recent = mailDayFormat.format(new Date(mail.received_at)) >= recentSince;
  return period === 'all' || (period === 'recent') === recent;
}

export function scopeRows(inbox: Inbox, period: Period, work?: MailWorkOverview): SummaryRow[] {
  const dueLimit = shiftDay(inbox.today, 3);
  const rows = inbox.threads.flatMap((thread) => thread.messages
    .filter((mail) => mail.unread && inPeriod(mail, period, inbox.recent_since))
    .map((mail) => {
      const analysis = availableAnalysis(mail);
      const due = analysis?.deadline_status === 'confirmed' ? analysis.deadline : null;
      const reply = analysis?.category === 'reply' && !mail.handled && work?.threads[thread.id]?.status !== 'done';
      const urgent = reply && due !== null && inbox.today <= due && due <= dueLimit;
      const overdue = reply && due !== null && due < inbox.today;
      return { thread, mail, day: mailDayFormat.format(new Date(mail.received_at)), reply, urgent, overdue };
    }));
  return rows.sort((left, right) => Date.parse(right.mail.received_at) - Date.parse(left.mail.received_at));
}

export function latestByConversation(rows: SummaryRow[]) {
  const seen = new Set<string>();
  return rows.filter(({ thread }) => {
    if (seen.has(thread.id)) return false;
    seen.add(thread.id);
    return true;
  });
}
