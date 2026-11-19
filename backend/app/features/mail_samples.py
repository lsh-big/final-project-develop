from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone


KST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class Sample:
    thread: str
    sender: str
    subject: str
    sentences: tuple[str, ...]
    summary: str
    category: str
    request: int | None = None
    deadline: int | None = None
    due_offset: int | None = None
    action: str | None = None
    unread: bool = True
    handled: bool = False


SAMPLES = (
    Sample("quote", "가상 한빛전기", "공급 단가 견적 확인",
           ("새 단가 견적서의 단가와 최소 수량을 확인해 주세요.", "검토 결과를 {due}까지 회신해 주세요."),
           "신규 공급 단가와 최소 수량 검토를 요청했습니다.", "reply", 0, 1, 2,
           "견적의 단가와 최소 수량을 내부 기준과 대조합니다.", unread=False),
    Sample("schedule", "가상 서림물류", "납품 일정 변경 문의",
           ("다음 주 중으로 납품 일정을 조정하고 싶습니다.", "변경 가능한 시간을 확인해 주세요."),
           "납품 일정 조정을 요청했지만 가능한 날짜가 정해지지 않았습니다.", "task", 1, 0,
           action="가능한 날짜와 회신 기한을 거래처에 확인합니다."),
    Sample("guide", "가상 동백유통", "출고장 이용 안내",
           ("신규 출고장은 2번 출입구 옆에 있습니다.", "이 메일은 안내용이며 별도 회신은 필요 없습니다."),
           "신규 출고장 위치를 안내했습니다.", "reference", unread=False),
    Sample("invoice", "가상 새봄산전", "거래명세서 금액 정정 요청",
           ("공급가액과 부가세가 기재된 정정본을 {due}까지 보내 주세요.", "기존 명세서 번호는 DM-204입니다."),
           "DM-204의 공급가액과 부가세 정정본이 필요합니다.", "reply", 0, 0, -1,
           "DM-204와 정정 요청을 확인하고 거래처에 진행 상황을 회신합니다."),
    Sample("inspection", "가상 늘푸른배선", "입고 검사표 등록",
           ("입고 검사표를 {due}까지 내부 구매 폴더에 등록해 주세요.", "거래처에 회신하실 필요는 없습니다."),
           "거래처 답장 없이 입고 검사표를 등록해야 합니다.", "task", 0, 0, 0,
           "검사표에 서명이 있는지 확인한 뒤 담당자에게 등록을 요청합니다."),
    Sample("notice", "가상 솔빛제어", "제품 포장 단위 안내",
           ("포장 단위가 상자당 24개에서 20개로 바뀝니다.", "참고용 안내이므로 회신은 필요 없습니다."),
           "포장 단위 변경 사항을 공유했습니다.", "reference"),
    Sample("quote", "가상 한빛전기", "RE: 공급 단가 견적 확인",
           ("오전에 보낸 견적의 최소 주문 수량은 20개로 정정합니다.", "정정 견적 검토 결과를 {due}까지 회신해 주세요."),
           "최소 주문 수량을 20개로 정정하고 견적 검토 결과를 요청했습니다.", "reply", 1, 1, 2,
           "정정된 수량과 단가를 확인하고 회신 내용을 정리합니다."),
    Sample("packaging", "가상 해솔부품", "변경 포장 규격 검토",
           ("변경된 포장 규격을 {due}까지 검토해 주세요.", "검토 결과는 내부 담당자에게 공유해 주세요."),
           "변경된 포장 규격을 검토하고 내부에 공유해야 합니다.", "task", 0, 0, 3,
           "현재 계약서의 포장 규격과 비교합니다."),
    Sample("schedule", "가상 서림물류", "RE: 납품 일정 변경 문의",
           ("차량 배차가 확정되면 변경 시간을 다시 안내하겠습니다.", "우선 다음 주 중 가능한 납품 일정을 알려 주세요."),
           "배차가 미확정이며 가능한 납품 일정을 회신해야 합니다.", "reply", 1, 1,
           action="내부 입고 일정을 확인하고 정확한 회신 기한을 문의합니다."),
    Sample("audit", "가상 미리내상사", "검수 확인서 보관 요청",
           ("검수 확인서를 {due}까지 보관해 주세요.", "보관 이외의 추가 작업이나 회신은 필요 없습니다."),
           "검수 확인서 보관 요청은 샘플에서 처리 완료된 상태입니다.", "task", 0, 0, 1,
           "보관 위치를 필요할 때 확인합니다.", unread=False, handled=True),
)


def mail_samples(today: date):
    messages = []
    for index, sample in enumerate(SAMPLES):
        due = today + timedelta(days=sample.due_offset) if sample.due_offset is not None else None
        sentences = tuple(sentence.format(due=due) for sentence in sample.sentences)
        received = datetime.combine(today - timedelta(days=3 - index // 4), time(9 + index % 4, 10), tzinfo=KST)
        messages.append({
            "id": f"sample-{index + 1:02d}",
            "thread_id": sample.thread,
            "sender": sample.sender,
            "subject": sample.subject,
            "received_at": received.isoformat(),
            "unread": sample.unread,
            "handled": sample.handled,
            "sentences": sentences,
            "analysis": {
                "summary": sample.summary,
                "category": sample.category,
                "request": sentences[sample.request] if sample.request is not None else None,
                "request_evidence": sentences[sample.request] if sample.request is not None else None,
                "deadline": due.isoformat() if due is not None else None,
                "deadline_status": "confirmed" if due else "ambiguous" if sample.deadline is not None else "none",
                "deadline_evidence": sentences[sample.deadline] if sample.deadline is not None else None,
                "next_action": sample.action,
            },
        })
    return messages
