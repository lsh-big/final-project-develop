from datetime import datetime, time, timedelta

from .mail_samples import KST


# Each conversation includes the original email and a short follow-up.
TOPICS = (
    ("가상 한빛전기", "분기 단가 견적 검토", "reply", 168, "제어반 부품 견적의 단가와 수량을 검토해 주세요.", "견적 검토 결과를 회신해 주세요."),
    ("가상 새봄산전", "누락된 시험성적서", "reply", 132, "누락된 시험성적서를 보내 주세요.", "시험성적서 전달 일정을 알려 주세요."),
    ("가상 동백유통", "출고장 출입구 변경", "reference", 105, "출고장 출입구가 2번 게이트로 변경됐습니다.", "현장 안내 표지판도 교체됐습니다."),
    ("가상 서림물류", "납품 시간 조정", "ambiguous", 84, "다음 주 중 가능한 납품 시간을 확인해 주세요.", "차량 배차가 확정된 후 다시 안내드리겠습니다."),
    ("가상 늘푸른배선", "불량 품목 사진 정리", "task", 70, "입고 불량 품목 사진을 내부 검수 폴더에 정리해 주세요.", "품목 번호를 사진 파일명에 함께 적어 주세요."),
    ("가상 다온부품", "공급가액 정정 요청", "reply", 57, "납품분의 공급가액을 확인하고 정정 내용을 회신해 주세요.", "정정본에는 명세서 번호를 적어 주세요."),
    ("가상 미리내상사", "반품 운송장 확인", "task", 49, "반품 운송장과 수거 수량을 내부 기록에 등록해 주세요.", "반품 접수 번호도 함께 저장해 주세요."),
    ("가상 솔빛제어", "포장 단위 변경 안내", "reference", 40, "릴레이 포장 단위가 상자당 24개에서 20개로 변경됩니다.", "기존 재고는 원래 포장 단위로 출고됩니다."),
    ("가상 해솔부품", "추가 주문 수량 회신", "reply", 34, "추가 주문이 필요한 수량을 회신해 주세요.", "최소 주문 수량은 20개입니다."),
    ("가상 온누리자재", "현장 직납 일정 변경", "ambiguous", 28, "이달 말쯤 현장 직납이 가능한 날을 확인해 주세요.", "현장 책임자의 일정을 확인 중입니다."),
    ("가상 모아기전", "검수 확인서 보관", "task", 21, "서명이 완료된 검수 확인서를 내부에 보관해 주세요.", "납품일이 누락되지 않았는지 확인해 주세요."),
    ("가상 푸른공급", "연휴 출고 운영 안내", "reference", 14, "연휴 기간에는 출고 담당자 근무 시간이 단축됩니다.", "긴급 출고 건은 사전에 담당 창구로 문의할 수 있습니다."),
    ("가상 두레정밀", "정정 견적 승인 회신", "reply", 9, "정정 견적에서 단가와 공급 수량을 확인해 주세요.", "검토 결과는 메일로 회신해 주세요."),
    ("가상 이룸산업", "구매 목록 검토", "task", 5, "이번 구매 목록을 검토하고 내부 담당자에게 공유해 주세요.", "단종 예정인 품목은 별도 표시해 주세요."),
    ("가상 바른유통", "이번 주 입고 일정 확정", "reply", 3, "이번 주 입고 가능 일정을 확인해 주세요.", "입고 가능 시간대를 회신해 주세요."),
)


def demo_mail_messages(today):
    messages = []
    for index, (sender, subject, kind, age, request, follow_up) in enumerate(TOPICS, 1):
        first_day = today - timedelta(days=age)
        due = (today + timedelta(days=3) if age <= 5 else first_day + timedelta(days=4)
               ) if kind in ("task", "reply") else None
        deadline = f"{due}까지 처리해 주세요." if due else None
        category = "task" if kind == "ambiguous" else kind
        for turn, content in enumerate((request, follow_up)):
            received = datetime.combine(first_day + timedelta(days=turn), time(9 + turn, 20), tzinfo=KST)
            if kind == "reference":
                sentences = (content, "참고용 안내로 회신은 필요하지 않습니다.")
            else:
                sentences = (request, content if turn else "확인 결과를 업무 담당자에게 공유해 주세요.")
                if deadline:
                    sentences += (deadline,)
            messages.append({
                "id": f"demo-{index:02d}-{turn + 1}", "thread_id": f"demo-thread-{index:02d}",
                "sender": sender, "subject": f"RE: {subject}" if turn else subject,
                "received_at": received.isoformat(), "unread": True, "handled": False, "sentences": sentences,
                "analysis": {
                    "summary": content, "category": category, "request": request if kind != "reference" else None,
                    "request_evidence": request if kind != "reference" else None,
                    "deadline": due.isoformat() if due else None,
                    "deadline_status": "confirmed" if due else "ambiguous" if kind == "ambiguous" else "none",
                    "deadline_evidence": deadline or (request if kind == "ambiguous" else None),
                    "next_action": {"reply": "요청 내용을 확인하고 회신에 필요한 정보를 정리합니다.",
                                    "task": "관련 내부 기록과 처리 담당자를 확인합니다.",
                                    "ambiguous": "가능한 일정을 확인하고 정확한 기한을 문의합니다."}.get(kind),
                },
            })
    return messages
